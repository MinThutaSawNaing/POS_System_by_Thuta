"""Checkout recovery contract; run: python run_tests.py test_checkout_recovery.py -q.

No inherited test cases, production database, mocked checkout, or in-memory SQLite.
Every request uses the existing POST /api/sales route. All four branches share one
private file-backed database; threaded requests have independent clients/sessions.
The CheckoutRequest model is intentionally required, not skipped when absent.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask
import pytest
from sqlalchemy import event, inspect, text


OCCURRED_AT = '2026-01-15T09:30:00+06:30'


@pytest.fixture(scope='module')
def backend():
    """Import our own app instance, with *all* filesystem writes redirected."""
    with tempfile.TemporaryDirectory(prefix='pos-checkout-recovery-') as directory:
        root = Path(directory)
        (root / 'instance').mkdir()
        shutil.copytree(Path(__file__).parent / 'templates', root / 'templates')
        original_init = Flask.__init__

        def isolated_init(app, *args, **kwargs):
            kwargs.update(instance_path=str(root / 'instance'), root_path=str(root))
            original_init(app, *args, **kwargs)

        name = '_isolated_checkout_recovery_app'
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name('app.py'))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            with patch.object(Flask, '__init__', isolated_init):
                spec.loader.exec_module(module)
            module.app.config.update(TESTING=True)
            module._recovery_password_hash = module.generate_password_hash('test-only')
            with module.app.app_context():
                database = Path(module.db.engine.url.database).resolve()
                assert database.is_relative_to(root.resolve())
                assert module.db.engine.dialect.name == 'sqlite'
            yield module
        finally:
            if hasattr(module, 'db'):
                with module.app.app_context():
                    module.db.session.remove()
                    module.db.engine.dispose()
            sys.modules.pop(name, None)


@pytest.fixture
def world(backend):
    """Reset only this module's private database, never the normal imported app."""
    m = backend
    with m.app.app_context():
        m.db.session.remove()
        m.db.drop_all()
        m.db.create_all()
        records = {}
        for code in 'ABCD':
            branch = m.Branch(name=f'Recovery {code}', code=code,
                              is_active=True, is_default=code == 'A')
            m.db.session.add(branch)
            m.db.session.flush()
            user = m.User(username=f'recovery-{code}', role='manager', branch_id=branch.id,
                          password=m._recovery_password_hash)
            product = m.Product(name=f'Product {code}', barcode='SAME-PHYSICAL-BARCODE',
                                price=100, cost=20, stock=40, tax_rate=5, branch_id=branch.id)
            customer = m.Customer(name=f'Customer {code}', branch_id=branch.id)
            m.db.session.add_all([user, product, customer])
            m.db.session.flush()
            records[code] = SimpleNamespace(branch=branch.id, user=user.id,
                                            product=product.id, customer=customer.id)
        other = m.User(username='other-owner', role='manager', branch_id=records['A'].branch,
                       password=m._recovery_password_hash)
        m.db.session.add(other)
        m.db.session.commit()
        other_id = other.id
    result = SimpleNamespace(m=m, records=records, other_user=other_id)
    yield result
    with m.app.app_context():
        m.db.session.rollback()
        m.db.session.remove()


def client(w, code='A', user_id=None):
    record = w.records[code]
    result = w.m.app.test_client()
    with result.session_transaction() as session:
        session.update(user_id=user_id or record.user, username=f'recovery-{code}',
                       role='manager', branch_id=record.branch)
    return result


def payload(w, code='A', txn='recovery-transaction', *, offline=False, context=True, quantity=2):
    record = w.records[code]
    result = {
        'transaction_id': txn,
        'items': [{'product_id': record.product, 'quantity': quantity, 'price': 100, 'tax_rate': 5}],
        'payment_method': 'cash', 'cash_received': max(1000, quantity * 105),
    }
    if context:
        result['checkout_context'] = {
            'branch_id': record.branch, 'user_id': record.user,
            'device_id': f'recovery-device-{code}', 'occurred_at': OCCURRED_AT,
            'offline': offline, 'expected_total': quantity * 105,
        }
    return result


def post(w, data, code='A', user_id=None):
    return client(w, code, user_id).post('/api/sales', json=data)


def ledger_model(m):
    model = getattr(m, 'CheckoutRequest', None)
    assert model is not None, 'Contract requires a durable CheckoutRequest model/table'
    return model


def state(w):
    m = w.m
    with m.app.app_context():
        m.db.session.remove()
        ledger = getattr(m, 'CheckoutRequest', None)
        return {
            'stocks': {code: m.db.session.get(m.Product, r.product).stock
                       for code, r in w.records.items()},
            'sales': m.Sale.query.count(), 'items': m.SaleItem.query.count(),
            'debts': m.Debt.query.count(), 'deliveries': m.Delivery.query.count(),
            'ledger': ledger.query.count() if ledger is not None else None,
            'audit': m.AuditLog.query.count(),
        }


def assert_accepted(response):
    assert response.status_code == 201, response.get_json()
    assert response.get_json()['success'] is True


def assert_replay(response, original):
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body['success'] is True
    # Implementations may add a replay marker/message; business data must be the
    # saved response, never reconstructed from today's mutable catalog/customer.
    for key, value in original.items():
        if key not in {'message', 'duplicate'}:
            assert body[key] == value, (key, body)


@pytest.mark.parametrize('context,offline', [(False, False), (True, False), (True, True)])
def test_every_client_transaction_has_complete_durable_ledger(world, context, offline):
    w, m = world, world.m
    data = payload(w, context=context, offline=offline)
    response = post(w, data)
    assert_accepted(response)
    with m.app.app_context():
        Ledger = ledger_model(m)
        row = Ledger.query.filter_by(transaction_id=data['transaction_id']).one()
        assert row.user_id == w.records['A'].user
        assert row.branch_id == w.records['A'].branch
        assert isinstance(row.fingerprint, str) and row.fingerprint.strip()
        assert row.accepted_at is not None
        saved = json.loads(row.response_json) if isinstance(row.response_json, str) else row.response_json
        assert saved == response.get_json()
        assert bool(row.offline) == offline
        if context:
            assert row.device_id == data['checkout_context']['device_id']
            occurred = row.occurred_at
            if isinstance(occurred, str):
                occurred = datetime.fromisoformat(occurred.replace('Z', '+00:00'))
            # A DB may preserve local wall time or normalize it to naive UTC.
            expected = datetime.fromisoformat(OCCURRED_AT)
            assert occurred in (expected, expected.replace(tzinfo=None),
                                expected.astimezone(timezone.utc).replace(tzinfo=None))
        columns = {column['name'] for column in inspect(m.db.engine).get_columns(Ledger.__tablename__)}
        assert {'transaction_id', 'fingerprint', 'user_id', 'branch_id', 'occurred_at',
                'device_id', 'offline', 'response_json', 'accepted_at'} <= columns
        assert not any(fk['referred_table'] == m.Sale.__tablename__
                       for fk in inspect(m.db.engine).get_foreign_keys(Ledger.__tablename__))
    before = state(w)
    assert_replay(post(w, data), response.get_json())
    assert state(w) == before


@pytest.mark.parametrize('context,change', [
    (context, change) for context in (False, True)
    for change in ('quantity', 'price', 'cash', 'customer', 'payment', 'device', 'total')
    if context or change not in {'device', 'total'}])
def test_same_id_different_payload_conflicts_without_writes(world, context, change):
    w = world
    data = payload(w, context=context)
    assert_accepted(post(w, data))
    changed = deepcopy(data)
    if change == 'quantity':
        changed['items'][0]['quantity'] = 3
    elif change == 'price':
        changed['items'][0]['price'] = 110
    elif change == 'cash':
        changed['cash_received'] = 2000
    elif change == 'customer':
        changed['customer_id'] = w.records['A'].customer
    elif change == 'payment':
        changed['payment_method'] = 'credit_card'
    elif change == 'device':
        changed['checkout_context']['device_id'] = 'different-device'
    else:
        changed['checkout_context']['expected_total'] = 211
    before = state(w)
    response = post(w, changed)
    assert response.status_code == 409, response.get_json()
    assert response.get_json().get('code') == 'idempotency_conflict'
    assert state(w) == before


@pytest.mark.parametrize('context,offline', [(False, False), (True, False), (True, True)])
def test_replay_precedes_mutable_product_and_customer_validation(world, context, offline):
    w, m = world, world.m
    data = payload(w, context=context, offline=offline)
    data['customer_id'] = w.records['A'].customer
    response = post(w, data)
    assert_accepted(response)
    with m.app.app_context():
        product = m.db.session.get(m.Product, w.records['A'].product)
        product.price, product.tax_rate, product.stock, product.name = 999, 17, 0, 'Changed'
        customer = m.db.session.get(m.Customer, w.records['A'].customer)
        customer.name, customer.branch_id = 'Changed', w.records['B'].branch
        m.db.session.commit()
    before = state(w)
    assert_replay(post(w, data), response.get_json())
    assert state(w) == before


@pytest.mark.parametrize('attacker', ['same-branch-owner', 'other-branch',
                                     'same-branch-owner-own-context', 'other-branch-own-context'])
def test_cross_owner_or_branch_never_replays_or_leaks_sale_details(world, attacker):
    w = world
    data = payload(w)
    original = post(w, data)
    assert_accepted(original)
    before = state(w)
    if attacker == 'same-branch-owner-own-context':
        data['checkout_context']['user_id'] = w.other_user
    elif attacker == 'other-branch-own-context':
        data = payload(w, code='B', txn=data['transaction_id'])
    response = (post(w, data, user_id=w.other_user) if attacker.startswith('same-branch-owner')
                else post(w, data, code='B'))
    assert response.status_code in {403, 404, 409}, response.get_json()
    body = response.get_json()
    assert isinstance(body, dict)
    assert not ({'transaction_id', 'total', 'refund_amount', 'items', 'receipt_snapshot',
                 'response_json', 'fingerprint', 'created_at', 'payment_breakdown'} & body.keys())
    assert 'recovery-device-A' not in response.get_data(as_text=True)
    assert state(w) == before


@pytest.mark.parametrize('context', [False, True])
def test_sale_history_deletion_preserves_ledger_tombstone(world, context):
    w, m = world, world.m
    data = payload(w, context=context)
    assert_accepted(post(w, data))
    with m.app.app_context():
        row = ledger_model(m).query.filter_by(transaction_id=data['transaction_id']).one()
        fingerprint, saved = row.fingerprint, row.response_json
    response = client(w).delete('/api/sales/' + data['transaction_id'], json={'confirm': True})
    assert response.status_code == 200, response.get_json()
    with m.app.app_context():
        assert m.Sale.query.filter_by(transaction_id=data['transaction_id']).count() == 0
        row = ledger_model(m).query.filter_by(transaction_id=data['transaction_id']).one()
        assert (row.fingerprint, row.response_json) == (fingerprint, saved)
    before = state(w)
    for request_data in (data, {**data, 'cash_received': 2000}):
        response = post(w, request_data)
        assert response.status_code == 409, response.get_json()
        assert state(w) == before


@pytest.mark.parametrize('offline,field', [(True, 'tax'), (True, 'expected_total'),
                                          (False, 'expected_total')])
def test_snapshot_drift_is_checkout_conflict(world, offline, field):
    w = world
    data = payload(w, offline=offline)
    if field == 'tax':
        data['items'][0]['tax_rate'] = 0
    else:
        data['checkout_context']['expected_total'] = 209.99
    before = state(w)
    response = post(w, data)
    assert response.status_code == 409, response.get_json()
    assert response.get_json().get('code') == 'checkout_conflict'
    assert state(w) == before


@pytest.mark.parametrize('restriction', ['credit_card', 'debt', 'split_payment', 'coupon', 'delivery'])
def test_marked_offline_is_cash_only_without_coupon_or_delivery(world, restriction):
    w, m = world, world.m
    data = payload(w, offline=True)
    if restriction == 'coupon':
        with m.app.app_context():
            m.db.session.add(m.Coupon(code='RECOVERY-VALID', discount_type='fixed',
                                      discount_value=10, is_active=True,
                                      branch_id=w.records['A'].branch))
            m.db.session.commit()
        data['coupon_code'] = 'RECOVERY-VALID'
        data['checkout_context']['expected_total'] = 200
    elif restriction == 'delivery':
        data['delivery'] = {'enabled': True, 'recipient_name': 'Recipient',
                            'recipient_phone': '091234567', 'delivery_address': 'Test street',
                            'delivery_fee': 0, 'payment_status': 'paid'}
    else:
        data['payment_method'] = restriction
        if restriction == 'debt':
            data['customer_id'] = w.records['A'].customer
        if restriction == 'split_payment':
            data['payment_breakdown'] = {'cash': 100, 'credit_card': 110}
    before = state(w)
    response = post(w, data)
    assert response.status_code in {400, 409}, response.get_json()
    assert state(w) == before
    if restriction == 'coupon':
        with m.app.app_context():
            assert m.Coupon.query.filter_by(code='RECOVERY-VALID').one().used_count == 0


@pytest.mark.parametrize('offline,price,promo,accepted', [
    (True, 100, 'none', True), (True, 80, 'active', True),
    (True, 120, 'none', False), (True, 80, 'none', False),
    (True, 80, 'expired', False), (False, 80, 'none', False),
    (False, 80, 'expired', False), (False, 80, 'active', True),
])
def test_existing_price_promotion_rules_and_strict_offline_prices(world, offline, price, promo, accepted):
    w, m = world, world.m
    if promo != 'none':
        with m.app.app_context():
            now = datetime.now(m.pytz.timezone('Asia/Yangon')).replace(tzinfo=None)
            m.db.session.add(m.Promotion(product_id=w.records['A'].product,
                                        discount_type='percent', discount_value=20,
                                        start_date=now - timedelta(days=2),
                                        end_date=now + timedelta(days=2) if promo == 'active'
                                        else now - timedelta(days=1)))
            m.db.session.commit()
    data = payload(w, offline=offline)
    data['items'][0]['price'] = price
    data['checkout_context']['expected_total'] = round(price * 2 * 1.05, 2)
    before = state(w)
    response = post(w, data)
    if accepted:
        assert_accepted(response)
        with m.app.app_context():
            sale = m.Sale.query.filter_by(transaction_id=data['transaction_id']).one()
            assert sale.total == data['checkout_context']['expected_total']
    else:
        assert response.status_code in {400, 409}, response.get_json()
        assert state(w) == before


@pytest.mark.parametrize('path,value', [
    (('items',), []), (('items',), None), (('items',), {}),
    (('items', 0, 'quantity'), 1.5), (('items', 0, 'quantity'), True),
    (('items', 0, 'quantity'), False), (('items', 0, 'quantity'), 0),
    (('items', 0, 'quantity'), -1), (('items', 0, 'quantity'), '1.5'),
    (('items', 0, 'price'), float('nan')), (('items', 0, 'price'), float('inf')),
    (('items', 0, 'price'), float('-inf')), (('items', 0, 'price'), -1),
    (('cash_received',), float('nan')), (('cash_received',), float('inf')),
    (('items', 0, 'tax_rate'), float('nan')),
    (('checkout_context', 'expected_total'), float('nan')),
    (('checkout_context', 'expected_total'), float('inf')),
    (('checkout_context', 'expected_total'), True),
    (('checkout_context', 'offline'), 'true'),
    (('checkout_context', 'branch_id'), True),
    (('checkout_context', 'user_id'), True),
    (('checkout_context', 'occurred_at'), 'not-a-date'),
    (('checkout_context', 'device_id'), ''),
    (('checkout_context',), []),
    (('delivery',), {'enabled': 'false'}),
    (('delivery',), {'enabled': True, 'recipient_name': []}),
    (('delivery',), {'enabled': True, 'recipient_name': 123}),
    (('delivery',), None),
    (('items', 0, 'product_id'), True),
    (('transaction_id',), ''), (('transaction_id',), 'x' * 129),
    (('transaction_id',), ' leading-space'),
])
def test_malformed_checkout_is_4xx_and_leaves_no_business_writes(world, path, value):
    w = world
    data = payload(w, offline=True)
    target = data
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    before = state(w)
    response = post(w, data)
    assert response.status_code in {400, 409, 422}, (path, value, response.get_json())
    assert state(w) == before


@pytest.mark.parametrize('field', ['branch_id', 'user_id'])
def test_context_must_match_authenticated_owner_and_active_branch(world, field):
    w = world
    data = payload(w, offline=True)
    data['checkout_context'][field] = (w.records['B'].branch if field == 'branch_id'
                                        else w.other_user)
    before = state(w)
    response = post(w, data)
    assert response.status_code in {403, 404, 409}, response.get_json()
    assert state(w) == before


def test_debt_requires_customer_and_preserves_obligation_on_replay(world):
    w, m = world, world.m
    data = payload(w, context=False)
    data['payment_method'] = 'debt'
    before = state(w)
    response = post(w, data)
    assert response.status_code == 400, response.get_json()
    assert state(w) == before
    data['customer_id'] = w.records['A'].customer
    accepted = post(w, data)
    assert_accepted(accepted)
    with m.app.app_context():
        debt = m.Debt.query.one()
        original_balance = debt.balance
        assert original_balance > 0
        m.db.session.get(m.Customer, data['customer_id']).branch_id = w.records['B'].branch
        m.db.session.commit()
    before_replay = state(w)
    assert_replay(post(w, data), accepted.get_json())
    assert state(w) == before_replay
    with m.app.app_context():
        assert m.Debt.query.one().balance == original_balance


def test_stock_shortage_rolls_back_earlier_line_sale_debt_ledger_and_audit(world):
    """Two individually valid duplicate lines fail the guarded second decrement."""
    w, m = world, world.m
    data = payload(w, context=False, quantity=25)
    data['items'].append(deepcopy(data['items'][0]))
    data.update(payment_method='debt', customer_id=w.records['A'].customer)
    before = state(w)
    response = post(w, data)
    assert response.status_code in {400, 409}, response.get_json()
    assert state(w) == before
    # A rejected key must remain retryable, not become a false accepted tombstone.
    with m.app.app_context():
        m.db.session.get(m.Product, w.records['A'].product).stock = 60
        m.db.session.commit()
    assert_accepted(post(w, data))
    with m.app.app_context():
        assert m.db.session.get(m.Product, w.records['A'].product).stock == 10
        assert m.Sale.query.count() == 1
        assert m.Debt.query.count() == 1
        assert ledger_model(m).query.count() == 1


def simultaneous(w, requests):
    """No sleeps or shared test clients: release all request threads at a barrier."""
    barrier = Barrier(len(requests))

    def submit(request):
        code, data = request
        own_client = client(w, code)
        barrier.wait(timeout=20)
        response = own_client.post('/api/sales', json=deepcopy(data))
        return code, data['transaction_id'], response.status_code, response.get_json()

    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        futures = [pool.submit(submit, request) for request in requests]
        return [future.result(timeout=40) for future in futures]


def test_four_branches_share_central_sqlite_concurrent_online_offline_and_duplicates(world):
    w, m = world, world.m
    requests = []
    unique = {}
    for code in 'ABCD':
        for index in range(3):
            data = payload(w, code, f'four-branch-{code}-{index}',
                           offline=code in 'CD', context=True, quantity=index + 1)
            unique[data['transaction_id']] = (code, data)
            requests.append((code, data))
            if code in 'CD':
                requests.append((code, deepcopy(data)))
    # 18 actual simultaneous requests, one database (not one DB per branch).
    results = simultaneous(w, requests)
    for txn, (code, data) in unique.items():
        group = [result for result in results if result[1] == txn]
        assert sorted(result[2] for result in group) == ([200, 201] if code in 'CD' else [201]), group
        if code in 'CD':
            original = next(result[3] for result in group if result[2] == 201)
            replay = next(result[3] for result in group if result[2] == 200)
            for key, value in original.items():
                if key not in {'message', 'duplicate'}:
                    assert replay[key] == value
    with m.app.app_context():
        Ledger = ledger_model(m)
        assert m.Sale.query.count() == m.SaleItem.query.count() == Ledger.query.count() == 12
        assert m.Debt.query.count() == m.Delivery.query.count() == 0
        for code, record in w.records.items():
            assert m.db.session.get(m.Product, record.product).stock == 34
            sales = m.Sale.query.filter_by(branch_id=record.branch).all()
            assert len(sales) == 3
            assert sum(sale.total for sale in sales) == 630
            assert all(sale.user_id == record.user for sale in sales)
            rows = Ledger.query.filter_by(branch_id=record.branch).all()
            assert len(rows) == 3
            assert all(row.user_id == record.user and bool(row.offline) == (code in 'CD') for row in rows)
            logs = m.AuditLog.query.filter_by(entity_type='Product', entity_id=str(record.product),
                                             action='update', request_path='/api/sales').all()
            stock_logs = [log for log in logs if 'stock' in (m.decode_audit_changes(log) or {})]
            assert len(stock_logs) == 3, [(log.summary, m.decode_audit_changes(log)) for log in logs]
            transitions = [m.decode_audit_changes(log)['stock'] for log in stock_logs]
            assert sum(change['before'] - change['after'] for change in transitions) == 6
            assert all(log.branch_id == record.branch and log.actor_user_id == record.user
                       for log in stock_logs)
    before = state(w)
    for code, data in unique.values():
        assert post(w, data, code).status_code == 200
    assert state(w) == before


def test_concurrent_same_id_different_payload_has_one_winner_and_one_conflict(world):
    w = world
    first = payload(w, offline=True, quantity=2)
    second = payload(w, offline=True, quantity=3)
    results = simultaneous(w, [('A', first), ('A', second)])
    assert sorted(row[2] for row in results) == [201, 409], results
    assert next(row[3] for row in results if row[2] == 409)['code'] == 'idempotency_conflict'
    snapshot = state(w)
    assert snapshot['sales'] == snapshot['ledger'] == snapshot['items'] == 1
    with w.m.app.app_context():
        sale = w.m.Sale.query.one()
        quantity = w.m.SaleItem.query.one().quantity
        assert snapshot['stocks']['A'] == 40 - quantity
        assert sale.total == quantity * 105


def test_duplicate_product_lines_record_accurate_stock_audit_chain(world):
    w, m = world, world.m
    data = payload(w, offline=True, quantity=2)
    data['items'].append({**data['items'][0], 'quantity': 3})
    data['checkout_context']['expected_total'] = 525
    assert_accepted(post(w, data))
    with m.app.app_context():
        logs = m.AuditLog.query.filter_by(entity_type='Product', entity_id=str(w.records['A'].product),
                                        action='update', request_path='/api/sales').order_by(m.AuditLog.id).all()
        transitions = [m.decode_audit_changes(log)['stock'] for log in logs]
        assert transitions == [{'before': 40, 'after': 38}, {'before': 38, 'after': 35}]
        assert m.db.session.get(m.Product, w.records['A'].product).stock == 35
    before = state(w)
    assert post(w, data).status_code == 200
    assert state(w) == before


@pytest.mark.parametrize('round_number', range(4))
def test_repeated_four_branch_burst_with_duplicates_never_loses_or_double_sells(world, round_number):
    w, m = world, world.m
    requests = []
    for code in 'ABCD':
        for index in range(4):
            data = payload(w, code, f'burst-{round_number}-{code}-{index}',
                           offline=code in 'CD', quantity=1)
            requests.extend([(code, data), (code, deepcopy(data))])
    # Reverse on alternating rounds to vary acquisition order without flaky sleeps.
    results = simultaneous(w, requests if round_number % 2 else requests[::-1])
    assert sum(row[2] == 201 for row in results) == 16, results
    assert sum(row[2] == 200 for row in results) == 16, results
    with m.app.app_context():
        assert m.Sale.query.count() == m.CheckoutRequest.query.count() == 16
        for record in w.records.values():
            assert m.db.session.get(m.Product, record.product).stock == 36
            assert m.Sale.query.filter_by(branch_id=record.branch).count() == 4


def test_exhausted_writer_lock_returns_503_then_same_id_succeeds(world):
    w, m = world, world.m
    data = payload(w, offline=True)
    with m.app.app_context():
        database, engine = m.db.engine.url.database, m.db.engine
    before = state(w)

    def short_timeout(connection, cursor, statement, parameters, context, executemany):
        if statement.strip().upper().startswith('BEGIN IMMEDIATE'):
            cursor.execute('PRAGMA busy_timeout=5')

    event.listen(engine, 'before_cursor_execute', short_timeout)
    try:
        with closing(sqlite3.connect(database, timeout=5)) as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            try:
                response = post(w, data)
                assert response.status_code == 503, response.get_json()
                assert response.get_json()['code'] == 'retry_later'
            finally:
                blocker.rollback()
    finally:
        event.remove(engine, 'before_cursor_execute', short_timeout)
    assert state(w) == before
    accepted = post(w, data)
    assert_accepted(accepted)
    assert_replay(post(w, data), accepted.get_json())


def test_concurrent_stock_race_cannot_oversell_or_record_losing_ledger(world):
    w = world
    requests = [('A', payload(w, txn=f'stock-race-{i}', context=False, quantity=25)) for i in range(2)]
    results = simultaneous(w, requests)
    assert sum(row[2] == 201 for row in results) == 1, results
    assert next(row[2] for row in results if row[2] != 201) in {400, 409}
    snapshot = state(w)
    assert snapshot['sales'] == snapshot['items'] == snapshot['ledger'] == 1
    assert snapshot['stocks']['A'] == 15


def test_transient_real_sqlite_writer_lock_recovers_and_retry_is_idempotent(world):
    w, m = world, world.m
    data = payload(w, offline=True)
    with m.app.app_context():
        database = m.db.engine.url.database
        engine = m.db.engine
    attempted = Event()

    def observe_begin(connection, cursor, statement, parameters, context, executemany):
        if statement.strip().upper().startswith('BEGIN IMMEDIATE'):
            attempted.set()

    event.listen(engine, 'before_cursor_execute', observe_begin)
    try:
        with closing(sqlite3.connect(database, timeout=5)) as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(post, w, data)
                try:
                    assert attempted.wait(timeout=10), 'Checkout never attempted its SQLite transaction'
                    assert not future.done(), 'Checkout should wait/retry the competing writer lock'
                finally:
                    blocker.rollback()
                response = future.result(timeout=20)
        assert_accepted(response)
    finally:
        event.remove(engine, 'before_cursor_execute', observe_begin)
    before = state(w)
    assert before['sales'] == before['ledger'] == 1
    assert before['stocks']['A'] == 38
    assert_replay(post(w, data), response.get_json())
    assert state(w) == before


@pytest.mark.parametrize('fail_on', ['ledger', 'stock'])
def test_real_database_write_failure_rolls_back_sale_stock_debt_and_ledger(world, fail_on):
    """A SQLite trigger aborts the real write, not a mocked session/checkout."""
    w, m = world, world.m
    data = payload(w, context=False)
    data.update(payment_method='debt', customer_id=w.records['A'].customer)
    with m.app.app_context():
        table = ledger_model(m).__tablename__ if fail_on == 'ledger' else 'product'
        operation = 'INSERT' if fail_on == 'ledger' else 'UPDATE OF stock'
        m.db.session.execute(text(f'CREATE TRIGGER recovery_abort BEFORE {operation} ON "{table}" '
                                  "BEGIN SELECT RAISE(ABORT, 'recovery injected write failure'); END"))
        m.db.session.commit()
    before = state(w)
    try:
        response = post(w, data)
        assert response.status_code >= 400, response.get_json()
        assert state(w) == before
    finally:
        with m.app.app_context():
            m.db.session.rollback()
            m.db.session.execute(text('DROP TRIGGER recovery_abort'))
            m.db.session.commit()
    assert_accepted(post(w, data))
    assert state(w)['ledger'] == state(w)['sales'] == state(w)['debts'] == 1


def test_startup_creates_missing_ledger_and_second_process_preserves_accepted_request(world):
    """Upgrade a pre-ledger DB, then exercise restart replay without create_all here."""
    w, m = world, world.m
    with m.app.app_context():
        table = ledger_model(m).__tablename__
        source_path = m.db.engine.url.database
        m.db.session.remove()
    with tempfile.TemporaryDirectory(prefix='pos-checkout-startup-') as directory:
        root = Path(directory)
        (root / 'instance').mkdir()
        target = root / 'instance' / 'pos.db'
        with closing(sqlite3.connect(source_path)) as source, closing(sqlite3.connect(target)) as destination:
            source.backup(destination)
            destination.execute(f'DROP TABLE "{table}"')
            destination.commit()
        script = '''
import json
from pathlib import Path
import sys
from flask import Flask
from unittest.mock import patch
root, phase = sys.argv[1:3]
original = Flask.__init__
def isolated(app, *args, **kwargs):
    kwargs.update(instance_path=str(Path(root) / 'instance'), root_path=root)
    original(app, *args, **kwargs)
with patch.object(Flask, '__init__', isolated):
    import app as m
try:
    data = json.loads(Path(root, 'request.json').read_text())
    c = m.app.test_client()
    with c.session_transaction() as session:
        session.update(user_id=data['checkout_context']['user_id'], role='manager',
                       username='recovery-A', branch_id=data['checkout_context']['branch_id'])
    response = c.post('/api/sales', json=data)
    assert response.status_code == (201 if phase == 'first' else 200), (response.status_code, response.get_json())
finally:
    with m.app.app_context():
        m.db.session.remove()
        m.db.engine.dispose()
'''
        (root / 'request.json').write_text(json.dumps(payload(w, offline=True)), encoding='utf-8')
        env = dict(os.environ, SESSION_COOKIE_SECURE='false', PYTHONDONTWRITEBYTECODE='1')
        snapshot = None
        for phase in ('first', 'second'):
            result = subprocess.run([sys.executable, '-B', '-c', script, str(root), phase],
                                    cwd=Path(__file__).parent, env=env,
                                    capture_output=True, text=True, timeout=90)
            assert result.returncode == 0, result.stdout + result.stderr
            with closing(sqlite3.connect(target)) as connection:
                rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()
                assert len(rows) == 1
                current = (rows,
                           connection.execute('SELECT transaction_id, total FROM sale').fetchall(),
                           connection.execute('SELECT id, stock FROM product ORDER BY id').fetchall(),
                           connection.execute('SELECT COUNT(*) FROM audit_log').fetchone())
                assert connection.execute('SELECT COUNT(*) FROM sale').fetchone()[0] == 1
                assert connection.execute('SELECT stock FROM product WHERE id=?',
                                          (w.records['A'].product,)).fetchone()[0] == 38
                if snapshot is not None:
                    assert current == snapshot
                snapshot = current