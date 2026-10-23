"""Measure real inventory APIs against synthetic, disposable SQLite data only.

Usage: python benchmark_inventory_scale.py --rows 1000000 --output .scratch/inventory-scale.json
The launcher never imports app, copies only source files/templates, and starts a
secret-free subprocess in a temporary checkout. The database is always deleted.
"""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time


def disposable_environment(checkout):
    """Do not forward deployment credentials, database URLs, or Python hooks."""
    allowed = ('PATH', 'SystemRoot', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP',
               'TMP', 'TMPDIR', 'HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'LANG', 'LC_ALL')
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update(SECRET_KEY='disposable-inventory-benchmark-only',
               PYTHONPATH=str(checkout), PYTHONIOENCODING='utf-8',
               PYTHONDONTWRITEBYTECODE='1', POS_SCALE_DISPOSABLE='1')
    return env


def copy_source(source, checkout):
    """Allowlist source rather than visiting instance/uploads/.env/local data."""
    checkout.mkdir()
    for path in source.glob('*.py'):
        shutil.copy2(path, checkout / path.name)
    shutil.copytree(source / 'templates', checkout / 'templates', ignore=shutil.ignore_patterns(
        '.env*', '*.db', '*.db-*', '*.sqlite*', '*.bak', '__pycache__'))
    (checkout / '.inventory-scale-disposable').write_text('synthetic only', encoding='utf-8')


def seed_inventory(m, rows, chunk_size=10000, analyze=False):
    """SQLite executemany: bounded memory, no per-row ORM or password hashing."""
    with m.app.app_context():
        branch = m.Branch(name='Synthetic Scale', code='SCALE', is_active=True)
        m.db.session.add(branch)
        m.db.session.flush()
        user = m.User(username='scale-manager', password='not-a-login-password',
                      role='manager', branch_id=branch.id)
        category = m.Category(name='Synthetic Category', branch_id=branch.id)
        m.db.session.add_all([user, category])
        m.db.session.commit()
        identity = {'branch': branch.id, 'user': user.id, 'category': category.id}
        # This connection can only point into the temporary, freshly booted app.
        database = Path(m.db.engine.url.database).resolve()
        database.relative_to(Path(__file__).resolve().parent)
        m.db.session.remove()
        raw = m.db.engine.raw_connection()
        try:
            cursor = raw.cursor()
            cursor.execute('PRAGMA synchronous=OFF')
            now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S.%f')
            supplier_count = min(1000, max(2, rows))
            cursor.executemany(
                'INSERT INTO supplier (id,name,is_active,branch_id,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                [(1000 + i, 'RareScaleSupplier' if i == 0 else f'Scale Supplier {i:06d}',
                  1, identity['branch'], now, now) for i in range(supplier_count)])
            rare = rows // 2
            for start in range(0, rows, chunk_size):
                end = min(start + chunk_size, rows)
                cursor.executemany(
                    'INSERT INTO product (id,name,barcode,catalog_id,price,cost,stock,tax_rate,category,category_id,branch_id) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    [(1000 + i, 'RareScaleName' if i == rare else f'Scale Product {i:09d}',
                      'RareScaleBarcode' if i == rare else f'SCALE-{i:09d}', f'scale-{i}',
                      12.34, 2.25, 10, 0, 'Synthetic Category', identity['category'], identity['branch'])
                     for i in range(start, end)])
                cursor.executemany(
                    'INSERT INTO purchase_order (id,po_number,supplier_id,status,total_amount,created_by,branch_id,created_at,updated_at) '
                    'VALUES (?,?,?,?,?,?,?,?,?)',
                    [(1000 + i, 'RareScaleNumber' if i == rare else f'SCALE-PO-{i:09d}',
                      1000 if i == rare else 1001 + i % (supplier_count - 1),
                      ('draft', 'approved', 'received')[i % 3], 22.50, identity['user'], identity['branch'], now, now)
                     for i in range(start, end)])
                cursor.executemany(
                    'INSERT INTO purchase_order_item (id,purchase_order_id,product_id,ordered_qty,received_qty,unit_cost) '
                    'VALUES (?,?,?,?,?,?)',
                    [(1000 + i, 1000 + i, 1000 + i, 10, 5, 2.25) for i in range(start, end)])
                cursor.executemany(
                    'INSERT INTO warehouse_inventory (id,product_id,quantity,unit_cost,branch_id,batch_number,received_date,created_at,updated_at) '
                    'VALUES (?,?,?,?,?,?,?,?,?)',
                    [(1000 + i, 1000 + i, 3 if i % 5 == 0 else 10, 2.25, identity['branch'],
                      f'SCALE-PO-{i:09d}', now, now, now) for i in range(start, end)])
                raw.commit()
                if end == rows or end % 100000 == 0:
                    print(f'Seeded {end:,} / {rows:,} rows in each of four tables', flush=True)
            # Statistics collection is opt-in: on slow disks a full scan can
            # dominate setup. Without sqlite_stat1, SQLite plans with a ~1M-row
            # default estimate, which is the scale under test, so results stay
            # conservative. --analyze collects real statistics when affordable.
            if analyze:
                print('Analyzing all indexes (this scans index pages)', flush=True)
                cursor.execute('PRAGMA analysis_limit=1000')
                cursor.execute('ANALYZE')
                raw.commit()
                print('Synthetic seed and full ANALYZE complete', flush=True)
            else:
                print('Synthetic seed complete (planner defaults, no ANALYZE)', flush=True)
        finally:
            raw.close()
        return identity


def measure_requests(m, identity, repeats):
    from sqlalchemy import event

    client = m.app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=identity['user'], role='manager', branch_id=identity['branch'])
    routes = {
        'products_first': ('/api/products', {'view': 'management', 'per_page': 30}),
        'warehouse_first': ('/api/warehouse', {'view': 'register', 'per_page': 30}),
        'orders_first': ('/api/purchase_orders', {'view': 'register', 'per_page': 30}),
        'products_broad_prefix': ('/api/products', {'view': 'management', 'q': 'Scale Product'}),
        'warehouse_broad_prefix': ('/api/warehouse', {'view': 'register', 'q': 'Scale Product'}),
        'orders_broad_prefix': ('/api/purchase_orders', {'view': 'register', 'q': 'SCALE-PO-'}),
        'warehouse_low': ('/api/warehouse', {'view': 'register', 'low_stock': 'true'}),
        'products_rare_name': ('/api/products', {'view': 'management', 'q': 'rarescalename'}),
        'products_rare_barcode': ('/api/products', {'view': 'management', 'q': 'rarescalebarcode'}),
        'warehouse_rare_name': ('/api/warehouse', {'view': 'register', 'q': 'rarescalename'}),
        'orders_rare_number': ('/api/purchase_orders', {'view': 'register', 'q': 'rarescalenumber'}),
        'orders_rare_supplier': ('/api/purchase_orders', {'view': 'register', 'q': 'rarescalesupplier'}),
        'product_lookup': ('/api/purchase_orders/products', {'q': 'rarescalename'}),
        'supplier_lookup': ('/api/suppliers', {'view': 'lookup', 'q': 'rarescalesupplier'}),
        'categories_no_counts': ('/api/categories', {'counts': 'false'}),
        'warehouse_summary': ('/api/warehouse/summary', {}),
        'orders_summary': ('/api/purchase_orders/summary', {}),
    }
    for name, path, view in [('products', '/api/products', 'management'),
                             ('warehouse', '/api/warehouse', 'register'),
                             ('orders', '/api/purchase_orders', 'register')]:
        routes[name + '_no_match'] = (path, {'view': view, 'q': 'NoSyntheticMatchExists'})
    results = {}
    with m.app.app_context():
        engine = m.db.engine
        for name, (path, params) in list(routes.items()):
            print(f'Measuring {name}', flush=True)
            samples, statements, payload, status = [], [], None, None

            def capture(connection, cursor, statement, parameters, context, executemany):
                if statement.lstrip().upper().startswith('SELECT'):
                    statements.append((statement, parameters))

            for _ in range(repeats):
                statements.clear()
                m.db.session.remove()
                event.listen(engine, 'before_cursor_execute', capture)
                started = time.perf_counter()
                try:
                    response = client.get(path, query_string=params)
                    payload, status = response.get_json(), response.status_code
                    samples.append((time.perf_counter() - started) * 1000)
                finally:
                    event.remove(engine, 'before_cursor_execute', capture)
                if status != 200:
                    raise RuntimeError(f'{name}: HTTP {status}: {payload}')
            plans = []
            with engine.connect() as connection:
                for sql, parameters in statements:
                    plan = connection.exec_driver_sql('EXPLAIN QUERY PLAN ' + sql, parameters).fetchall()
                    plans.append({'sql': sql, 'plan': [list(row) for row in plan]})
            result = {'status': status, 'median_ms': round(statistics.median(samples), 3),
                      'samples_ms': [round(value, 3) for value in samples],
                      'selects': len(statements), 'response_bytes': len(response.data), 'explain': plans}
            if isinstance(payload, list):
                result['items'] = len(payload)
            elif 'items' in payload:
                result.update(items=len(payload['items']), has_more=payload['has_more'])
                if payload['next_cursor'] and name.endswith('_first'):
                    # Record a real second-page request with the same filters.
                    next_params = dict(params, cursor=payload['next_cursor'])
                    started = time.perf_counter()
                    next_response = client.get(path, query_string=next_params)
                    result['next_page_ms'] = round((time.perf_counter() - started) * 1000, 3)
                    result['next_page_status'] = next_response.status_code
                    if next_response.status_code != 200:
                        raise RuntimeError(f'{name}: second page failed')
            else:
                result['summary'] = payload
            if name.endswith('_no_match') and result.get('items') != 0:
                raise RuntimeError(f'{name}: no-match search unexpectedly returned items')
            if '_rare_' in name and result.get('items') != 1:
                raise RuntimeError(f'{name}: rare prefix should return exactly one row')
            if name.endswith('_first'):
                # Mint the exact same signed cursor the application uses, in
                # the same session/filter context, to isolate deep-seek cost
                # without making 33,000 requests to traverse a million rows.
                from inventory_paging import _encode_cursor
                with m.app.test_request_context(path, query_string=params):
                    from flask import session
                    session.update(user_id=identity['user'], role='manager', branch_id=identity['branch'])
                    deep_cursor = _encode_cursor(1031)
                started = time.perf_counter()
                deep = client.get(path, query_string=dict(params, cursor=deep_cursor))
                result['deep_page_ms'] = round((time.perf_counter() - started) * 1000, 3)
                result['deep_page_status'] = deep.status_code
                if deep.status_code != 200:
                    raise RuntimeError(f'{name}: deep cursor failed: {deep.get_json()}')
                deep_ids = [row['id'] for row in deep.get_json()['items']]
                if not deep_ids or not all(identifier < 1031 for identifier in deep_ids):
                    raise RuntimeError(f'{name}: deep cursor boundary violated: {deep_ids}')
            results[name] = result
            print(f'{name}: {result["median_ms"]:.3f} ms, {len(statements)} SELECTs', flush=True)
    return results


def worker(args):
    root = Path(__file__).resolve().parent
    if (os.environ.get('POS_SCALE_DISPOSABLE') != '1'
            or not (root / '.inventory-scale-disposable').is_file()
            or root.parent.parent != Path(tempfile.gettempdir()).resolve()
            or (root / '.env').exists()):
        raise RuntimeError('Worker requires the launcher-created disposable checkout')
    sys.path.insert(0, str(root))
    import app as m

    import faulthandler
    faulthandler.dump_traceback_later(60, repeat=True)
    m.app.config.update(TESTING=True)
    started = time.perf_counter()
    try:
        identity = seed_inventory(m, args.rows, analyze=args.analyze)
        seeded = time.perf_counter() - started
        report = {'rows_per_table': args.rows, 'tables': ['product', 'purchase_order',
                  'purchase_order_item', 'warehouse_inventory'], 'seed_seconds': round(seeded, 3),
                  'python': sys.version, 'disposable': True,
                  'statistics': 'full ANALYZE (analysis_limit=1000)' if args.analyze
                  else 'SQLite planner defaults (~1,048,576 rows); no ANALYZE',
                  'requests': measure_requests(m, identity, args.repeats)}
        (root / 'benchmark-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    finally:
        faulthandler.cancel_dump_traceback_later()
        with m.app.app_context():
            m.db.session.remove()
            m.db.engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', type=int, default=1000000, help='rows in EACH of four tables')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--output', type=Path, help='optional JSON report, never a database')
    parser.add_argument('--analyze', action='store_true',
                        help='collect full SQLite statistics (slow: scans index pages)')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.rows < 1 or args.repeats < 1:
        parser.error('rows and repeats must be positive')
    if args.worker:
        worker(args)
        return 0
    source = Path(__file__).resolve().parent
    output = args.output.resolve() if args.output else None
    if output and (output.suffix.lower() != '.json' or output.exists()):
        parser.error('output must be a NEW .json report (existing files are never overwritten)')
    with tempfile.TemporaryDirectory(prefix='pos-inventory-scale-') as directory:
        checkout = Path(directory) / 'source'
        copy_source(source, checkout)
        print('Benchmark database is disposable; no local data or secrets copied.', flush=True)
        result = subprocess.run([sys.executable, '-B', str(checkout / Path(__file__).name),
                                 '--worker', '--rows', str(args.rows), '--repeats', str(args.repeats)],
                                cwd=checkout, env=disposable_environment(checkout))
        if result.returncode:
            return result.returncode
        report = (checkout / 'benchmark-report.json').read_text(encoding='utf-8')
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open('x', encoding='utf-8') as handle:
                handle.write(report)
            print(f'Report: {output}', flush=True)
        else:
            print(report)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())