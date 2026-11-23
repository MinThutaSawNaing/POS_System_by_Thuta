"""Per-item MOQ reminders: payload, product fields, and the daily email.

Run with ``python run_tests.py test_inventory_reminders.py -q --tb=short``.
The app is imported from a disposable root and the SMTP boundary is faked, so
nothing can touch workspace data or leave the machine.
"""

import importlib.util
from datetime import datetime
from pathlib import Path
import re
import shutil
import subprocess
import sys
from unittest.mock import patch

from flask import Flask
import pytest

import email_auth


MANAGER_EMAIL = 'manager@example.com'
BOSS_EMAIL = 'boss@example.com'
CASHIER_EMAIL = 'cashier@example.com'
OTHER_EMAIL = 'other@example.com'
API_KEY = 're_inventory_reminder_secret'
FROM_EMAIL = 'POS Reminders <reminders@example.com>'


@pytest.fixture(scope='module')
def reminder_app(tmp_path_factory):
    root = tmp_path_factory.mktemp('inventory-reminders')
    (root / 'instance').mkdir()
    shutil.copytree(Path(__file__).parent / 'templates', root / 'templates')
    original_init = Flask.__init__

    def isolated_init(application, *args, **kwargs):
        kwargs.update(instance_path=str(root / 'instance'), root_path=str(root))
        original_init(application, *args, **kwargs)

    name = '_isolated_inventory_reminder_app'
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name('app.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        with patch.object(Flask, '__init__', isolated_init):
            spec.loader.exec_module(module)
        module.app.config.update(TESTING=True, SECRET_KEY='isolated-inventory-reminder-only')
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
def world(reminder_app, monkeypatch):
    """Fresh branches/products plus a fake mail transport that records sends."""
    m = reminder_app
    sent = []

    def fake_send_mail(get_setting, secret_key, to, subject, text, html=None,
                       attachments=(), reply_to=None):
        recipients = list(to) if isinstance(to, (list, tuple)) else [to]
        sent.append({'to': recipients, 'subject': subject, 'text': text,
                     'html': html, 'attachments': list(attachments)})
        return {'to': recipients, 'subject': subject, 'attachments': []}

    monkeypatch.setattr(email_auth, 'send_mail', fake_send_mail)

    with m.app.app_context():
        m.db.session.remove()
        m.db.drop_all()
        m.db.create_all()
        branch = m.Branch(name='Alpha', code='MOQ-A', is_active=True, is_default=True)
        other = m.Branch(name='Beta', code='MOQ-B', is_active=True)
        m.db.session.add_all([branch, other])
        m.db.session.flush()
        manager = m.User(username='manager', role='manager', branch_id=branch.id,
                         password='x', email=MANAGER_EMAIL)
        boss = m.User(username='boss', role='boss', branch_id=branch.id,
                      password='x', email=BOSS_EMAIL)
        cashier = m.User(username='cashier', role='cashier', branch_id=branch.id,
                         password='x', email=CASHIER_EMAIL)
        other_manager = m.User(username='manager-b', role='manager', branch_id=other.id,
                               password='x', email=OTHER_EMAIL)
        m.db.session.add_all([manager, boss, cashier, other_manager])
        m.db.session.flush()
        m.db.session.add_all([
            m.Product(name='Cola', sku='COLA-1', barcode='C1', price=2, cost=1, stock=3,
                      reorder_point=3, reorder_quantity=12, reorder_enabled=True,
                      branch_id=branch.id, category='Drinks'),
            m.Product(name='Bread', sku='BREAD-1', barcode='B1', price=3, cost=1.5, stock=4,
                      reorder_point=3, reorder_quantity=20, reorder_enabled=True,
                      branch_id=branch.id, category='Bakery'),
            m.Product(name='Milk', sku='MILK-1', barcode='M1', price=1, cost=0.5, stock=0,
                      reorder_point=10, reorder_quantity=0, reorder_enabled=True,
                      branch_id=branch.id, category='Dairy'),
            m.Product(name='Soap', sku='SOAP-1', barcode='S1', price=1, cost=0.4, stock=1,
                      reorder_point=10, reorder_quantity=5, reorder_enabled=False,
                      branch_id=branch.id, category='Home'),
            m.Product(name='Coffee', sku='COFFEE-1', barcode='K1', price=5, cost=2, stock=1,
                      reorder_point=5, reorder_quantity=6, reorder_enabled=True,
                      branch_id=other.id, category='Drinks'),
        ])
        for key, value in (('resend_api_key', API_KEY), ('email_from', FROM_EMAIL),
                           ('email_smtp_host', 'smtp.resend.com'),
                           ('email_smtp_port', '465'),
                           ('email_smtp_username', 'resend')):
            m.set_setting(key, value)
        m.db.session.commit()
        ids = {'branch': branch.id, 'other_branch': other.id,
               'manager': manager.id, 'boss': boss.id, 'cashier': cashier.id,
               'other_manager': other_manager.id}

    class World:
        pass

    w = World()
    w.m, w.app, w.ids, w.sent = m, m.app, ids, sent
    w.service = m.inventory_reminders
    yield w
    with m.app.app_context():
        m.db.session.remove()
        m.db.drop_all()


def client_for(m, user_id, role, branch_id):
    """A signed-in test client whose email-auth fingerprint matches the user."""
    client = m.app.test_client()
    with m.app.app_context():
        user = m.db.session.get(m.User, user_id)
        fingerprint = m.email_auth.user_fingerprint(user)
    with client.session_transaction() as session:
        session.update(user_id=user_id, username=role, role=role, branch_id=branch_id,
                       email_auth_fingerprint=fingerprint)
    return client


def test_payload_lists_only_enabled_items_at_or_below_their_own_moq(world):
    with world.app.app_context():
        payload = world.service.payload(world.ids['branch'])
    items = payload['items']
    assert [item['name'] for item in items] == ['Cola', 'Milk']
    cola, milk = items
    assert (cola['current_stock'], cola['moq'], cola['reorder_point'],
            cola['suggested_qty']) == (3, 3, 3, 12)
    # A zero order quantity falls back to topping the item back up to its MOQ.
    assert (milk['current_stock'], milk['moq'], milk['suggested_qty']) == (0, 10, 10)
    assert payload['summary'] == {'total_products': 4, 'low_stock_count': 2,
                                  'out_of_stock_count': 1}
    assert payload['email'] == {'enabled': True, 'ready': True}


def test_reminder_endpoint_is_manager_or_boss_only(world):
    branch = world.ids['branch']
    manager = client_for(world.m, world.ids['manager'], 'manager', branch)
    response = manager.get('/api/inventory/reminders')
    assert response.status_code == 200
    body = response.get_json()
    assert [item['name'] for item in body['items']] == ['Cola', 'Milk']
    # Only readiness flags cross the API; never SMTP secrets.
    assert body['email'] == {'enabled': True, 'ready': True}

    cashier = client_for(world.m, world.ids['cashier'], 'cashier', branch)
    assert cashier.get('/api/inventory/reminders').status_code == 403

    boss = client_for(world.m, world.ids['boss'], 'boss', branch)
    assert boss.get('/api/inventory/reminders').status_code == 200


def test_moq_fields_round_trip_through_the_product_api(world):
    branch = world.ids['branch']
    manager = client_for(world.m, world.ids['manager'], 'manager', branch)
    created = manager.post('/api/products', json={
        'name': 'MOQ Widget', 'price': 5, 'stock': 0,
        'reorder_point': 7, 'reorder_quantity': 21, 'reorder_enabled': False,
    })
    assert created.status_code == 201, created.get_data(as_text=True)
    with world.app.app_context():
        product = world.m.Product.query.filter_by(name='MOQ Widget',
                                                  branch_id=branch).one()
        product_id = product.id
    fetched = manager.get(f'/api/products/{product_id}').get_json()
    assert (fetched['reorder_point'], fetched['reorder_quantity'],
            fetched['reorder_enabled']) == (7, 21, False)

    updated = manager.put(f'/api/products/{product_id}', json={
        'reorder_point': 9, 'reorder_quantity': 0, 'reorder_enabled': True,
    })
    assert updated.status_code == 200, updated.get_data(as_text=True)
    fetched = manager.get(f'/api/products/{product_id}').get_json()
    assert (fetched['reorder_point'], fetched['reorder_quantity'],
            fetched['reorder_enabled']) == (9, 0, True)


def test_daily_email_goes_to_branch_managers_and_boss_once_per_day(world):
    today = datetime(2026, 3, 2, 9, 0)
    with world.app.app_context():
        results = world.service.run(now=today)
    assert [result['status'] for result in results] == ['sent', 'sent']
    assert len(world.sent) == 2
    alpha, beta = world.sent
    assert alpha['to'] == [BOSS_EMAIL, MANAGER_EMAIL]
    assert beta['to'] == [BOSS_EMAIL, OTHER_EMAIL]
    assert CASHIER_EMAIL not in alpha['to']
    assert alpha['subject'] == 'Low stock: 2 item(s) at or below MOQ — Alpha'
    assert 'Cola' in alpha['text'] and 'Milk' in alpha['text']
    assert 'Bread' not in alpha['text'] and 'Soap' not in alpha['text']
    assert 'MOQ 3, suggested order 12' in alpha['text']

    # A second tick (or a restart) the same day must not repeat a reminder.
    with world.app.app_context():
        assert world.service.run(now=datetime(2026, 3, 2, 17, 0)) == []
    assert len(world.sent) == 2

    # The next day reminds again while the items are still low.
    with world.app.app_context():
        assert len(world.service.run(now=datetime(2026, 3, 3, 9, 0))) == 2
    assert len(world.sent) == 4


def test_email_reminders_can_be_switched_off(world):
    with world.app.app_context():
        world.m.set_setting('inventory_reminder_email', '0')
        results = world.service.run(now=datetime(2026, 3, 2, 9, 0))
        payload = world.service.payload(world.ids['branch'])
    assert results == []
    assert world.sent == []
    assert payload['email']['enabled'] is False
    # The dashboard list is independent of the email switch.
    assert payload['summary']['low_stock_count'] == 2


def test_legacy_alert_payload_keeps_its_documented_shape(world):
    manager = client_for(world.m, world.ids['manager'], 'manager', world.ids['branch'])
    data = manager.get('/api/inventory/alerts').get_json()
    assert set(data) == {'summary', 'low_stock_items', 'suggested_purchase_order'}
    assert data['summary'] == {'total_products': 4, 'low_stock_count': 2,
                               'out_of_stock_count': 1}
    assert set(data['low_stock_items'][0]) == {
        'product_id', 'name', 'barcode', 'sku', 'category', 'current_stock',
        'reorder_point', 'reorder_quantity', 'suggested_qty'}
    assert [row['suggested_qty'] for row in
            data['suggested_purchase_order']['items']] == [12, 10]


def test_missing_smtp_settings_leave_reminders_pending(world):
    with world.app.app_context():
        world.m.set_setting('resend_api_key', '')
        assert world.service.run(now=datetime(2026, 3, 2, 9, 0)) == []
        assert world.service.mail_status()['ready'] is False
        # Nothing is marked as sent, so the next tick after SMTP is configured
        # still delivers today's reminder.
        assert world.service.models['InventoryReminderLog'].query.count() == 0
    assert world.sent == []


def test_settings_switch_stops_and_starts_reminder_emails(world):
    manager = client_for(world.m, world.ids['manager'], 'manager', world.ids['branch'])
    before = manager.get('/api/settings/inventory-reminders')
    assert before.status_code == 200
    assert before.get_json() == {'success': True, 'enabled': True, 'mail_ready': True,
                                 'account_email': MANAGER_EMAIL}

    off = manager.put('/api/settings/inventory-reminders', json={'enabled': False})
    assert off.status_code == 200
    assert off.get_json()['enabled'] is False
    with world.app.app_context():
        assert world.service.run(now=datetime(2026, 3, 2, 9, 0)) == []
    assert world.sent == []

    invalid = manager.put('/api/settings/inventory-reminders', json={'enabled': 'maybe'})
    assert invalid.status_code == 400

    on = manager.put('/api/settings/inventory-reminders', json={'enabled': True})
    assert on.status_code == 200
    assert on.get_json()['enabled'] is True
    with world.app.app_context():
        assert len(world.service.run(now=datetime(2026, 3, 2, 9, 0))) == 2
    assert len(world.sent) == 2

    cashier = client_for(world.m, world.ids['cashier'], 'cashier', world.ids['branch'])
    assert cashier.get('/api/settings/inventory-reminders').status_code == 403
    assert cashier.put('/api/settings/inventory-reminders',
                       json={'enabled': False}).status_code == 403


def test_test_send_emails_the_real_template_to_one_address(world):
    manager = client_for(world.m, world.ids['manager'], 'manager', world.ids['branch'])
    response = manager.post('/api/settings/inventory-reminders/test',
                            json={'email': 'owner@example.com'})
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert body['success'] is True
    assert (body['items'], body['recipient']) == (2, 'owner@example.com')
    assert len(world.sent) == 1
    mail = world.sent[0]
    assert mail['to'] == ['owner@example.com']
    assert 'Cola' in mail['text'] and 'Milk' in mail['text']
    # A test never consumes the scheduled reminder for the day.
    with world.app.app_context():
        assert world.service.models['InventoryReminderLog'].query.count() == 0
        assert len(world.service.run(now=datetime(2026, 3, 2, 9, 0))) == 2

    assert manager.post('/api/settings/inventory-reminders/test',
                        json={'email': 'not-an-email'}).status_code == 400
    manager.put('/api/settings/inventory-reminders', json={'enabled': False})
    refused = manager.post('/api/settings/inventory-reminders/test',
                           json={'email': 'owner@example.com'})
    assert refused.status_code == 400
    assert 'switched off' in refused.get_json()['message'].lower()


def test_dashboard_exposes_moq_controls_and_the_reminder_panel():
    html = (Path(__file__).parent / 'templates' / 'dashboard.html').read_text(
        encoding='utf-8')
    for snippet in (
            'id="add-moq"', 'id="edit-moq"', 'id="add-reorder-qty"',
            'id="edit-reorder-qty"', 'id="add-reorder-enabled"',
            'id="edit-reorder-enabled"', 'id="inventory-reminder-card"',
            'id="inventory-reminder-table"', '/api/inventory/reminders',
            'function loadInventoryReminders', 'function dismissInventoryReminders',
            'id="inventory-reminder-email-on"', 'id="inventory-reminder-email-off"',
            'id="save-inventory-reminder-settings-btn"',
            'id="inventory-reminder-test-email"',
            'function saveInventoryReminderSettings', 'function sendInventoryReminderTest',
            '/api/settings/inventory-reminders',
            'productData.append("reorder_point"'):
        assert snippet in html, snippet


def test_reminder_javascript_parses_and_fingerprints_stock_changes():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required to execute dashboard JavaScript')
    source = (Path(__file__).parent / 'templates' / 'dashboard.html').read_text(
        encoding='utf-8')

    def function(name):
        match = re.search(rf'function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{', source)
        assert match, f'JavaScript function {name} was not found'
        depth = 0
        for index in range(match.end() - 1, len(source)):
            if source[index] == '{':
                depth += 1
            elif source[index] == '}':
                depth -= 1
                if depth == 0:
                    return source[match.start():index + 1]
        raise AssertionError(f'JavaScript function {name} has an unbalanced body')

    script = '\n'.join(function(name) for name in (
        'inventoryReminderFingerprint', 'inventoryReminderDismissedFingerprint',
        'dismissInventoryReminders', 'renderInventoryReminders',
        'loadInventoryReminders', 'setInventoryReminderSettingsStatus',
        'loadInventoryReminderSettings', 'saveInventoryReminderSettings',
        'sendInventoryReminderTest')) + '''
console.log(inventoryReminderFingerprint([
  {product_id: 1, current_stock: 2},
  {product_id: 2, current_stock: 0}
]));
'''
    result = subprocess.run([node, '-e', script], check=True, capture_output=True,
                            text=True)
    assert result.stdout.strip() == '1:2|2:0'
