"""Optional stock-code contracts. Run with python run_tests.py test_product_sku.py.

All startup/migration work uses a private instance directory, never local POS data.
The seams are the product/warehouse HTTP APIs, exported workbook, and SQLite
constraints (including an actual legacy database upgraded by application startup).
"""
import importlib.util
import io
from pathlib import Path
import shutil
import sqlite3
import sys
import uuid
import xml.etree.ElementTree as ET
import zipfile
from unittest.mock import patch

from flask import Flask
import pytest
from sqlalchemy.exc import IntegrityError


def load_isolated(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / 'instance').mkdir(exist_ok=True)
    original = Flask.__init__

    def isolated_init(app, *args, **kwargs):
        kwargs.update(root_path=str(root), instance_path=str(root / 'instance'))
        original(app, *args, **kwargs)

    name = '_sku_tests_' + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name('app.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    with patch.object(Flask, '__init__', isolated_init):
        spec.loader.exec_module(module)
    module.app.config.update(TESTING=True, SECRET_KEY='sku-tests-only')
    return module


def dispose(module):
    with module.app.app_context():
        module.db.session.remove()
        module.db.engine.dispose()
    sys.modules.pop(module.__name__, None)


@pytest.fixture(scope='module')
def backend(tmp_path_factory):
    module = load_isolated(tmp_path_factory.mktemp('sku-backend'))
    yield module
    dispose(module)


@pytest.fixture
def world(backend):
    m = backend
    with m.app.app_context():
        token = uuid.uuid4().hex[:10]
        branches = [m.Branch(name='SKU ' + token + suffix, code=token + suffix,
                             is_active=True) for suffix in ('A', 'B')]
        m.db.session.add_all(branches)
        m.db.session.flush()
        user = m.User(username='sku-' + token, password='unused', role='manager',
                      branch_id=branches[0].id)
        m.db.session.add(user)
        m.db.session.commit()
        a, b, uid = branches[0].id, branches[1].id, user.id

    def client(branch=a):
        c = m.app.test_client()
        with c.session_transaction() as session:
            session.update(user_id=uid, username='sku-' + token, role='manager', branch_id=branch)
        return c

    yield m, client, a, b
    with m.app.app_context():
        m.db.session.rollback()
        # Remove dependent records before their products and throwaway branches.
        ids = [p.id for p in m.Product.query.filter(m.Product.branch_id.in_([a, b]))]
        m.WarehouseTransfer.query.filter(m.WarehouseTransfer.product_id.in_(ids)).delete(synchronize_session=False)
        m.WarehouseInventory.query.filter(m.WarehouseInventory.product_id.in_(ids)).delete(synchronize_session=False)
        # Audit history is deliberately append-only; the private instance is
        # discarded as a whole after this module rather than deleting history.
        m.Product.query.filter(m.Product.branch_id.in_([a, b])).delete(synchronize_session=False)
        m.User.query.filter_by(id=uid).delete(synchronize_session=False)
        m.Branch.query.filter(m.Branch.id.in_([a, b])).delete(synchronize_session=False)
        m.db.session.commit()


def create(client, **values):
    payload = dict(name='Neutral item', price=12, stock=5)
    payload.update(values)
    response = client.post('/api/products', json=payload)
    assert response.status_code == 201, response.get_json()
    return client.get('/api/products').get_json()[0]


@pytest.mark.parametrize('multipart', [False, True])
def test_create_trims_text_preserves_zeros_and_independent_barcode(world, multipart):
    _, clients, _, _ = world
    c = clients()
    payload = dict(name='Neutral item', price='12', stock='5', sku='  0007-Ab  ', barcode='998877')
    response = (c.post('/api/products', data=payload, content_type='multipart/form-data')
                if multipart else c.post('/api/products', json=payload))
    assert response.status_code == 201, response.get_json()
    row = c.get('/api/products').get_json()[0]
    assert row['sku'] == '0007-Ab'
    assert row['barcode'] == '998877'
    assert c.get('/api/products/' + str(row['id'])).get_json()['sku'] == '0007-Ab'


@pytest.mark.parametrize('values', [{}, {'sku': None}, {'sku': ''}, {'sku': ' \t '}])
def test_optional_sku_serializes_as_null_and_allows_multiple_products(world, values):
    _, clients, _, _ = world
    c = clients()
    assert create(c, **values)['sku'] is None
    assert create(c, **values)['sku'] is None


@pytest.mark.parametrize('multipart', [False, True])
def test_edit_omission_preserves_and_blank_clears_without_changing_barcode(world, multipart):
    _, clients, _, _ = world
    c = clients()
    row = create(c, sku='000012', barcode='SCAN-123')
    url = '/api/products/' + str(row['id'])
    for payload, expected in [({'name': 'Renamed'}, '000012'), ({'sku': '  '}, None)]:
        response = (c.put(url, data=payload, content_type='multipart/form-data')
                    if multipart else c.put(url, json=payload))
        assert response.status_code == 200, response.get_json()
        updated = c.get(url).get_json()
        assert updated['sku'] == expected
        assert updated['barcode'] == 'SCAN-123'


@pytest.mark.parametrize('invalid', ['X' * 101, 123, True, [], {}])
def test_invalid_sku_rejected_on_create_and_edit_without_mutation(world, invalid):
    _, clients, _, _ = world
    c = clients()
    row = create(c, sku='VALID')
    response = c.post('/api/products', json=dict(name='Rejected', price=1, stock=1, sku=invalid))
    assert response.status_code == 400, response.get_json()
    url = '/api/products/' + str(row['id'])
    response = c.put(url, json={'sku': invalid})
    assert response.status_code == 400, response.get_json()
    assert c.get(url).get_json()['sku'] == 'VALID'
    assert len(c.get('/api/products').get_json()) == 1


def test_maximum_length_and_explicit_null_edit(world):
    _, clients, _, _ = world
    c = clients()
    row = create(c, sku='0' * 100)
    url = '/api/products/' + str(row['id'])
    assert c.put(url, json={'sku': '0' * 100}).status_code == 200
    assert c.put(url, json={'sku': None}).status_code == 200
    assert c.get(url).get_json()['sku'] is None


def test_case_insensitive_duplicate_is_branch_scoped_on_create_and_edit(world):
    _, clients, _, other = world
    c = clients()
    first = create(c, sku='AbC-007')
    second = create(c, sku='OTHER')
    response = c.post('/api/products', json=dict(name='Duplicate', price=1, stock=1, sku=' abc-007 '))
    assert response.status_code == 400, response.get_json()
    url = '/api/products/' + str(second['id'])
    assert c.put(url, json={'sku': 'aBc-007'}).status_code == 400
    assert c.get(url).get_json()['sku'] == 'OTHER'
    assert c.put('/api/products/' + str(first['id']), json={'sku': 'ABC-007'}).status_code == 200
    assert create(clients(other), sku='abc-007')['sku'] == 'abc-007'


@pytest.mark.parametrize('endpoint', [
    '/api/products?q=0007-ab', '/api/products?view=management&q=0007-ab',
    '/api/products?view=lookup&q=0007-ab', '/api/products/search?q=0007-ab',
    '/api/products?view=pos',
])
def test_product_views_return_sku_and_search_by_sku(world, endpoint):
    _, clients, _, other = world
    c = clients()
    row = create(c, sku='0007-Ab')
    create(clients(other), sku='0007-Ab')
    result = c.get(endpoint)
    assert result.status_code == 200
    rows = result.get_json()
    if isinstance(rows, dict):
        rows = rows['items']
    assert [(p['id'], p['sku']) for p in rows] == [(row['id'], '0007-Ab')]


@pytest.mark.parametrize('collision', [False, True])
def test_warehouse_sku_search_transfer_clone_and_collision_atomicity(world, collision):
    m, clients, source, target = world
    c = clients()
    row = create(c, sku='0007-Ab', barcode='SCAN-007')
    if collision:
        create(clients(target), sku='0007-aB', barcode='DIFFERENT', stock=9)
    with m.app.app_context():
        m.db.session.add(m.WarehouseInventory(product_id=row['id'], quantity=10, branch_id=source))
        m.db.session.commit()
    for suffix in ('', '&view=register'):
        rows = c.get('/api/warehouse?q=0007-ab' + suffix).get_json()
        if isinstance(rows, dict):
            rows = rows['items']
        assert len(rows) == 1
        assert rows[0]['sku'] == '0007-Ab'
    response = c.post('/api/warehouse/transfer', json=dict(product_id=row['id'], quantity=3, target_branch_id=target))
    assert response.status_code == (409 if collision else 200), response.get_json()
    with m.app.app_context():
        assert m.WarehouseInventory.query.filter_by(product_id=row['id']).one().quantity == (10 if collision else 7)
        assert m.db.session.get(m.Product, row['id']).stock == 5
        assert m.WarehouseTransfer.query.filter_by(product_id=row['id']).count() == (0 if collision else 1)
    destination = clients(target).get('/api/products').get_json()
    assert len(destination) == 1
    assert destination[0]['sku'] == ('0007-aB' if collision else '0007-Ab')
    assert destination[0]['stock'] == (9 if collision else 3)
    assert destination[0]['barcode'] == ('DIFFERENT' if collision else 'SCAN-007')


def test_catalog_excel_export_has_textual_sku_field_and_search(world):
    _, clients, _, _ = world
    c = clients()
    create(c, sku='000007', barcode='998877')
    create(c, sku='EXCLUDED')
    response = c.get('/api/products/export?format=xlsx&q=000007')
    assert response.status_code == 200
    ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        strings = ET.fromstring(archive.read('xl/sharedStrings.xml'))
        values = [''.join(node.itertext()) for node in strings]
        sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
    rows = []
    for element in sheet.findall('.//s:sheetData/s:row', ns):
        cells = []
        for cell in element.findall('s:c', ns):
            value = cell.find('s:v', ns)
            cells.append(values[int(value.text)] if cell.get('t') == 's' else (value.text if value is not None else None))
        rows.append(cells)
    header_index = next(i for i, values in enumerate(rows) if 'SKU' in values)
    sku_column = rows[header_index].index('SKU')
    assert rows[header_index + 1][sku_column] == '000007'
    assert not any('EXCLUDED' in values for values in rows)


def test_database_enforces_case_insensitive_branch_uniqueness(world):
    m, clients, branch, other = world
    create(clients(), sku='Db-007')
    with m.app.app_context():
        m.db.session.add(m.Product(name='Bypass API', price=1, branch_id=branch, sku='db-007'))
        with pytest.raises(IntegrityError):
            m.db.session.commit()
        m.db.session.rollback()
        m.db.session.add_all([
            m.Product(name='Other branch', price=1, branch_id=other, sku='DB-007'),
            m.Product(name='Optional A', price=1, branch_id=branch, sku=None),
            m.Product(name='Optional B', price=1, branch_id=branch, sku=None),
        ])
        m.db.session.commit()


def test_legacy_database_startup_adds_nullable_sku_and_is_repeatable(tmp_path):
    # Build a real pre-SKU database from the application's schema, then remove
    # only the new column in a copied DB. No deployment database is ever opened.
    seed = load_isolated(tmp_path / 'seed')
    with seed.app.app_context():
        branch = seed.Branch(name='Legacy SKU', code='LEG-SKU', is_active=True)
        seed.db.session.add(branch)
        seed.db.session.flush()
        product = seed.Product(name='Legacy item', barcode='LEGACY-SCAN', price=12, stock=7, branch_id=branch.id)
        seed.db.session.add(product)
        seed.db.session.commit()
        pid, bid = product.id, branch.id
    dispose(seed)
    root = tmp_path / 'upgrade'
    (root / 'instance').mkdir(parents=True)
    path = root / 'instance' / 'pos.db'
    shutil.copy2(tmp_path / 'seed' / 'instance' / 'pos.db', path)
    with sqlite3.connect(path) as connection:
        # Rebuild the product table without SKU or its indexes, as an old DB.
        columns = connection.execute('PRAGMA table_info(product)').fetchall()
        names = [c[1] for c in columns if c[1] != 'sku']
        definitions = ', '.join('"%s" %s%s' % (c[1], c[2], ' PRIMARY KEY' if c[5] else '')
                                for c in columns if c[1] != 'sku')
        selected = ', '.join('"' + name + '"' for name in names)
        connection.execute('CREATE TABLE legacy_product (' + definitions + ')')
        connection.execute('INSERT INTO legacy_product (' + selected + ') SELECT ' + selected + ' FROM product')
        connection.execute('DROP TABLE product')
        connection.execute('ALTER TABLE legacy_product RENAME TO product')
    for attempt in range(2):
        upgraded = load_isolated(root)
        try:
            with upgraded.app.app_context():
                item = upgraded.db.session.get(upgraded.Product, pid)
                assert (item.name, item.barcode, item.stock, item.sku) == ('Legacy item', 'LEGACY-SCAN', 7, None)
                upgraded.db.session.add(upgraded.Product(name='Migrated SKU', price=1, branch_id=bid, sku='Migration-007'))
                if attempt == 0:
                    upgraded.db.session.commit()
                else:
                    with pytest.raises(IntegrityError):
                        upgraded.db.session.commit()
                    upgraded.db.session.rollback()
                upgraded.db.session.add(upgraded.Product(name='Duplicate', price=1, branch_id=bid, sku='migration-007'))
                with pytest.raises(IntegrityError):
                    upgraded.db.session.commit()
                upgraded.db.session.rollback()
        finally:
            dispose(upgraded)