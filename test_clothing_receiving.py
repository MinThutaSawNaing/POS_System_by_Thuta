"""Retry-safe receiving via existing PO/warehouse workflow, synthetic data only."""

from concurrent.futures import ThreadPoolExecutor
import uuid

import pytest


@pytest.fixture
def receiving():
    import app as pos
    with pos.app.app_context():
        pos.app.config['TESTING'] = True
        tag = uuid.uuid4().hex
        branch = pos.Branch.query.filter_by(is_active=True).first()
        user = pos.User.query.filter_by(username='admin').first()
        supplier = pos.Supplier(name=tag, branch_id=branch.id)
        style = pos.ClothingStyle(name='Shirt', code=tag, code_norm=tag)
        product = pos.Product(name='Shirt', price=20, stock=0, barcode=tag, branch_id=branch.id)
        pos.db.session.add_all([supplier, style, product])
        pos.db.session.flush()
        variant = pos.ClothingVariant(catalog_id=product.catalog_id, style_id=style.id,
            size='M', color='Black', size_norm='m', color_norm='black')
        po = pos.PurchaseOrder(po_number=tag, supplier_id=supplier.id, status='approved',
            total_amount=100, created_by=user.id, branch_id=branch.id)
        pos.db.session.add_all([variant, po])
        pos.db.session.flush()
        line = pos.PurchaseOrderItem(purchase_order_id=po.id, product_id=product.id,
            ordered_qty=10, received_qty=0, unit_cost=10)
        pos.db.session.add(line)
        pos.db.session.commit()
        ids = (po.id, line.id, product.id, user.id, branch.id)
        def client():
            result = pos.app.test_client()
            with result.session_transaction() as state:
                state.update(user_id=ids[3], role='manager', branch_id=ids[4])
            return result
        yield pos, ids, client
        pos.db.session.rollback()
        pos.ReceivingRequest.query.filter(pos.ReceivingRequest.key.like(tag + '%')).delete(synchronize_session=False)
        pos.WarehouseInventory.query.filter_by(product_id=ids[2]).delete()
        pos.PurchaseOrderItem.query.filter_by(purchase_order_id=ids[0]).delete()
        pos.db.session.delete(po)
        pos.db.session.delete(variant)
        pos.db.session.delete(product)
        pos.db.session.delete(style)
        pos.db.session.delete(supplier)
        pos.db.session.commit()


def test_partial_and_completed_receiving_retries_do_not_add_stock_twice(receiving):
    pos, (po, item, product, *_), clients = receiving
    key = pos.db.session.get(pos.PurchaseOrder, po).po_number
    endpoint = f'/api/purchase_orders/{po}/receive'
    payload = dict(items=[dict(purchase_order_item_id=item, received_qty=4)], idempotency_key=key)
    assert clients().post(endpoint, json=payload).status_code == 200
    assert clients().post(endpoint, json=payload).get_json()['replayed'] is True
    changed = dict(payload, items=[dict(purchase_order_item_id=item, received_qty=5)])
    assert clients().post(endpoint, json=changed).status_code == 409
    remainder = dict(items=[dict(purchase_order_item_id=item, received_qty=6)], idempotency_key=key + '-final')
    assert clients().post(endpoint, json=remainder).status_code == 200
    assert clients().post(endpoint, json=remainder).get_json()['replayed'] is True
    pos.db.session.expire_all()
    assert sum(row.quantity for row in pos.WarehouseInventory.query.filter_by(product_id=product)) == 10
    assert pos.db.session.get(pos.Product, product).stock == 0
    assert pos.db.session.get(pos.PurchaseOrderItem, item).received_qty == 10


def test_concurrent_identical_receiving_request_applies_once(receiving):
    pos, (po, item, product, *_), clients = receiving
    key = pos.db.session.get(pos.PurchaseOrder, po).po_number
    payload = dict(items=[dict(purchase_order_item_id=item, received_qty=3)], idempotency_key=key)
    pairs = [clients(), clients()]
    pos.db.session.remove()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda client: client.post(f'/api/purchase_orders/{po}/receive', json=payload), pairs))
    assert [result.status_code for result in results] == [200, 200]
    assert sum(bool(result.get_json().get('replayed')) for result in results) == 1
    assert sum(row.quantity for row in pos.WarehouseInventory.query.filter_by(product_id=product)) == 3