"""Inventory API/query-budget regressions. Run with python run_tests.py this_file.

An independently initialized app retains real startup indexes: deliberately no
drop_all/create_all, and no import of the deployment app/database at collection.
"""

from contextlib import contextmanager
from datetime import datetime
import importlib.util
from pathlib import Path
import re
import sys
import tempfile
from unittest.mock import patch

from flask import Flask
import pytest
from sqlalchemy import event, text


VIEWS = [('/api/products', 'management'), ('/api/warehouse', 'register'),
         ('/api/purchase_orders', 'register')]


@pytest.fixture(scope='module')
def world():
    with tempfile.TemporaryDirectory(prefix='pos-inventory-regression-') as directory:
        root = Path(directory)
        original = Flask.__init__

        def isolated_init(app, *args, **kwargs):
            kwargs.update(root_path=str(root), instance_path=str(root / 'instance'))
            original(app, *args, **kwargs)

        spec = importlib.util.spec_from_file_location('_inventory_scale_test_app',
                                                     Path(__file__).with_name('app.py'))
        m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = m
        with patch.object(Flask, '__init__', isolated_init):
            spec.loader.exec_module(m)
        m.app.config.update(TESTING=True, SECRET_KEY='inventory-regression-only')
        with m.app.app_context():
            branch = m.Branch(name='Scale Local', code='SCL', is_active=True)
            foreign = m.Branch(name='Scale Foreign', code='SCF', is_active=True)
            m.db.session.add_all([branch, foreign])
            m.db.session.flush()
            user = m.User(username='scale-regression', password='unused', role='manager', branch_id=branch.id)
            category = m.Category(name='Scale Category', branch_id=branch.id)
            unit = m.Unit(name='Scale Unit', symbol='su')
            suppliers = [m.Supplier(name=f'Scale Supplier {i:03d}', branch_id=foreign.id) for i in range(131)]
            m.db.session.add_all([user, category, unit, *suppliers])
            m.db.session.flush()
            products = [m.Product(name=f'Scale Product {i:03d}', barcode=f'SCB-{i:03d}',
                                  price=12.34, cost=2.25, stock=10, tax_rate=0,
                                  branch_id=branch.id, category_id=category.id,
                                  category=category.name, unit_id=unit.id) for i in range(131)]
            m.db.session.add_all(products)
            m.db.session.flush()
            now = datetime.utcnow()
            orders = [m.PurchaseOrder(po_number=f'SCALE-PO-{i:03d}', supplier_id=suppliers[i].id,
                                     status='approved' if i % 2 else 'draft', total_amount=22.50,
                                     branch_id=foreign.id, created_by=user.id,
                                     created_at=now, updated_at=now) for i in range(131)]
            warehouse = [m.WarehouseInventory(product_id=p.id, quantity=3 if i % 2 else 10,
                                              unit_cost=2.25, branch_id=foreign.id)
                         for i, p in enumerate(products)]
            m.db.session.add_all([*orders, *warehouse])
            m.db.session.flush()
            # Many lines on every PO; list serialization must aggregate, not load graphs.
            m.db.session.add_all([m.PurchaseOrderItem(purchase_order_id=po.id,
                                   product_id=products[(i + j) % 131].id,
                                   ordered_qty=10, received_qty=10 if j == 0 else 2, unit_cost=2.25)
                                 for i, po in enumerate(orders) for j in range(20)])
            foreign_product = m.Product(name='Foreign Only', price=1, branch_id=foreign.id)
            m.db.session.add(foreign_product)
            m.db.session.commit()
            m.scale = dict(branch=branch.id, foreign=foreign.id, user=user.id,
                           products=[p.id for p in products], orders=[p.id for p in orders],
                           warehouse=[p.id for p in warehouse], suppliers=[s.id for s in suppliers],
                           foreign_product=foreign_product.id, category=category.id)
            m.db.session.remove()
        yield m
        with m.app.app_context():
            m.db.session.remove()
            m.db.engine.dispose()
        sys.modules.pop(spec.name, None)


def client(world, branch=None):
    result = world.app.test_client()
    with result.session_transaction() as session:
        session.update(user_id=world.scale['user'], role='manager',
                       branch_id=branch or world.scale['branch'])
    return result


@contextmanager
def capture(world):
    statements, loaded = [], []
    with world.app.app_context():
        engine = world.db.engine

        def sql(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith('SELECT'):
                statements.append(statement)
                if 'OFFSET' in statement.upper():
                    # SQLAlchemy SQLite emits OFFSET 0 with LIMIT. A nonzero
                    # value would reintroduce deep-page work on cursor routes.
                    assert parameters[-1] == 0, (statement, parameters)

        def materialized(session, instance):
            loaded.append(type(instance).__name__)

        event.listen(engine, 'before_cursor_execute', sql)
        event.listen(world.db.session.session_factory, 'loaded_as_persistent', materialized)
        try:
            yield statements, loaded
        finally:
            event.remove(engine, 'before_cursor_execute', sql)
            event.remove(world.db.session.session_factory, 'loaded_as_persistent', materialized)


def bounded_sql(statements, budget=10):
    assert len(statements) <= budget, '\n'.join(statements)
    # SQLite's dialect renders LIMIT ? OFFSET ? even for an absent offset; the
    # query-budget tests additionally inspect actual parameters below.
    assert not any(re.search(r'COUNT\s*\(', sql, re.I) and 'GROUP BY' not in sql.upper()
                   for sql in statements), '\n'.join(statements)


@pytest.mark.parametrize('path,view', VIEWS)
def test_cursor_pages_complete_bounded_descending_without_count_or_deep_offset(world, path, view):
    c = client(world)
    ids, cursor = [], None
    while True:
        params = {'view': view, 'per_page': 30}
        if cursor:
            params['cursor'] = cursor
        with capture(world) as (statements, loaded):
            response = c.get(path, query_string=params)
        assert response.status_code == 200, response.get_json()
        data = response.get_json()
        assert set(data) == {'items', 'has_more', 'next_cursor'}
        assert len(data['items']) <= 30
        bounded_sql(statements)
        assert 'PurchaseOrderItem' not in loaded
        page = [item['id'] for item in data['items']]
        assert page == sorted(page, reverse=True)
        assert not set(page) & set(ids)
        ids.extend(page)
        if not data['has_more']:
            assert data['next_cursor'] is None
            break
        cursor = data['next_cursor']
        assert isinstance(cursor, str) and not cursor.isdigit()
        assert len(ids) < 300
    key = {'/api/products': 'products', '/api/warehouse': 'warehouse',
           '/api/purchase_orders': 'orders'}[path]
    assert ids == sorted(world.scale[key], reverse=True)


@pytest.mark.parametrize('path,view', VIEWS)
def test_default_maximum_legacy_and_invalid_page_sizes(world, path, view):
    c = client(world)
    assert len(c.get(path, query_string={'view': view}).get_json()['items']) == 30
    assert len(c.get(path, query_string={'view': view, 'per_page': 99999}).get_json()['items']) == 100
    legacy = c.get(path).get_json()
    assert isinstance(legacy, list) and len(legacy) == 100
    for invalid in ('0', '-1', 'not-an-integer'):
        assert c.get(path, query_string={'view': view, 'per_page': invalid}).status_code == 400


@pytest.mark.parametrize('path,view', VIEWS)
def test_signed_cursor_rejects_tampering_filter_endpoint_and_branch_reuse(world, path, view):
    c = client(world)
    params = {'view': view, 'per_page': 30}
    cursor = c.get(path, query_string=params).get_json()['next_cursor']
    for invalid in ('123', 'garbage', cursor[:-1] + ('a' if cursor[-1] != 'a' else 'b')):
        assert c.get(path, query_string=dict(params, cursor=invalid)).status_code == 400
    assert c.get(path, query_string=dict(params, cursor=cursor, q='Scale')).status_code == 400
    extra_filters = {'/api/products': {'per_page': 31},
                     '/api/warehouse': {'low_stock': 'true'},
                     '/api/purchase_orders': {'status': 'approved',
                                              'supplier_id': str(world.scale['suppliers'][0]),
                                              'start_date': '2020-01-01', 'end_date': '2099-01-01'}}
    for key, value in extra_filters[path].items():
        assert c.get(path, query_string=dict(params, cursor=cursor, **{key: value})).status_code == 400
    assert client(world, world.scale['foreign']).get(path, query_string=dict(params, cursor=cursor)).status_code == 400
    other, other_view = next((p, v) for p, v in VIEWS if p != path)
    assert c.get(other, query_string={'view': other_view, 'per_page': 30, 'cursor': cursor}).status_code == 400


def test_filters_prefix_literal_wildcards_and_legacy_substring(world):
    c = client(world)
    for q in ('sCaLe PrOdUcT 130', 'scb-130'):
        for path, view in VIEWS[:2]:
            items = c.get(path, query_string={'view': view, 'q': q}).get_json()['items']
            assert len(items) == 1
    assert len(c.get('/api/products', query_string={'view': 'management', 'q': 'Scale Category'}).get_json()['items']) == 30
    assert c.get('/api/products', query_string={'view': 'management', 'q': 'Product 130'}).get_json()['items'] == []
    assert len(c.get('/api/products?q=Product%20130').get_json()) == 1
    for path, view in VIEWS:
        assert c.get(path, query_string={'view': view, 'q': '%_'}).get_json()['items'] == []
        assert c.get(path, query_string={'view': view, 'q': 'no-match-exists'}).get_json()['items'] == []
    low = c.get('/api/warehouse?view=register&low_stock=true').get_json()['items']
    assert low and all(0 < row['quantity'] <= 5 for row in low)
    for q in ('scale-po-130', 'scale supplier 130'):
        rows = c.get('/api/purchase_orders', query_string={'view': 'register', 'q': q}).get_json()['items']
        assert len(rows) == 1 and rows[0]['items_count'] == 20
        assert rows[0]['total_ordered'] == 200 and rows[0]['received_items_count'] == 1
    today = datetime.utcnow().strftime('%Y-%m-%d')
    rows = c.get('/api/purchase_orders', query_string={'view': 'register', 'status': 'approved',
                 'supplier_id': world.scale['suppliers'][129], 'start_date': today, 'end_date': today}).get_json()['items']
    assert len(rows) == 1 and rows[0]['status'] == 'approved'
    legacy = c.get('/api/products?page=2&per_page=100').get_json()
    assert len(legacy['items']) == 31 and legacy['total'] == 131
    assert c.get('/api/products?page=101&per_page=30').status_code == 400


def test_remote_lookups_are_bounded_and_exact_ids_cross_branch(world):
    c = client(world)
    for path, params, identifier in [('/api/purchase_orders/products', {}, world.scale['foreign_product']),
                                    ('/api/suppliers', {'view': 'lookup'}, world.scale['suppliers'][-1])]:
        with capture(world) as (statements, loaded):
            result = c.get(path, query_string=dict(params, limit=99999)).get_json()
        assert isinstance(result, list) and len(result) == 50
        bounded_sql(statements)
        exact = c.get(path, query_string=dict(params, id=identifier, q='ignored')).get_json()
        assert [item['id'] for item in exact] == [identifier]
        assert c.get(path, query_string=dict(params, id=2147483647)).get_json() == []
    assert len(c.get('/api/purchase_orders/products?q=sCaLe%20Product%20130').get_json()) == 1
    assert len(c.get('/api/suppliers?view=lookup&q=sCaLe%20Supplier%20130').get_json()) == 1


def test_category_no_counts_and_sql_summaries_do_not_load_inventory_graphs(world):
    c = client(world)
    with capture(world) as (statements, loaded):
        rows = c.get('/api/categories?counts=false').get_json()
    assert rows and all(row['product_count'] == row['supplier_count'] == 0 for row in rows)
    assert not {'Product', 'Supplier'} & set(loaded)
    assert not any(re.search(r'\b(FROM|JOIN)\s+(product|supplier)\b', sql, re.I) for sql in statements)
    for path, budget in [('/api/warehouse/summary', 8), ('/api/purchase_orders/summary', 7)]:
        with capture(world) as (statements, loaded):
            response = c.get(path)
        assert response.status_code == 200
        assert len(statements) <= budget
        assert not {'WarehouseInventory', 'Product', 'PurchaseOrder', 'PurchaseOrderItem'} & set(loaded)
        assert any('SUM(' in sql.upper() for sql in statements)
        if 'warehouse' in path:
            assert response.get_json()['total_units'] == 855
            assert response.get_json()['total_value'] == 1923.75
        else:
            assert response.get_json()['total'] == 131
            assert sum(bool(re.search(r'\bFROM\s+purchase_order\b', sql, re.I))
                       for sql in statements) == 1


@pytest.mark.parametrize('model_name,path', [('Product', '/api/products/export'),
                         ('WarehouseInventory', '/api/warehouse/export'),
                         ('PurchaseOrder', '/api/purchase_orders/export'),
                         ('PurchaseOrderItem', '/api/purchase_orders/export')])
def test_oversized_exports_reject_before_materializing_graphs(world, model_name, path):
    m, now = world, datetime.utcnow()
    model = getattr(m, model_name)
    with m.app.app_context():
        rows = []
        for i in range(5001):
            row = {'id': 10000 + i}
            if model_name == 'Product':
                row.update(name=f'Export {i}', price=1, branch_id=m.scale['branch'])
            elif model_name == 'WarehouseInventory':
                row.update(product_id=m.scale['products'][0], quantity=1, unit_cost=1,
                           branch_id=m.scale['branch'], created_at=now)
            elif model_name == 'PurchaseOrder':
                row.update(po_number=f'EXPORT-{i}', supplier_id=m.scale['suppliers'][0],
                           branch_id=m.scale['branch'], created_at=now)
            else:
                row.update(purchase_order_id=m.scale['orders'][0], product_id=m.scale['products'][0], ordered_qty=1)
            rows.append(row)
        m.db.session.bulk_insert_mappings(model, rows)
        m.db.session.commit()
    try:
        with capture(m) as (statements, loaded):
            response = client(m).get(path)
        assert response.status_code == 413, response.get_json()
        assert response.is_json
        assert not {'Product', 'WarehouseInventory', 'PurchaseOrder', 'PurchaseOrderItem'} & set(loaded)
        assert len(statements) <= 10
    finally:
        with m.app.app_context():
            model.query.filter(model.id >= 10000).delete(synchronize_session=False)
            m.db.session.commit()


def test_startup_indexes_and_query_plans_use_real_prefix_fifo_and_register_indexes(world):
    expected = {'idx_product_branch_name_nocase', 'idx_product_branch_barcode_nocase',
                'idx_product_branch_category_nocase', 'idx_product_category_id',
                'idx_product_name_nocase', 'idx_product_barcode_nocase', 'idx_supplier_name_nocase',
                'idx_po_item_order', 'idx_po_item_product', 'idx_po_status_id', 'idx_po_supplier_id',
                'idx_po_created_id', 'idx_po_number_nocase', 'idx_warehouse_active_id',
                'idx_warehouse_low_id', 'idx_warehouse_fifo', 'idx_warehouse_batch_fifo', 'idx_transfer_created'}
    probes = [
        ("SELECT id FROM product WHERE branch_id=1 AND name LIKE 'Scale%' ORDER BY id DESC LIMIT 31", 'idx_product_branch_name_nocase'),
        ("SELECT id FROM product WHERE name LIKE 'Scale%' LIMIT 31", 'idx_product_name_nocase'),
        ("SELECT id FROM supplier WHERE name LIKE 'Scale%' LIMIT 31", 'idx_supplier_name_nocase'),
        ("SELECT id FROM purchase_order_item WHERE purchase_order_id=1", 'idx_po_item_order'),
        ("SELECT id FROM purchase_order WHERE status='draft' ORDER BY id DESC LIMIT 31", 'idx_po_status_id'),
        ("SELECT id FROM warehouse_inventory WHERE quantity>0 AND quantity<=5 ORDER BY id DESC LIMIT 31", 'idx_warehouse_low_id'),
        # INDEXED BY tests the FIFO index's ordering/partial-predicate usability;
        # tiny fixtures may otherwise prefer the competing product/qty index.
        ("SELECT id FROM warehouse_inventory INDEXED BY idx_warehouse_fifo WHERE product_id=1 AND quantity>0 ORDER BY received_date,id LIMIT 31", 'idx_warehouse_fifo'),
    ]
    with world.app.app_context():
        indexes = dict(world.db.session.execute(text("SELECT name,sql FROM sqlite_master WHERE type='index'")).all())
        assert expected <= indexes.keys()
        assert 'COLLATE NOCASE' in indexes['idx_product_branch_name_nocase'].upper()
        assert 'WHERE QUANTITY > 0' in indexes['idx_warehouse_active_id'].upper()
        for sql, index in probes:
            plan = ' '.join(str(tuple(row)) for row in world.db.session.execute(text('EXPLAIN QUERY PLAN ' + sql)))
            assert index in plan, plan