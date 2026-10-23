"""Bounded receiving, aggregate money, and FIFO chunk regressions."""

from datetime import datetime

from sqlalchemy import func

from test_inventory_scalability import world, capture, client


def test_summary_rounds_each_batch_without_materializing_inventory(world):
    m = world
    with m.app.app_context():
        product = m.Product(name='Rounding', price=1, stock=4, branch_id=m.scale['branch'])
        m.db.session.add(product)
        m.db.session.flush()
        product_id = product.id
        m.db.session.add_all([m.WarehouseInventory(product_id=product_id, quantity=3, unit_cost=0.105),
                              m.WarehouseInventory(product_id=product_id, quantity=1, unit_cost=0.005)])
        m.db.session.commit()
    c = client(m)
    with capture(m) as (sql, loaded):
        data = c.get('/api/warehouse/summary').get_json()
    assert 'WarehouseInventory' not in loaded
    assert len(sql) <= 4
    # 131 original batches alternate10/3 units at2.25, plus new0.32+0.01.
    assert data['total_value'] == (66 * 10 + 65 * 3) * 2.25 + 0.33
    summary = c.get('/api/products/summary').get_json()
    assert summary['total_products'] == 132
    assert summary['low_stock'] == 1
    with m.app.app_context():
        m.WarehouseInventory.query.filter_by(product_id=product_id).delete()
        m.Product.query.filter_by(id=product_id).delete()
        m.db.session.commit()


def test_transfer_chunking_and_batch_cap_are_atomic(world):
    m = world
    with m.app.app_context():
        products = [m.Product(name=f'FIFO {i}', catalog_id=f'chunk-{i}', price=1, stock=0,
                              branch_id=m.scale['branch']) for i in range(2)]
        m.db.session.add_all(products)
        m.db.session.flush()
        ids = [p.id for p in products]
        now = datetime.utcnow()
        for product_id, count in zip(ids, (150, 501)):
            m.db.session.bulk_insert_mappings(m.WarehouseInventory, [dict(product_id=product_id,
                quantity=1, unit_cost=1, received_date=now, created_at=now) for _ in range(count)])
        m.db.session.commit()
    c = client(m)
    payload = dict(product_id=ids[0], quantity=150, target_branch_id=m.scale['branch'], idempotency_key='fifo-150')
    first = c.post('/api/warehouse/transfer', json=payload)
    assert first.status_code == 200, first.get_json()
    replay = c.post('/api/warehouse/transfer', json=payload)
    assert replay.status_code == 200 and replay.get_json()['replayed']
    capped = c.post('/api/warehouse/transfer', json=dict(product_id=ids[1], quantity=501,
        target_branch_id=m.scale['branch'], idempotency_key='fifo-501'))
    assert capped.status_code == 400, capped.get_json()
    with m.app.app_context():
        assert m.db.session.get(m.Product, ids[0]).stock == 150
        assert m.db.session.get(m.Product, ids[1]).stock == 0
        assert m.db.session.query(func.sum(m.WarehouseInventory.quantity)).filter_by(product_id=ids[0]).scalar() == 0
        assert m.db.session.query(func.sum(m.WarehouseInventory.quantity)).filter_by(product_id=ids[1]).scalar() == 501
        assert m.db.session.get(m.TransferRequest, 'fifo-501') is None


def test_large_order_receiving_detail_pages_are_complete_and_owned(world):
    m = world
    with m.app.app_context():
        po = m.PurchaseOrder(po_number='LARGE-LINE-PAGING', supplier_id=m.scale['suppliers'][0],
                             branch_id=m.scale['branch'], status='approved')
        m.db.session.add(po)
        m.db.session.flush()
        po_id = po.id
        m.db.session.bulk_insert_mappings(m.PurchaseOrderItem, [dict(purchase_order_id=po_id,
            product_id=m.scale['products'][0], ordered_qty=1, received_qty=0, unit_cost=1) for _ in range(601)])
        m.db.session.commit()
    c, cursor, seen = client(m), None, []
    assert c.get(f'/api/purchase_orders/{po_id}').status_code == 413
    first_cursor = None
    while True:
        params = dict(view='receiving', per_page=30)
        if cursor:
            params['cursor'] = cursor
        with capture(m) as (sql, loaded):
            response = c.get(f'/api/purchase_orders/{po_id}', query_string=params)
        assert response.status_code == 200, response.get_json()
        data = response.get_json()
        assert len(data['items']) <= 30
        # One bounded lookahead row establishes has_more without COUNT(*).
        assert loaded.count('PurchaseOrderItem') <= 31
        assert len(sql) <= 8
        seen.extend(item['id'] for item in data['items'])
        if not data['has_more']:
            break
        cursor = data['next_cursor']
        first_cursor = first_cursor or cursor
    assert len(seen) == len(set(seen)) == 601
    other_id = m.scale['orders'][0]
    assert c.get(f'/api/purchase_orders/{other_id}', query_string=dict(
        view='receiving', per_page=30, cursor=first_cursor)).status_code == 400


def test_branch_lookup_exact_ids_cannot_escape_branch(world):
    c = client(world)
    own = c.get('/api/products', query_string=dict(view='lookup', id=world.scale['products'][0])).get_json()
    foreign = c.get('/api/products', query_string=dict(view='lookup', id=world.scale['foreign_product'])).get_json()
    assert len(own) == 1
    assert foreign == []