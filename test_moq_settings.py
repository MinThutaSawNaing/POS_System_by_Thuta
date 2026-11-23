"""Settings MOQ rules: single/selected/all scopes, smart suggestions, auth.

Run with ``python run_tests.py test_moq_settings.py -q --tb=short``.
"""

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path
import re
import shutil
import subprocess
import sys
from unittest.mock import patch

from flask import Flask
import pytest


@pytest.fixture(scope='module')
def moq_app(tmp_path_factory):
    root = tmp_path_factory.mktemp('moq-settings')
    (root / 'instance').mkdir()
    shutil.copytree(Path(__file__).parent / 'templates', root / 'templates')
    original_init = Flask.__init__

    def isolated_init(application, *args, **kwargs):
        kwargs.update(instance_path=str(root / 'instance'), root_path=str(root))
        original_init(application, *args, **kwargs)

    name = '_isolated_moq_settings_app'
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name('app.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        with patch.object(Flask, '__init__', isolated_init):
            spec.loader.exec_module(module)
        module.app.config.update(TESTING=True)
        with module.app.app_context():
            module.db.drop_all()
            module.db.create_all()
        yield module
    finally:
        if hasattr(module, 'db'):
            with module.app.app_context():
                module.db.session.remove()
                module.db.engine.dispose()
        sys.modules.pop(name, None)


@pytest.fixture
def world(moq_app):
    m = moq_app
    with m.app.app_context():
        m.db.session.remove()
        m.db.drop_all()
        m.db.create_all()
        alpha = m.Branch(name='Alpha', code='MOQ-A', is_active=True, is_default=True)
        beta = m.Branch(name='Beta', code='MOQ-B', is_active=True)
        m.db.session.add_all([alpha, beta])
        m.db.session.flush()
        manager = m.User(username='manager', role='manager', branch_id=alpha.id, password='x')
        boss = m.User(username='boss', role='boss', branch_id=alpha.id, password='x')
        cashier = m.User(username='cashier', role='cashier', branch_id=alpha.id, password='x')
        m.db.session.add_all([manager, boss, cashier])
        m.db.session.flush()
        products = {
            'bread': m.Product(name='Bread', sku='BREAD-1', barcode='B1', price=3, stock=50,
                               reorder_point=10, reorder_quantity=20, reorder_enabled=True,
                               branch_id=alpha.id),
            'coffee': m.Product(name='Coffee', sku='COFFEE-1', barcode='K1', price=5, stock=2,
                                reorder_point=5, reorder_quantity=6, reorder_enabled=True,
                                branch_id=alpha.id),
            'cola': m.Product(name='Cola', sku='COLA-1', barcode='C1', price=2, stock=5,
                              reorder_point=10, reorder_quantity=40, reorder_enabled=True,
                              branch_id=alpha.id),
            'soap': m.Product(name='Soap', sku='SOAP-1', barcode='S1', price=1, stock=1,
                              reorder_point=0, reorder_quantity=0, reorder_enabled=False,
                              branch_id=alpha.id),
            'juice': m.Product(name='Juice', sku='JUICE-1', barcode='J1', price=4, stock=1,
                               reorder_point=10, reorder_quantity=5, reorder_enabled=True,
                               branch_id=beta.id),
        }
        m.db.session.add_all(products.values())
        m.db.session.flush()
        sale_counter = {'value': 0}

        def sale(product, quantity, days_ago, branch_id):
            sale_counter['value'] += 1
            row = m.Sale(transaction_id=f'MOQ-SALE-{sale_counter["value"]}',
                         date=datetime.utcnow() - timedelta(days=days_ago),
                         total=quantity * 2, tax=0, payment_method='cash',
                         user_id=manager.id, branch_id=branch_id)
            m.db.session.add(row)
            m.db.session.flush()
            m.db.session.add(m.SaleItem(sale_id=row.id, product_id=product.id,
                                        quantity=quantity, price=2, tax=0))

        sale(products['cola'], 30, 5, alpha.id)       # inside 30-day window
        sale(products['bread'], 10, 10, alpha.id)     # inside 30-day window
        sale(products['cola'], 100, 59, alpha.id)     # outside 30-day, inside 60-day
        sale(products['juice'], 50, 5, beta.id)       # other branch
        m.db.session.commit()
        ids = {'branch': alpha.id, 'other_branch': beta.id,
               'manager': manager.id, 'boss': boss.id, 'cashier': cashier.id,
               'bread': products['bread'].id, 'coffee': products['coffee'].id,
               'cola': products['cola'].id, 'soap': products['soap'].id,
               'juice': products['juice'].id}

    class World:
        pass

    w = World()
    w.m, w.app, w.ids = m, m.app, ids
    yield w
    with m.app.app_context():
        m.db.session.remove()
        m.db.drop_all()


def client_for(m, user_id, role, branch_id):
    client = m.app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=user_id, username=role, role=role, branch_id=branch_id)
    return client


def manager(world):
    return client_for(world.m, world.ids['manager'], 'manager', world.ids['branch'])


def test_product_list_reports_summary_filters_and_smart_suggestions(world):
    client = manager(world)
    data = client.get('/api/settings/moq/products').get_json()
    assert data['success'] is True
    assert [item['name'] for item in data['items']] == ['Bread', 'Coffee', 'Cola', 'Soap']
    assert data['summary'] == {'total_products': 4, 'low_count': 2, 'unset_count': 1,
                               'lookback_days': 30, 'cover_days': 7}
    assert (data['total'], data['page'], data['total_pages']) == (4, 1, 1)
    by_name = {item['name']: item for item in data['items']}
    assert (by_name['Cola']['sold'], by_name['Cola']['suggested_moq']) == (30, 7)
    assert (by_name['Bread']['sold'], by_name['Bread']['suggested_moq']) == (10, 3)
    assert (by_name['Coffee']['sold'], by_name['Coffee']['suggested_moq']) == (0, None)
    assert (by_name['Soap']['suggested_moq'], by_name['Soap']['reminders_enabled']) == (None, False)

    low = client.get('/api/settings/moq/products?filter=low').get_json()
    assert {item['name'] for item in low['items']} == {'Cola', 'Coffee'}
    unset = client.get('/api/settings/moq/products?filter=unset').get_json()
    assert [item['name'] for item in unset['items']] == ['Soap']
    disabled = client.get('/api/settings/moq/products?filter=disabled').get_json()
    assert [item['name'] for item in disabled['items']] == ['Soap']
    searched = client.get('/api/settings/moq/products?q=Col').get_json()
    assert [item['name'] for item in searched['items']] == ['Cola']

    single = client.get(
        f"/api/settings/moq/products?product_id={world.ids['cola']}").get_json()
    assert [item['name'] for item in single['items']] == ['Cola']
    assert single['items'][0]['suggested_moq'] == 7
    assert single['total'] == 1

    cover = client.get('/api/settings/moq/products?cover_days=14').get_json()
    cover_by = {item['name']: item for item in cover['items']}
    assert (cover_by['Cola']['suggested_moq'], cover_by['Bread']['suggested_moq']) == (14, 5)

    window = client.get('/api/settings/moq/products?lookback_days=60').get_json()
    window_by = {item['name']: item for item in window['items']}
    # The 60-day window adds the 100-unit sale; 130 × 7 ÷ 60 = 15.17 → 16.
    assert (window_by['Cola']['sold'], window_by['Cola']['suggested_moq']) == (130, 16)
    assert window_by['Bread']['suggested_moq'] == 2


def test_product_list_is_manager_or_boss_only(world):
    cashier = client_for(world.m, world.ids['cashier'], 'cashier', world.ids['branch'])
    assert cashier.get('/api/settings/moq/products').status_code == 403
    assert cashier.get('/api/settings/moq/products?product_id=1').status_code == 403
    boss = client_for(world.m, world.ids['boss'], 'boss', world.ids['branch'])
    assert boss.get('/api/settings/moq/products').status_code == 200


def test_manual_selected_products_update_only_those_rows(world):
    m = world.m
    client = manager(world)
    response = client.post('/api/settings/moq', json={
        'scope': 'ids', 'mode': 'manual',
        'product_ids': [world.ids['cola'], world.ids['bread']],
        'moq': 3, 'reorder_quantity': 12, 'reminders_enabled': False,
        'branch_id': world.ids['branch'],
    })
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()['updated'] == 2
    with m.app.app_context():
        cola = m.db.session.get(m.Product, world.ids['cola'])
        bread = m.db.session.get(m.Product, world.ids['bread'])
        coffee = m.db.session.get(m.Product, world.ids['coffee'])
        assert (cola.reorder_point, cola.reorder_quantity, cola.reorder_enabled) == (3, 12, False)
        assert (bread.reorder_point, bread.reorder_quantity, bread.reorder_enabled) == (3, 12, False)
        # Unselected rows and optional fields stay untouched.
        assert (coffee.reorder_point, coffee.reorder_quantity, coffee.reorder_enabled) == (5, 6, True)

    kept = client.post('/api/settings/moq', json={
        'scope': 'ids', 'mode': 'manual',
        'product_ids': [world.ids['coffee']], 'moq': 2,
    })
    assert kept.status_code == 200
    with m.app.app_context():
        coffee = m.db.session.get(m.Product, world.ids['coffee'])
        assert (coffee.reorder_point, coffee.reorder_quantity, coffee.reorder_enabled) == (2, 6, True)


def test_manual_all_scope_and_only_unset(world):
    m = world.m
    client = manager(world)
    only_unset = client.post('/api/settings/moq', json={
        'scope': 'all', 'mode': 'manual', 'moq': 4, 'only_unset': True,
        'branch_id': world.ids['branch'],
    })
    assert only_unset.status_code == 200
    assert only_unset.get_json()['updated'] == 1
    with m.app.app_context():
        assert m.db.session.get(m.Product, world.ids['soap']).reorder_point == 4
        assert m.db.session.get(m.Product, world.ids['cola']).reorder_point == 10

    every = client.post('/api/settings/moq', json={
        'scope': 'all', 'mode': 'manual', 'moq': 7,
        'branch_id': world.ids['branch'],
    })
    assert every.status_code == 200
    assert every.get_json()['updated'] == 4
    with m.app.app_context():
        for key in ('bread', 'coffee', 'cola', 'soap'):
            assert m.db.session.get(m.Product, world.ids[key]).reorder_point == 7
        # The other branch is never touched by an all-scope update.
        assert m.db.session.get(m.Product, world.ids['juice']).reorder_point == 10

    nothing = client.post('/api/settings/moq', json={
        'scope': 'all', 'mode': 'manual', 'moq': 9, 'only_unset': True,
    })
    assert nothing.get_json()['updated'] == 0


def test_smart_suggestions_use_sales_velocity_and_skip_unsold(world):
    m = world.m
    client = manager(world)
    response = client.post('/api/settings/moq', json={
        'scope': 'ids', 'mode': 'suggestions',
        'product_ids': [world.ids['cola'], world.ids['bread'],
                        world.ids['coffee'], world.ids['soap']],
        'cover_days': 7, 'lookback_days': 30,
    })
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert (body['updated'], body['skipped']) == (2, 2)
    with m.app.app_context():
        assert m.db.session.get(m.Product, world.ids['cola']).reorder_point == 7
        assert m.db.session.get(m.Product, world.ids['bread']).reorder_point == 3
        # No sales inside the window means no suggestion, not a zero MOQ.
        assert m.db.session.get(m.Product, world.ids['coffee']).reorder_point == 5
        assert m.db.session.get(m.Product, world.ids['soap']).reorder_point == 0

    longer_cover = client.post('/api/settings/moq', json={
        'scope': 'ids', 'mode': 'suggestions',
        'product_ids': [world.ids['cola'], world.ids['bread']],
        'cover_days': 14, 'lookback_days': 30,
    })
    assert longer_cover.get_json()['updated'] == 2
    with m.app.app_context():
        assert m.db.session.get(m.Product, world.ids['cola']).reorder_point == 14
        assert m.db.session.get(m.Product, world.ids['bread']).reorder_point == 5

    wider_window = client.post('/api/settings/moq', json={
        'scope': 'ids', 'mode': 'suggestions',
        'product_ids': [world.ids['cola'], world.ids['bread']],
        'cover_days': 7, 'lookback_days': 60,
    })
    assert wider_window.get_json()['updated'] == 2
    with m.app.app_context():
        assert m.db.session.get(m.Product, world.ids['cola']).reorder_point == 16
        assert m.db.session.get(m.Product, world.ids['bread']).reorder_point == 2

    unset_only = client.post('/api/settings/moq', json={
        'scope': 'all', 'mode': 'suggestions', 'only_unset': True,
        'cover_days': 7, 'lookback_days': 30,
    })
    assert (unset_only.get_json()['updated'], unset_only.get_json()['skipped']) == (0, 1)


def test_validation_and_branch_guards(world):
    m = world.m
    client = manager(world)

    def post(payload):
        return client.post('/api/settings/moq', json=payload)

    assert post({'scope': 'ids', 'mode': 'manual', 'moq': 5}).status_code == 400
    assert post({'scope': 'nope', 'mode': 'manual', 'moq': 5}).status_code == 400
    assert post({'scope': 'ids', 'mode': 'manual',
                 'product_ids': [world.ids['cola']]}).status_code == 400
    assert post({'scope': 'ids', 'mode': 'manual', 'product_ids': [world.ids['cola']],
                 'moq': -1}).status_code == 400
    assert post({'scope': 'ids', 'mode': 'manual', 'product_ids': [world.ids['cola']],
                 'moq': 1.5}).status_code == 400
    assert post({'scope': 'ids', 'mode': 'manual', 'product_ids': list(range(1, 503)),
                 'moq': 5}).status_code == 400
    assert post({'scope': 'ids', 'mode': 'manual', 'product_ids': [world.ids['cola']],
                 'moq': 5, 'branch_id': world.ids['other_branch']}).status_code == 409
    assert post({'scope': 'ids', 'mode': 'manual', 'product_ids': [world.ids['cola']],
                 'moq': 5, 'reorder_quantity': 'x'}).status_code == 400
    assert post({'scope': 'ids', 'mode': 'manual', 'product_ids': [world.ids['cola']],
                 'moq': 5, 'reminders_enabled': 'maybe'}).status_code == 400

    other_branch = post({'scope': 'ids', 'mode': 'manual',
                         'product_ids': [world.ids['juice']], 'moq': 5})
    assert other_branch.status_code == 200
    assert other_branch.get_json()['updated'] == 0
    with m.app.app_context():
        assert m.db.session.get(m.Product, world.ids['juice']).reorder_point == 10

    cashier = client_for(world.m, world.ids['cashier'], 'cashier', world.ids['branch'])
    assert cashier.post('/api/settings/moq', json={
        'scope': 'all', 'mode': 'manual', 'moq': 3}).status_code == 403


def test_settings_template_and_javascript_contract():
    source = (Path(__file__).parent / 'templates' / 'dashboard.html').read_text(
        encoding='utf-8')
    for snippet in (
            'data-group="inventory"', 'id="settings-group-inventory"',
            'id="moq-scope-single"', 'id="moq-scope-multiple"', 'id="moq-scope-all"',
            'id="moq-single-product"', 'id="moq-products-table"',
            'id="moq-all-only-unset"', 'id="moq-value"', 'id="moq-cover-days"',
            'id="moq-apply-btn"', 'id="moq-suggest-btn"',
            'function applyMoqSettings', 'function loadMoqProducts',
            '/api/settings/moq'):
        assert snippet in source, snippet

    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required to execute dashboard JavaScript')
    names = ('currentMoqScope', 'moqCoverDays', 'moqLookbackDays', 'setMoqStatus',
             'updateMoqScopePanels', 'updateMoqSummaries', 'syncMoqSelectionUi',
             'toggleMoqSelection', 'toggleMoqPageSelection', 'clearMoqSelection',
             'loadMoqProducts', 'renderMoqProducts', 'renderMoqPager',
             'loadMoqSingleProduct', 'initMoqSettingsUi', 'loadMoqSettings',
             'refreshMoqSettings', 'applyMoqSettings')
    functions = []
    for name in names:
        match = re.search(rf'(?:async\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{',
                          source)
        assert match, f'JavaScript function {name} was not found'
        depth = 0
        for index in range(match.start(), len(source)):
            if source[index] == '{':
                depth += 1
            elif source[index] == '}':
                depth -= 1
                if depth == 0:
                    functions.append(source[match.start():index + 1])
                    break
        else:
            raise AssertionError(f'JavaScript function {name} has an unbalanced body')
    script = '\n'.join(functions) + '\nconsole.log("moq-js-ok");\n'
    result = subprocess.run([node, '-e', script], check=True, capture_output=True,
                            text=True)
    assert result.stdout.strip() == 'moq-js-ok'
