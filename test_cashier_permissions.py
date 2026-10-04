"""HTTP authorization regressions; subprocess uses disposable SQLite before app import."""
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch
import pytest

CHILD = os.environ.get('POS_PERMISSION_CHILD') == '1'


@pytest.mark.skipif(CHILD, reason='Parent launcher only')
def test_isolated_backend_permissions():
    with tempfile.TemporaryDirectory(prefix='pos-permissions-') as directory:
        env = dict(os.environ, POS_PERMISSION_CHILD='1', POS_PERMISSION_INSTANCE=directory)
        result = subprocess.run([sys.executable, '-m', 'pytest', str(Path(__file__).resolve()),
                                 '-q', '--tb=short'], env=env, capture_output=True,
                                text=True, timeout=240, cwd=Path(__file__).resolve().parent)
    assert result.returncode == 0, result.stdout + '\n' + result.stderr


@pytest.fixture(scope='module')
def backend():
    if not CHILD:
        pytest.skip('Runs in disposable subprocess')
    from flask import Flask
    original = Flask.__init__
    def isolated_init(self, *args, **kwargs):
        kwargs['instance_path'] = os.environ['POS_PERMISSION_INSTANCE']
        original(self, *args, **kwargs)
    # app hardcodes sqlite:///pos.db and migrates on import: redirect Flask's
    # instance path before import, never import it against the real instance.
    with patch.object(Flask, '__init__', isolated_init):
        import app as module
    module.app.config.update(TESTING=True)
    with module.app.app_context():
        assert str(module.db.engine.url.database).startswith(os.environ['POS_PERMISSION_INSTANCE'])
    yield module
    with module.app.app_context():
        module.db.session.remove()
        module.db.engine.dispose()


@pytest.fixture
def world(backend):
    m = backend
    with m.app.app_context():
        m.db.session.remove()
        m.db.drop_all()
        m.db.create_all()
        branches = [m.Branch(name='Local', code='LOCAL', is_default=True),
                    m.Branch(name='Foreign', code='FOREIGN'),
                    m.Branch(name='Inactive', code='OFF', is_active=False)]
        users = [m.User(username=name, password='test-only', role=role) for name, role in
                 [('own', 'cashier'), ('other', 'cashier'), ('manager', 'manager'), ('boss', 'boss')]]
        m.db.session.add_all(branches + users)
        m.db.session.flush()
        products = [m.Product(name=name, barcode=name, branch_id=b.id,
                              price=100, cost=61, stock=50, tax_rate=0) for name, b in
                    [('LocalProduct', branches[0]), ('ForeignProduct', branches[1])]]
        customers = [m.Customer(name=name, branch_id=b.id) for name, b in
                     [('LocalCustomer', branches[0]), ('ForeignCustomer', branches[1])]]
        m.db.session.add_all(products + customers)
        m.db.session.flush()
        sales, items, deliveries, workflows = [], [], [], []
        for i, (user, branch, product) in enumerate([(users[0], branches[0], products[0]),
                (users[1], branches[0], products[0]), (users[0], branches[1], products[1])]):
            sale = m.Sale(transaction_id=f'permission-sale-{i}', total=200, tax=0,
                          payment_method='cash', cash_received=200, user_id=user.id, branch_id=branch.id)
            m.db.session.add(sale)
            m.db.session.flush()
            item = m.SaleItem(sale_id=sale.id, product_id=product.id, quantity=2, price=100, tax=0)
            delivery = m.Delivery(delivery_number=f'PERMISSION-DLV-{i}', sale_id=sale.id,
                                  branch_id=branch.id, created_by=user.id, recipient_name=f'Recipient-{i}',
                                  recipient_phone='123', delivery_address='Test address')
            workflow = m.ReturnExchange(workflow_id=f'permission-workflow-{i}', mode='return',
                original_sale_id=sale.id, user_id=user.id, return_total=0, exchange_total=0,
                net_total=0, refund_amount=0, collected_amount=0, settlement_method='cash')
            m.db.session.add_all([item, delivery, workflow])
            sales.append(sale)
            items.append(item)
            deliveries.append(delivery)
            workflows.append(workflow)
        m.db.session.commit()
        w = dict(m=m, client=m.app.test_client(), branches=branches, users=users,
                 products=products, customers=customers, sales=sales, items=items,
                 deliveries=deliveries, workflows=workflows)
        login(w)
        yield w
        m.db.session.rollback()
        m.db.session.remove()


def login(w, role='cashier', branch=0):
    user = w['users'][{'cashier': 0, 'manager': 2, 'boss': 3}[role]]
    with w['client'].session_transaction() as s:
        s.clear()
        s.update(user_id=user.id, username=user.username, role=role)
        if branch is not None:
            s['branch_id'] = w['branches'][branch].id


def checkout(w, **overrides):
    payload = dict(items=[dict(product_id=w['products'][0].id, quantity=1, price=100)],
                   payment_method='cash', cash_received=100)
    payload.update(overrides)
    return payload


def state(w):
    m = w['m']
    m.db.session.expire_all()
    return ([p.stock for p in w['products']], *[model.query.count() for model in
            (m.Sale, m.SaleItem, m.ReturnExchange, m.ReturnExchangeItem, m.Debt, m.Delivery)])


def assert_safe(value):
    if isinstance(value, dict):
        for key, child in value.items():
            assert not any(term in key.lower() for term in ('cost', 'profit', 'supplier', 'agreed_price', 'margin'))
            assert_safe(child)
    elif isinstance(value, list):
        for child in value:
            assert_safe(child)



DENIED = [
('/api/branches', 'GET POST'), ('/api/branches/1', 'GET PUT DELETE'),
('/api/branches/switch/2', 'POST'), ('/api/users', 'GET POST'),
('/api/users/2', 'GET PUT DELETE'), ('/api/customers', 'GET POST'),
('/api/customers/1', 'GET PUT DELETE'), ('/api/suppliers', 'GET POST'),
('/api/suppliers/1', 'GET PUT DELETE'), ('/api/purchase_orders', 'GET POST'),
('/api/purchase_orders/1', 'GET PUT'), ('/api/purchase_orders/1/approve', 'POST'),
('/api/purchase_orders/1/receive', 'POST'), ('/api/warehouse', 'GET'),
('/api/warehouse/transfer', 'POST'), ('/api/inventory/alerts', 'GET'),
('/api/debts', 'GET POST'), ('/api/debts/1', 'GET PUT DELETE'),
('/api/debts/1/payment', 'POST'), ('/api/promotions', 'GET POST'),
('/api/promotions/1', 'GET PUT DELETE'), ('/api/logs', 'GET'),
('/api/settings', 'PUT'), ('/api/settings/database_backup', 'GET'),
('/api/settings/database_restore', 'POST'), ('/api/products', 'POST'),
('/api/products/1', 'PUT DELETE'), ('/api/categories', 'POST'),
('/api/categories/1', 'PUT DELETE'), ('/api/units', 'POST'),
('/api/units/1', 'PUT DELETE'), ('/api/deliveries', 'POST'),
('/api/deliveries/1', 'PUT'), ('/api/deliveries/export', 'GET'),
('/api/reports/sales/export', 'GET'), ('/api/dashboard/sales_data', 'GET'),
('/api/dashboard/top_products', 'GET')]


@pytest.mark.parametrize('path,method', [(p, v) for p, verbs in DENIED for v in verbs.split()])
def test_cashier_manager_endpoint_matrix_denied_without_writes(world, path, method):
    before = state(world)
    response = world['client'].open(path, method=method, json={})
    assert response.status_code == 403, (method, path, response.get_data(as_text=True))
    assert state(world) == before


@pytest.mark.parametrize('path', ['/api/products', '/api/products/search?q=Product',
    '/api/products/1', '/api/categories', '/api/units', '/api/pos/customers', '/api/settings'])
def test_read_only_catalog_has_no_costs_or_foreign_records(world, path):
    response = world['client'].get(path)
    assert response.status_code == 200
    assert_safe(response.get_json())
    assert 'ForeignProduct' not in response.get_data(as_text=True)
    assert 'ForeignCustomer' not in response.get_data(as_text=True)


SCOPED = ['/api/products', '/api/sales', '/api/returns_exchanges', '/api/deliveries',
          '/api/deliveries/stats', '/api/deliveries/report', '/api/pos/customers', '/api/reports/sales']


@pytest.mark.parametrize('path', SCOPED)
@pytest.mark.parametrize('branch', ['foreign', 'all'])
def test_foreign_or_all_branch_query_rejected(world, path, branch):
    value = world['branches'][1].id if branch == 'foreign' else 'all'
    assert world['client'].get(path, query_string={'branch_id': value}).status_code == 403


@pytest.mark.parametrize('branch', [None, 2])
@pytest.mark.parametrize('path', SCOPED)
def test_absent_or_inactive_branch_fails_closed(world, branch, path):
    login(world, branch=branch)
    assert world['client'].get(path).status_code == 403


@pytest.mark.parametrize('index,status', [(0, 200), (1, 404), (2, 404)])
@pytest.mark.parametrize('suffix', ['', '/print'])
@pytest.mark.parametrize('kind', ['sales', 'returns_exchanges', 'deliveries'])
def test_detail_and_print_ownership(world, index, status, suffix, kind):
    identifier = {'sales': world['sales'][index].transaction_id,
                  'returns_exchanges': world['workflows'][index].workflow_id,
                  'deliveries': world['deliveries'][index].id}[kind]
    assert world['client'].get(f'/api/{kind}/{identifier}{suffix}').status_code == status


@pytest.mark.parametrize('path', ['/api/sales?scope=all', '/api/reports/sales?scope=all',
    '/api/returns_exchanges?scope=all', '/api/returns_exchanges?page=1&per_page=20&scope=all'])
def test_lists_cannot_widen_to_other_cashiers_or_branches(world, path):
    response = world['client'].get(path)
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert world['sales'][0].transaction_id in text
    for sale in world['sales'][1:]:
        assert sale.transaction_id not in text


@pytest.mark.parametrize('path', ['/api/deliveries', '/api/deliveries/stats', '/api/deliveries/report'])
def test_delivery_list_statistics_and_report_only_own_sales(world, path):
    response = world['client'].get(path + '?scope=all')
    assert response.status_code == 200
    if path.endswith('/stats') or path.endswith('/report'):
        assert response.get_json()['total'] == 1
    text = response.get_data(as_text=True)
    for i in (1, 2):
        assert f'Recipient-{i}' not in text
        assert f'PERMISSION-DLV-{i}' not in text


@pytest.mark.parametrize('attack,status', [('product', 404), ('customer', 404), ('debt', 403), ('padded_debt', 403), ('branch', 403)])
def test_checkout_rejects_foreign_objects_and_credit_without_writes(world, attack, status):
    payload = checkout(world)
    if attack == 'product':
        payload['items'][0]['product_id'] = world['products'][1].id
    elif attack == 'customer':
        payload['customer_id'] = world['customers'][1].id
    elif attack in ('debt', 'padded_debt'):
        payload.update(payment_method=' \tDEBT\n' if attack == 'padded_debt' else 'debt', customer_id=world['customers'][0].id)
    else:
        payload['branch_id'] = world['branches'][1].id
    before = state(world)
    assert world['client'].post('/api/sales', json=payload).status_code == status
    assert state(world) == before


def test_cash_customer_association_does_not_create_debt(world):
    response = world['client'].post('/api/sales', json=checkout(world,
        customer_id=world['customers'][0].id, transaction_id='cash-association'))
    assert response.status_code == 201, response.get_data(as_text=True)
    assert response.get_json()['success'] is True
    m = world['m']
    m.db.session.expire_all()
    sale = m.Sale.query.filter_by(transaction_id='cash-association').one()
    assert sale.payment_method == 'cash'
    assert sale.user_id == world['users'][0].id
    assert sale.branch_id == world['branches'][0].id
    assert m.Debt.query.count() == 0
    assert world['products'][0].stock == 49
    assert world['client'].get('/api/sales/cash-association').status_code == 200
    assert world['client'].get('/api/sales/cash-association/print').status_code == 200


@pytest.mark.parametrize('index,status', [(0, 200), (1, 404)])
def test_idempotent_sale_replay_respects_owner(world, index, status):
    before = state(world)
    response = world['client'].post('/api/sales', json=checkout(world,
        transaction_id=world['sales'][index].transaction_id))
    assert response.status_code == status
    if index == 0:
        assert response.get_json()['duplicate'] is True
    assert state(world) == before


@pytest.mark.parametrize('index,status', [(0, 201), (1, 404), (2, 404)])
def test_return_creation_respects_original_sale_owner(world, index, status):
    payload = dict(original_transaction_id=world['sales'][index].transaction_id,
        return_items=[dict(sale_item_id=world['items'][index].id, quantity=1)], settlement_method='cash')
    before = state(world)
    response = world['client'].post('/api/returns_exchanges', json=payload)
    assert response.status_code == status, response.get_data(as_text=True)
    if index:
        assert state(world) == before
    else:
        assert response.get_json()['success'] is True
        world['m'].db.session.expire_all()
        assert world['products'][0].stock == 51


def test_foreign_exchange_product_rejected_without_stock_writes(world):
    payload = dict(original_transaction_id=world['sales'][0].transaction_id,
        return_items=[dict(sale_item_id=world['items'][0].id, quantity=1)],
        exchange_items=[dict(product_id=world['products'][1].id, quantity=1, price=100)],
        settlement_method='cash')
    before = state(world)
    assert world['client'].post('/api/returns_exchanges', json=payload).status_code == 404
    assert state(world) == before


def test_return_export_only_own_workflows(world):
    import zipfile
    response = world['client'].get('/api/returns_exchanges/export?format=xlsx&scope=all')
    assert response.status_code == 200
    # Inspect OOXML directly: no optional Excel reader dependency is needed.
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        text = '\n'.join(archive.read(name).decode('utf-8') for name in archive.namelist()
                         if name.startswith('xl/') and name.endswith('.xml'))
    assert world['sales'][0].transaction_id in text
    for sale in world['sales'][1:]:
        assert sale.transaction_id not in text


@pytest.mark.parametrize('role', ['manager', 'boss'])
def test_manager_behavior_preserved(world, role):
    login(world, role=role)
    response = world['client'].get('/api/products/1')
    assert response.status_code == 200
    assert response.get_json()['cost'] == 61
    response = world['client'].get('/api/sales?scope=all')
    assert response.status_code == 200
    assert {item['transaction_id'] for item in response.get_json()['items']} == {
        s.transaction_id for s in world['sales']}
    assert world['client'].get('/api/sales/permission-sale-1').status_code == 200
    assert world['client'].get('/api/returns_exchanges/permission-workflow-1').status_code == 200
    assert world['client'].get('/api/deliveries/stats').get_json()['total'] == 2
    assert world['client'].put(f"/api/deliveries/{world['deliveries'][1].id}",
                               json={'stage': 'packaged'}).status_code == 200
    response = world['client'].post('/api/sales', json=checkout(world,
        payment_method='debt', customer_id=world['customers'][0].id))
    assert response.status_code == 201, response.get_data(as_text=True)
    debt = world['m'].Debt.query.one()
    assert debt.amount == 100 and debt.balance == 100


@pytest.mark.parametrize('path', ['/api/branches', '/api/users', '/api/customers',
    '/api/suppliers', '/api/purchase_orders', '/api/warehouse', '/api/debts', '/api/logs'])
def test_manager_sections_remain_readable(world, path):
    login(world, role='manager')
    assert world['client'].get(path).status_code == 200


@pytest.mark.parametrize('index', [1, 2])
def test_other_sale_scoped_return_history_is_empty(world, index):
    response = world['client'].get('/api/returns_exchanges', query_string={
        'sale_transaction_id': world['sales'][index].transaction_id})
    assert response.status_code == 200
    assert response.get_json() == []


@pytest.mark.parametrize('path', ['/api/products/2', '/api/products/search?q=ForeignProduct'])
def test_foreign_catalog_object_cannot_be_read(world, path):
    response = world['client'].get(path)
    if path == '/api/products/2':
        assert response.status_code == 404
    else:
        assert response.status_code == 200
        assert 'ForeignProduct' not in response.get_data(as_text=True)


@pytest.mark.parametrize('path', ['/api/sales', '/api/returns_exchanges'])
def test_missing_branch_write_fails_closed(world, path):
    login(world, branch=None)
    before = state(world)
    assert world['client'].post(path, json=checkout(world)).status_code == 403
    assert state(world) == before


def test_cashier_checkout_creates_own_readable_delivery(world):
    response = world['client'].post('/api/sales', json=checkout(world,
        transaction_id='own-delivery-checkout', customer_id=world['customers'][0].id,
        delivery={'enabled': True, 'recipient_name': 'Own new recipient',
                  'recipient_phone': '123', 'delivery_address': 'Own address'}))
    assert response.status_code == 201, response.get_data(as_text=True)
    m = world['m']
    sale = m.Sale.query.filter_by(transaction_id='own-delivery-checkout').one()
    delivery = m.Delivery.query.filter_by(sale_id=sale.id).one()
    assert delivery.branch_id == world['branches'][0].id
    assert delivery.customer_id == world['customers'][0].id
    assert m.Debt.query.count() == 0
    for suffix in ('', '/print'):
        assert world['client'].get(f'/api/deliveries/{delivery.id}{suffix}').status_code == 200
    assert world['client'].get('/api/deliveries/stats').get_json()['total'] == 2
    assert world['client'].put(f'/api/deliveries/{delivery.id}',
                               json={'stage': 'packaged'}).status_code == 403
    m.db.session.expire_all()
    assert delivery.stage == 'to_deliver'


def test_cashier_local_exchange_succeeds_and_is_readable(world):
    response = world['client'].post('/api/returns_exchanges', json={
        'original_transaction_id': world['sales'][0].transaction_id,
        'return_items': [{'sale_item_id': world['items'][0].id, 'quantity': 1}],
        'exchange_items': [{'product_id': world['products'][0].id, 'quantity': 1, 'price': 100}],
        'settlement_method': 'cash'})
    assert response.status_code == 201, response.get_data(as_text=True)
    workflow_id = response.get_json()['workflow_id']
    for suffix in ('', '/print'):
        assert world['client'].get(f'/api/returns_exchanges/{workflow_id}{suffix}').status_code == 200
    world['m'].db.session.expire_all()
    assert world['products'][0].stock == 50
    assert world['products'][1].stock == 50

