"""Return integrity tests. Execute only through python run_tests.py."""

from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier
from types import SimpleNamespace
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, inspect, text


@pytest.fixture
def world():
    import app as pos
    context = pos.app.app_context()
    context.push()
    pos.app.config['TESTING'] = True
    branch = pos.get_default_branch_id()
    user = pos.User.query.filter_by(username='admin').first()
    tag = uuid.uuid4().hex
    product = pos.Product(name='Shirt ' + tag, barcode=tag, branch_id=branch,
                          price=120, tax_rate=10, stock=5)
    replacement = pos.Product(name='Replacement ' + tag, barcode='R' + tag,
                              branch_id=branch, price=150, tax_rate=10, stock=1)
    pos.db.session.add_all([product, replacement])
    sale = pos.Sale(transaction_id=tag, branch_id=branch, user_id=user.id,
                    payment_method='cash', total=210, tax=10)
    pos.db.session.add(sale)
    pos.db.session.flush()
    item = pos.SaleItem(sale_id=sale.id, product_id=product.id,
                        quantity=2, price=100, tax=10)
    pos.db.session.add(item)
    pos.db.session.commit()
    ids = dict(sale=sale.id, item=item.id, product=product.id, replacement=replacement.id,
               user=user.id, branch=branch, transaction=tag)

    def client(role='manager', user_id=None, branch_id=None):
        result = pos.app.test_client()
        with result.session_transaction() as cookie:
            cookie.update(user_id=user_id or ids['user'], role=role,
                          branch_id=branch_id or ids['branch'])
        return result

    payload = dict(original_transaction_id=tag,
                   return_items=[dict(sale_item_id=item.id, quantity=1)])
    yield SimpleNamespace(pos=pos, ids=ids, client=client, payload=payload)
    pos.db.session.rollback()
    workflows = pos.ReturnExchange.query.filter_by(original_sale_id=ids['sale']).all()
    adjustment_ids = [row.adjustment_sale_id for row in workflows if row.adjustment_sale_id]
    workflow_ids = [row.id for row in workflows]
    pos.ReturnExchangeItem.query.filter(pos.ReturnExchangeItem.return_exchange_id.in_(workflow_ids)).delete(synchronize_session=False)
    pos.ReturnExchange.query.filter(pos.ReturnExchange.id.in_(workflow_ids)).delete(synchronize_session=False)
    sale_ids = [ids['sale'], *adjustment_ids]
    pos.SaleItem.query.filter(pos.SaleItem.sale_id.in_(sale_ids)).delete(synchronize_session=False)
    pos.Sale.query.filter(pos.Sale.id.in_(sale_ids)).delete(synchronize_session=False)
    pos.Product.query.filter(pos.Product.id.in_([ids['product'], ids['replacement']])).delete(synchronize_session=False)
    pos.db.session.commit()
    context.pop()


def stock(world, key='product'):
    world.pos.db.session.expire_all()
    return world.pos.db.session.get(world.pos.Product, world.ids[key]).stock


@pytest.mark.parametrize('disposition,expected', [('sellable', 6), ('damaged', 5)])
def test_disposition_preserves_historical_financials(world, disposition, expected):
    payload = {**world.payload, 'reason': 'Fit', 'return_items': [
        {**world.payload['return_items'][0], 'disposition': disposition, 'reason': 'Torn seam'}]}
    response = world.client().post('/api/returns_exchanges', json=payload)
    assert response.status_code == 201
    result = response.get_json()
    assert Decimal(str(result['return_total'])) == Decimal(str(result['refund_amount'])) == 105
    assert Decimal(str(result['net_total'])) == -105
    assert stock(world) == expected
    detail = world.client().get('/api/returns_exchanges/' + result['workflow_id']).get_json()
    assert detail['reason'] == 'Fit'
    assert detail['items'][0]['reason'] == 'Torn seam'
    assert detail['items'][0]['disposition'] == disposition


def test_legacy_default_sellable(world):
    response = world.client().post('/api/returns_exchanges', json=world.payload)
    assert response.status_code == 201
    assert stock(world) == 6


@pytest.mark.parametrize('changes', [
    {'disposition': 'discarded'}, {'reason': ['bad']}, {'reason': 'x' * 301},
])
def test_invalid_return_metadata_rolls_back_and_releases_writer(world, changes):
    payload = {**world.payload, 'return_items': [{**world.payload['return_items'][0], **changes}]}
    assert world.client().post('/api/returns_exchanges', json=payload).status_code == 400
    # A different request must not inherit an open writer transaction.
    assert world.client().post('/api/returns_exchanges', json=world.payload).status_code == 201
    assert stock(world) == 6


@pytest.mark.parametrize('bad', [True, 1.5, 0, -1, '1.0'])
def test_invalid_quantity_is_rejected_without_truncation(world, bad):
    world.payload['return_items'][0]['quantity'] = bad
    assert world.client().post('/api/returns_exchanges', json=world.payload).status_code == 400
    assert stock(world) == 5


def test_duplicate_returns_and_replacement_demand_are_rejected(world):
    row = world.payload['return_items'][0]
    payload = {**world.payload, 'return_items': [row, row]}
    assert world.client().post('/api/returns_exchanges', json=payload).status_code == 400
    exchange = dict(product_id=world.ids['replacement'], quantity=1)
    payload = {**world.payload, 'exchange_items': [exchange, exchange]}
    assert world.client().post('/api/returns_exchanges', json=payload).status_code == 400
    assert stock(world) == 5
    assert stock(world, 'replacement') == 1


def test_exchange_settlement_and_adjustment_branch(world):
    payload = {**world.payload, 'exchange_items': [
        dict(product_id=world.ids['replacement'], quantity=1)]}
    response = world.client().post('/api/returns_exchanges', json=payload)
    assert response.status_code == 201
    result = response.get_json()
    assert tuple(Decimal(str(result[key])) for key in ('return_total', 'exchange_total', 'net_total')) == (105, 165, 60)
    assert Decimal(str(result['collected_amount'])) == 60
    sale = world.pos.Sale.query.filter_by(transaction_id=result['adjustment_transaction_id']).one()
    assert sale.branch_id == world.ids['branch']
    assert sale.total == 165 and sale.tax == 15 and sale.payment_method == 'exchange'
    assert stock(world, 'replacement') == 0


def test_damaged_return_does_not_supply_replacement_stock(world):
    world.pos.db.session.get(world.pos.Product, world.ids['product']).stock = 0
    world.pos.db.session.commit()
    payload = {**world.payload, 'return_items': [
        {**world.payload['return_items'][0], 'disposition': 'damaged'}],
        'exchange_items': [dict(product_id=world.ids['product'], quantity=1)]}
    assert world.client().post('/api/returns_exchanges', json=payload).status_code == 400
    assert stock(world) == 0


def test_idempotency_retries_are_durable_and_payload_bound(world):
    payload = {**world.payload, 'idempotency_key': uuid.uuid4().hex}
    first = world.client().post('/api/returns_exchanges', json=payload)
    assert first.status_code == 201
    world.pos.db.session.remove()
    second = world.client().post('/api/returns_exchanges', json=payload)
    assert second.status_code == 200
    assert second.get_json()['replayed'] is True
    assert second.get_json()['workflow_id'] == first.get_json()['workflow_id']
    assert stock(world) == 6
    payload['reason'] = 'Different request'
    assert world.client().post('/api/returns_exchanges', json=payload).status_code == 409


def test_role_and_sale_owner_price_rules_unchanged(world):
    assert world.client('unknown').post('/api/returns_exchanges', json=world.payload).status_code == 403
    assert world.client('cashier', user_id=999999).post('/api/returns_exchanges', json=world.payload).status_code == 404
    payload = {**world.payload, 'exchange_items': [
        dict(product_id=world.ids['replacement'], quantity=1, price=1)]}
    assert world.client('cashier').post('/api/returns_exchanges', json=payload).status_code == 400
    assert stock(world) == 5


@pytest.mark.parametrize('method', ['mmqr', 'split_payment', 'invented'])
def test_no_mobile_settlement_or_provider_refund_bypass(world, method):
    payload = {**world.payload, 'settlement_method': method}
    assert world.client().post('/api/returns_exchanges', json=payload).status_code == 400
    assert stock(world) == 5


def test_mobile_collection_restriction_and_manual_legacy_behavior(world, monkeypatch):
    import payment_routes
    payload = {**world.payload, 'settlement_method': 'mobile_payment', 'exchange_items': [
        dict(product_id=world.ids['replacement'], quantity=1)]}
    monkeypatch.setattr(payment_routes, 'manual_payment_allowed', lambda: False)
    assert world.client().post('/api/returns_exchanges', json=payload).status_code == 409
    assert stock(world, 'replacement') == 1
    monkeypatch.setattr(payment_routes, 'manual_payment_allowed', lambda: True)
    assert world.client().post('/api/returns_exchanges', json=payload).status_code == 201


def test_mobile_refund_is_bookkeeping_only(world, monkeypatch):
    import payment_routes
    monkeypatch.setattr(payment_routes, 'manual_payment_allowed', lambda: False)
    payload = {**world.payload, 'settlement_method': 'mobile_payment'}
    response = world.client().post('/api/returns_exchanges', json=payload)
    assert response.status_code == 201
    assert Decimal(str(response.get_json()['refund_amount'])) == 105
    assert response.get_json()['provider_refund_processed'] is False


def test_key_cannot_be_replayed_by_other_user_or_branch(world):
    payload = {**world.payload, 'idempotency_key': uuid.uuid4().hex}
    assert world.client().post('/api/returns_exchanges', json=payload).status_code == 201
    assert world.client(user_id=999999).post('/api/returns_exchanges', json=payload).status_code == 409
    assert world.client(branch_id=999999).post('/api/returns_exchanges', json=payload).status_code in (403, 404)
    assert stock(world) == 6


def test_failed_commit_rolls_back_inventory_and_idempotency(world, monkeypatch):
    payload = {**world.payload, 'idempotency_key': uuid.uuid4().hex,
               'exchange_items': [dict(product_id=world.ids['replacement'], quantity=1)]}
    original_commit = world.pos.db.session.commit

    def fail():
        raise RuntimeError('test commit failure')

    monkeypatch.setattr(world.pos.db.session, 'commit', fail)
    assert world.client().post('/api/returns_exchanges', json=payload).status_code == 500
    monkeypatch.setattr(world.pos.db.session, 'commit', original_commit)
    assert stock(world) == 5
    assert stock(world, 'replacement') == 1
    assert world.pos.ReturnExchange.query.filter_by(idempotency_key=payload['idempotency_key']).first() is None
    assert world.client().post('/api/returns_exchanges', json=payload).status_code == 201


def test_return_description_uses_original_snapshot_not_renamed_product(world):
    item = world.pos.db.session.get(world.pos.SaleItem, world.ids['item'])
    item.display_snapshot = json.dumps(dict(name='Original Shirt — Blue / M', size='M', color='Blue'))
    world.pos.db.session.get(world.pos.Product, world.ids['product']).name = 'Renamed Product'
    world.pos.db.session.commit()
    response = world.client().post('/api/returns_exchanges', json=world.payload)
    workflow_id = response.get_json()['workflow_id']
    detail = world.client().get('/api/returns_exchanges/' + workflow_id).get_json()
    assert detail['items'][0]['product_name'] == 'Original Shirt — Blue / M'
    printed = world.client().get('/api/returns_exchanges/' + workflow_id + '/print').get_data(as_text=True)
    assert 'Original Shirt' in printed and 'Blue / M' in printed


@pytest.mark.parametrize('kind', ['overreturn', 'replacement', 'retry'])
def test_concurrent_requests_are_serialized(world, kind):
    payload = {**world.payload}
    if kind == 'overreturn':
        payload['return_items'] = [{**payload['return_items'][0], 'quantity': 2}]
    elif kind == 'replacement':
        payload['exchange_items'] = [dict(product_id=world.ids['replacement'], quantity=1)]
    else:
        payload['idempotency_key'] = uuid.uuid4().hex
    clients = [world.client(), world.client()]
    barrier = Barrier(2)
    world.pos.db.session.remove()

    def run(client):
        barrier.wait(timeout=10)
        response = client.post('/api/returns_exchanges', json=payload)
        return response.status_code, response.get_json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, clients))
    expected = [200, 201] if kind == 'retry' else [201, 400]
    assert sorted(status for status, _ in results) == expected
    assert world.pos.ReturnExchange.query.filter_by(original_sale_id=world.ids['sale']).count() == 1
    assert stock(world) == (7 if kind == 'overreturn' else 6)


def test_additive_migration_is_repeatable_and_preserves_legacy_data():
    from clothing_returns import migrate_clothing_returns
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE return_exchange (id INTEGER PRIMARY KEY, return_total FLOAT)'))
        connection.execute(text('CREATE TABLE return_exchange_item (id INTEGER PRIMARY KEY, quantity INTEGER)'))
        connection.execute(text('INSERT INTO return_exchange VALUES (1, 105)'))
        connection.execute(text('INSERT INTO return_exchange_item VALUES (1, 2)'))
    db = SimpleNamespace(engine=engine, session=SimpleNamespace(rollback=lambda: None))
    migrate_clothing_returns(db)
    migrate_clothing_returns(db)
    with engine.connect() as connection:
        assert connection.execute(text('SELECT return_total FROM return_exchange')).scalar() == 105
        assert connection.execute(text('SELECT quantity, disposition FROM return_exchange_item')).one() == (2, 'sellable')
    assert 'response_json' in {col['name'] for col in inspect(engine).get_columns('return_exchange')}
    engine.dispose()