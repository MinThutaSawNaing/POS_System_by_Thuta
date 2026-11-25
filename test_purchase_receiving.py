"""Shared receiving is atomic across branches and concurrent manager requests."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import importlib.util
import shutil
import sys
import tempfile
from unittest.mock import patch
import unittest
from flask import Flask
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from purchase_receiving import receive_purchase_order


class PurchaseReceivingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='pos-receiving-')
        root = Path(cls.temporary.name)
        shutil.copytree(Path(__file__).parent / 'templates', root / 'templates')
        original = Flask.__init__

        def isolated_init(app, *args, **kwargs):
            kwargs.update(root_path=str(root), instance_path=str(root / 'instance'))
            original(app, *args, **kwargs)

        spec = importlib.util.spec_from_file_location('_receiving_test_app', Path(__file__).with_name('app.py'))
        cls.m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.m
        with patch.object(Flask, '__init__', isolated_init):
            spec.loader.exec_module(cls.m)
        cls.m.app.config.update(TESTING=True)

    @classmethod
    def tearDownClass(cls):
        with cls.m.app.app_context():
            cls.m.db.session.remove()
            cls.m.db.engine.dispose()
        cls.temporary.cleanup()

    def setUp(self):
        m = self.m
        with m.app.app_context():
            m.db.drop_all()
            m.db.create_all()
            local = m.Branch(name='Local', code='L', is_default=True)
            foreign = m.Branch(name='Catalog', code='F')
            m.db.session.add_all([local, foreign])
            m.db.session.flush()
            user = m.User(username='manager', password='test', role='manager', branch_id=local.id)
            product = m.Product(name='Other branch product', barcode='RECEIVE', price=10, stock=0, branch_id=foreign.id)
            supplier = m.Supplier(name='Shared', branch_id=foreign.id)
            m.db.session.add_all([user, product, supplier])
            m.db.session.flush()
            po = m.PurchaseOrder(po_number='PO-TEST', supplier_id=supplier.id, status='approved',
                                 total_amount=30, created_by=user.id, branch_id=local.id)
            m.db.session.add(po)
            m.db.session.flush()
            item = m.PurchaseOrderItem(purchase_order_id=po.id, product_id=product.id,
                                      ordered_qty=10, received_qty=0, unit_cost=3)
            m.db.session.add(item)
            m.db.session.commit()
            self.user, self.local, self.foreign = user.id, local.id, foreign.id
            self.po, self.item, self.product, self.supplier = po.id, item.id, product.id, supplier.id

    def client(self, role='manager', branch=None):
        client = self.m.app.test_client()
        with client.session_transaction() as session:
            session.update(user_id=self.user, role=role, branch_id=branch or self.local)
        return client

    def receive(self, quantity, role='manager', **changes):
        client = self.client(role)
        data = {'items': [{'purchase_order_item_id': self.item, 'received_qty': quantity}]}
        data.update(changes)
        return client.post(f'/api/purchase_orders/{self.po}/receive', json=data)

    def invoice(self, **changes):
        data = {'supplier_reference': 'BILL-100', 'invoice_date': '2026-11-18', 'total_amount': '30.00'}
        data.update(changes)
        return self.client().post(f'/api/purchase_orders/{self.po}/invoice', json=data)

    def test_verification_prints_are_scoped_and_pdf(self):
        client = self.client()
        inventory = client.get(f'/api/branch_inventory/print?location_id={self.foreign}&q=RECEIVE')
        self.assertEqual(inventory.status_code, 200)
        self.assertEqual(inventory.mimetype, 'application/pdf')
        self.assertTrue(inventory.data.startswith(b'%PDF-'))
        self.assertIn('no-store', inventory.headers['Cache-Control'])
        self.assertEqual(self.client('cashier').get(
            f'/api/branch_inventory/print?location_id={self.foreign}').status_code, 403)
        self.assertEqual(client.get('/api/branch_inventory/print?stock=invalid').status_code, 400)

        number = self.receive(4).json['grn_number']
        receipt = client.get(f'/api/purchase_orders/{self.po}/receipts').json['items'][0]
        self.assertEqual(receipt['number'], number)
        path = f'/api/purchase_orders/{self.po}/receipts/{receipt["id"]}/print'
        printed = client.get(path)
        self.assertEqual(printed.status_code, 200)
        self.assertTrue(printed.data.startswith(b'%PDF-'))
        self.assertIn('no-store', printed.headers['Cache-Control'])
        self.assertEqual(client.get(f'/api/purchase_orders/999/receipts/{receipt["id"]}/print').status_code, 404)
        self.assertEqual(self.client('cashier').get(path).status_code, 403)

    def test_goods_receipt_header_renders_actual_po_number_and_order_date(self):
        from po_template import PurchaseOrderTemplateHeader, render_po_template_text

        client = self.client()
        self.assertEqual(self.receive(4).status_code, 200)
        receipt = client.get(f'/api/purchase_orders/{self.po}/receipts').json['items'][0]
        path = f'/api/purchase_orders/{self.po}/receipts/{receipt["id"]}/print'
        with self.m.app.app_context():
            po = self.m.db.session.get(self.m.PurchaseOrder, self.po)
            expected_date = po.created_at.strftime('%Y-%m-%d')
            expected_number = po.po_number
        rendered = []
        draw_text = PurchaseOrderTemplateHeader._draw_text

        def record_text(header, canvas, element):
            rendered.append(render_po_template_text(element.get('text'), header.context))
            return draw_text(header, canvas, element)

        with patch.object(PurchaseOrderTemplateHeader, '_draw_text', record_text):
            response = client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b'%PDF-'))
        reference = next(text for text in rendered if text.startswith('PO '))
        self.assertIn(expected_number, reference)
        self.assertIn(expected_date, reference)
        self.assertNotIn('{{po_number}}', reference)
        self.assertNotIn('{{po_date}}', reference)

    def test_branch_inventory_reflects_transfer_not_receipt_or_invoice(self):
        client = self.client()
        path = '/api/branch_inventory?q=RECEIVE'
        other = path + f'&location_id={self.foreign}'
        self.assertEqual(client.get(path).json['items'], [])
        self.assertEqual(self.receive(10).status_code, 200)
        self.assertEqual(self.invoice().status_code, 201)
        self.assertEqual(client.get(other).json['items'][0]['stock'], 0)
        self.assertEqual(client.get(path).json['items'], [])
        payload = dict(product_id=self.product, quantity=3, target_branch_id=self.local,
                       idempotency_key='branch-inventory-transfer')
        for _ in range(2):
            response = client.post('/api/warehouse/transfer', json=payload)
            self.assertEqual(response.status_code, 200, response.json)
        row, = client.get(path).json['items']
        self.assertEqual(row['stock'], 3)
        self.assertNotEqual(row['id'], self.product)
        self.assertEqual(client.get(other).json['items'][0]['stock'], 0)
        with self.m.app.app_context():
            self.assertEqual(self.m.WarehouseInventory.query.one().quantity, 7)
            self.assertEqual(self.m.db.session.get(self.m.PurchaseOrderItem, self.item).product_id, self.product)

    def test_goods_receipt_and_matched_invoice_end_to_end(self):
        m, client = self.m, self.client()
        uri = f'/api/purchase_orders/{self.po}'
        self.assertEqual(self.invoice().status_code, 409)
        blocked = client.get(uri + '/print')
        self.assertEqual(blocked.status_code, 409)
        self.assertIn('Record the purchase invoice', blocked.json['message'])
        part = self.receive(4, idempotency_key='part').json
        self.assertEqual(part['status'], 'partially_received')
        self.assertEqual(self.invoice().status_code, 409)
        self.assertEqual(client.get(uri + '/print').status_code, 409)
        self.assertEqual(client.post(uri + '/cancel', json={}).status_code, 409)
        self.assertEqual(self.receive(4, idempotency_key='part').json['grn_number'], part['grn_number'])
        full = self.receive(6, idempotency_key='full').json
        self.assertEqual(full['status'], 'received')
        page = client.get(uri + '/receipts?per_page=1').json
        self.assertEqual(page['items'][0]['number'], full['grn_number'])
        older = client.get(uri + '/receipts', query_string={'per_page': 1, 'cursor': page['next_cursor']}).json
        self.assertEqual(older['items'][0]['items'][0]['received_qty'], 4)
        self.assertEqual(older['items'][0]['actor_id'], self.user)
        self.assertEqual(self.invoice(total_amount='29.99').status_code, 409)
        self.assertEqual(self.invoice(total_amount='NaN').status_code, 400)
        self.assertEqual(client.get(uri + '/print').status_code, 409)
        self.assertEqual(self.client('cashier').get(uri + '/receipts').status_code, 403)
        self.assertEqual(self.client('cashier').post(uri + '/invoice', json={}).status_code, 403)

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.invoice(), range(2)))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 201])
        self.assertEqual(self.invoice(supplier_reference='OTHER').status_code, 409)
        invoice = client.get(uri + '/invoice').json['invoice']
        self.assertEqual(invoice['items'][0]['quantity'], 10)
        self.assertEqual(invoice['total_amount'], '30.00')
        pdf = client.get(uri + '/print')
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.mimetype, 'application/pdf')
        self.assertIn('no-store', pdf.headers['Cache-Control'])
        order = client.get('/api/purchase_orders', query_string={'status': 'invoiced', 'view': 'register'}).json['items'][0]
        self.assertEqual(order['status'], 'received')
        self.assertEqual(order['invoice']['supplier_reference'], 'BILL-100')
        self.assertEqual(client.get('/api/purchase_orders/summary').json['invoiced'], 1)
        self.assertEqual(client.get('/api/purchase_orders/export?format=xlsx&status=invoiced').status_code, 200)
        self.assertEqual(client.delete(uri, json={'confirm': True}).status_code, 409)
        with m.app.app_context():
            self.assertEqual(m.GoodsReceipt.query.count(), 2)
            self.assertEqual(m.PurchaseInvoice.query.count(), 1)
            self.assertEqual(sum(batch.quantity for batch in m.WarehouseInventory.query.all()), 10)
            self.assertEqual(m.db.session.get(m.Product, self.product).stock, 0)
            self.assertEqual(m.db.session.get(m.Supplier, self.supplier).total_orders, 1)
            m.WarehouseInventory.query.delete()
            m.db.session.commit()
        self.assertEqual(self.client(branch=self.foreign).delete(
            f'/api/products/{self.product}?force=1&cascade=1').status_code, 409)

    def test_additive_schema_preserves_legacy_received_order(self):
        m = self.m
        with m.app.app_context():
            m.PurchaseInvoice.__table__.drop(m.db.engine)
            m.GoodsReceipt.__table__.drop(m.db.engine)
            m.db.session.get(m.PurchaseOrder, self.po).status = 'received'
            m.db.session.get(m.PurchaseOrderItem, self.item).received_qty = 10
            m.db.session.add(m.WarehouseInventory(product_id=self.product, branch_id=self.foreign,
                quantity=10, batch_number='PO-TEST', unit_cost=3))
            m.db.session.commit()
            m.db.create_all()
            m.db.create_all()
            self.assertEqual(m.GoodsReceipt.query.count(), 0)
            self.assertEqual(m.PurchaseInvoice.query.count(), 0)
        self.assertEqual(self.invoice().status_code, 201)
        with m.app.app_context():
            self.assertEqual(m.WarehouseInventory.query.one().quantity, 10)
            self.assertEqual(m.GoodsReceipt.query.count(), 0)

    def test_duplicate_supplier_invoice_reference_across_received_orders(self):
        m = self.m
        self.assertEqual(self.receive(10).status_code, 200)
        self.assertEqual(self.invoice().status_code, 201)
        with m.app.app_context():
            order = m.PurchaseOrder(po_number='PO-SECOND', supplier_id=self.supplier,
                status='received', total_amount=30, branch_id=self.local)
            m.db.session.add(order)
            m.db.session.flush()
            m.db.session.add(m.PurchaseOrderItem(purchase_order_id=order.id, product_id=self.product,
                ordered_qty=10, received_qty=10, unit_cost=3))
            m.db.session.commit()
            uri = f'/api/purchase_orders/{order.id}/invoice'
        response = self.client().post(uri, json={'supplier_reference': 'bill-100',
            'invoice_date': '2026-11-18', 'total_amount': '30'})
        self.assertEqual(response.status_code, 409)
        self.assertIn('already recorded', response.json['message'])
        self.assertEqual(self.client().get(uri).status_code, 404)

    def service_receive(self, quantity=10, *, items=None, **kwargs):
        m = self.m
        return receive_purchase_order(m.db, {
            'PurchaseOrder': m.PurchaseOrder,
            'PurchaseOrderItem': m.PurchaseOrderItem,
            'WarehouseInventory': m.WarehouseInventory,
            'ReceiptRequest': m.ReceiptRequest,
        }, self.po, items if items is not None else [
            {'purchase_order_item_id': self.item, 'received_qty': quantity}], **kwargs)

    def test_keyed_receipt_replays_after_completion_and_session_restart(self):
        m = self.m
        with m.app.app_context():
            first, status = self.service_receive(idempotency_key='durable-retry', actor_id=self.user)
            self.assertEqual(status, 200)
            m.db.session.remove()
            replay, status = self.service_receive(idempotency_key='durable-retry', actor_id=self.user)
            self.assertEqual(status, 200)
            self.assertTrue(replay.pop('replayed'))
            first.pop('replayed', None)
            self.assertEqual(replay, first)
            self.assertEqual(m.ReceiptRequest.query.count(), 1)
            self.assertEqual(m.WarehouseInventory.query.count(), 1)
            self.assertEqual(m.db.session.get(m.Supplier, self.supplier).total_orders, 1)

    def test_http_retry_forwards_key_and_uses_authenticated_actor(self):
        first = self.receive(10, idempotency_key='http-key', actor_id='untrusted')
        self.assertEqual(first.status_code, 200)
        replay = self.receive(10, idempotency_key='http-key', actor_id='another-untrusted')
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json['replayed'])
        self.assertEqual(self.receive(9, idempotency_key='http-key').status_code, 409)
        with self.m.app.app_context():
            self.assertEqual(self.m.WarehouseInventory.query.count(), 1)

    def test_fingerprint_normalizes_quantities_and_rejects_changed_intent(self):
        m = self.m
        with m.app.app_context():
            _, status = self.service_receive(items=[
                {'purchase_order_item_id': str(self.item), 'received_qty': '2'},
                {'purchase_order_item_id': self.item, 'received_qty': 3},
            ], idempotency_key='canonical', actor_id=self.user)
            self.assertEqual(status, 200)
            replay, status = self.service_receive(5, idempotency_key='canonical', actor_id=self.user)
            self.assertEqual(status, 200)
            self.assertTrue(replay['replayed'])
            for changes in ({'quantity': 4}, {'actor_id': self.user + 1}, {'receive_all': True}):
                kwargs = dict(quantity=5, idempotency_key='canonical', actor_id=self.user)
                kwargs.update(changes)
                payload, status = self.service_receive(**kwargs)
                self.assertEqual(status, 409)
                self.assertIn('different receiving request', payload['message'])
            with patch.object(self, 'po', self.po + 1):
                _, status = self.service_receive(5, idempotency_key='canonical', actor_id=self.user)
                self.assertEqual(status, 409)
            self.assertEqual(m.ReceiptRequest.query.count(), 1)
            self.assertEqual(m.db.session.get(m.PurchaseOrderItem, self.item).received_qty, 5)

    def test_invalid_keys_never_mutate_and_failed_request_can_retry(self):
        m = self.m
        with m.app.app_context():
            for key in ('', '  ', 'x' * 129, 1, False, [], {}):
                with self.subTest(key=key):
                    _, status = self.service_receive(1, idempotency_key=key)
                    self.assertEqual(status, 400)
            _, status = self.service_receive(11, idempotency_key='failed-first')
            self.assertEqual(status, 400)
            self.assertEqual(m.ReceiptRequest.query.count(), 0)
            self.assertEqual(m.WarehouseInventory.query.count(), 0)
            _, status = self.service_receive(10, idempotency_key='failed-first')
            self.assertEqual(status, 200)

    def test_concurrent_same_key_replays_without_duplicate_stock(self):
        m = self.m

        def receive(_):
            with m.app.app_context():
                return self.service_receive(4, idempotency_key='concurrent', actor_id=self.user)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(receive, range(2)))
        self.assertEqual([status for _, status in results], [200, 200])
        self.assertEqual(sorted(payload['replayed'] for payload, _ in results), [False, True])
        with m.app.app_context():
            self.assertEqual(m.WarehouseInventory.query.count(), 1)
            self.assertEqual(m.db.session.get(m.PurchaseOrderItem, self.item).received_qty, 4)
            self.assertEqual(m.db.session.get(m.Supplier, self.supplier).total_orders, 1)
            self.assertEqual(m.ReceiptRequest.query.count(), 1)

    def test_receipt_ledger_failure_rolls_back_stock_cost_status_and_counter(self):
        m = self.m
        with m.app.app_context():
            original_cost = m.db.session.get(m.Product, self.product).cost

            def reject_ledger_insert(mapper, connection, target):
                raise IntegrityError('insert receipt request', {}, Exception('simulated constraint failure'))

            event.listen(m.ReceiptRequest, 'before_insert', reject_ledger_insert)
            try:
                _, status = self.service_receive(idempotency_key='atomic')
                self.assertEqual(status, 409)
            finally:
                event.remove(m.ReceiptRequest, 'before_insert', reject_ledger_insert)
            self.assertEqual(m.WarehouseInventory.query.count(), 0)
            self.assertEqual(m.ReceiptRequest.query.count(), 0)
            self.assertEqual(m.db.session.get(m.PurchaseOrderItem, self.item).received_qty, 0)
            self.assertEqual(m.db.session.get(m.PurchaseOrder, self.po).status, 'approved')
            self.assertEqual(m.db.session.get(m.Supplier, self.supplier).total_orders, 0)
            self.assertEqual(m.db.session.get(m.Product, self.product).cost, original_cost)
            _, status = self.service_receive(idempotency_key='atomic')
            self.assertEqual(status, 200)

    def test_partial_receipt_loads_only_requested_lines_and_joins_products(self):
        m = self.m
        with m.app.app_context():
            products = [m.Product(name=f'Line {index}', price=5, stock=0, branch_id=self.foreign)
                        for index in range(600)]
            m.db.session.add_all(products)
            m.db.session.flush()
            lines = [m.PurchaseOrderItem(purchase_order_id=self.po, product_id=product.id,
                                        ordered_qty=2, received_qty=0, unit_cost=2)
                     for product in products]
            m.db.session.add_all(lines)
            m.db.session.commit()
            requested_ids = [self.item, lines[0].id, lines[1].id]
            m.db.session.remove()
            statements, loaded = [], []

            def track_sql(connection, cursor, statement, parameters, context, executemany):
                if statement.lstrip().upper().startswith('SELECT'):
                    statements.append(statement)

            def track_loaded(session, instance):
                loaded.append(type(instance))

            session = m.db.session()
            event.listen(m.db.engine, 'before_cursor_execute', track_sql)
            event.listen(session, 'loaded_as_persistent', track_loaded)
            try:
                result, status = self.service_receive(items=[
                    {'purchase_order_item_id': item_id, 'received_qty': 1}
                    for item_id in requested_ids])
            finally:
                event.remove(m.db.engine, 'before_cursor_execute', track_sql)
                event.remove(session, 'loaded_as_persistent', track_loaded)
            self.assertEqual(status, 200)
            self.assertEqual(result['status'], 'partially_received')
            self.assertEqual(loaded.count(m.PurchaseOrderItem), 3)
            self.assertEqual(loaded.count(m.Product), 3)
            self.assertLessEqual(len(statements), 6)
            self.assertGreaterEqual(sum('EXISTS' in sql.upper() for sql in statements), 2)
            # The first received lines need not be in the next receipt's selection.
            next_item = m.PurchaseOrderItem.query.filter(
                m.PurchaseOrderItem.purchase_order_id == self.po,
                ~m.PurchaseOrderItem.id.in_(requested_ids)).first()
            _, status = self.service_receive(items=[
                {'purchase_order_item_id': next_item.id, 'received_qty': 1}])
            self.assertEqual(status, 200)
            self.assertEqual(m.db.session.get(m.Supplier, self.supplier).total_orders, 1)

    def test_receipt_caps_never_truncate_and_receive_all_replays(self):
        m = self.m
        with m.app.app_context():
            m.db.session.add_all([
                m.PurchaseOrderItem(purchase_order_id=self.po, product_id=self.product,
                                    ordered_qty=1, received_qty=0, unit_cost=3)
                for _ in range(500)])
            m.db.session.commit()
            _, status = self.service_receive(items=[
                {'purchase_order_item_id': self.item, 'received_qty': 0}] * 501)
            self.assertEqual(status, 400)
            result, status = self.service_receive(receive_all=True, idempotency_key='all')
            self.assertEqual(status, 400)
            self.assertIn('500', result['message'])
            self.assertEqual(m.WarehouseInventory.query.count(), 0)
            self.assertEqual(m.ReceiptRequest.query.count(), 0)
            _, status = self.service_receive(10)
            self.assertEqual(status, 200)
            result, status = self.service_receive(receive_all=True, idempotency_key='all')
            self.assertEqual(status, 200)
            self.assertEqual(len(result['received_lines']), 500)
            self.assertEqual(result['status'], 'received')
            replay, status = self.service_receive(receive_all=True, idempotency_key='all')
            self.assertEqual(status, 200)
            self.assertTrue(replay['replayed'])
            self.assertEqual(len(replay['received_lines']), 500)
            self.assertEqual(m.WarehouseInventory.query.count(), 501)
            self.assertEqual(m.db.session.get(m.Supplier, self.supplier).total_orders, 1)

    def test_legacy_model_map_and_receive_all_need_no_key(self):
        m = self.m
        with m.app.app_context():
            result, status = receive_purchase_order(m.db, {
                'PurchaseOrder': m.PurchaseOrder, 'WarehouseInventory': m.WarehouseInventory,
            }, self.po, receive_all=True)
            self.assertEqual(status, 200)
            self.assertEqual(result['status'], 'received')
            self.assertEqual(m.ReceiptRequest.query.count(), 0)

    def test_validation_of_all_selected_lines_precedes_any_mutation(self):
        m = self.m
        with m.app.app_context():
            second = m.PurchaseOrderItem(purchase_order_id=self.po, product_id=self.product,
                                         ordered_qty=2, received_qty=0, unit_cost=6)
            m.db.session.add(second)
            m.db.session.commit()
            _, status = self.service_receive(items=[
                {'purchase_order_item_id': self.item, 'received_qty': 3},
                {'purchase_order_item_id': second.id, 'received_qty': 3},
            ], idempotency_key='all-or-nothing')
            self.assertEqual(status, 400)
            self.assertEqual(m.WarehouseInventory.query.count(), 0)
            self.assertEqual(m.ReceiptRequest.query.count(), 0)
            self.assertEqual(m.db.session.get(m.PurchaseOrderItem, self.item).received_qty, 0)
            self.assertEqual(m.db.session.get(m.Supplier, self.supplier).total_orders, 0)

    def test_partial_receipts_share_product_branch_and_count_order_once(self):
        self.assertEqual(self.receive(4).status_code, 200)
        self.assertEqual(self.receive(6).status_code, 200)
        self.assertEqual(self.receive(1).status_code, 400)
        with self.m.app.app_context():
            rows = self.m.WarehouseInventory.query.all()
            self.assertEqual(sum(row.quantity for row in rows), 10)
            self.assertTrue(all(row.branch_id == self.foreign for row in rows))
            self.assertEqual(self.m.db.session.get(self.m.Supplier, self.supplier).total_orders, 1)

    def test_invalid_and_unapproved_receipts_never_mutate(self):
        for quantity in (True, -1, 1.5, 'bad', 0, 11):
            with self.subTest(quantity=quantity):
                self.assertEqual(self.receive(quantity).status_code, 400)
        self.assertEqual(self.receive(1, items=[
            {'purchase_order_item_id': self.item, 'received_qty': 6},
            {'purchase_order_item_id': self.item, 'received_qty': 6},
        ]).status_code, 400)
        with self.m.app.app_context():
            self.assertEqual(self.m.WarehouseInventory.query.count(), 0)
            po = self.m.db.session.get(self.m.PurchaseOrder, self.po)
            po.status = 'draft'
            self.m.db.session.commit()
        self.assertEqual(self.receive(1).status_code, 400)
        self.assertEqual(self.receive(1, role='cashier').status_code, 403)

    def test_concurrent_receipts_cannot_double_receive(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda _: self.receive(6).status_code, range(2)))
        self.assertEqual(sorted(statuses), [200, 400])
        with self.m.app.app_context():
            self.assertEqual(self.m.db.session.get(self.m.PurchaseOrderItem, self.item).received_qty, 6)
            self.assertEqual(sum(row.quantity for row in self.m.WarehouseInventory.query.all()), 6)

    def test_shared_product_photo_survives_other_branch_update_and_delete(self):
        m = self.m
        with m.app.app_context():
            first = m.db.session.get(m.Product, self.product)
            first.photo_filename = 'shared.png'
            second = m.Product(name='Local photo clone', price=10, stock=0,
                               photo_filename='shared.png', branch_id=self.local)
            m.db.session.add(second)
            m.db.session.commit()
            second_id = second.id
            photo = Path(m.app.config['UPLOAD_FOLDER']) / 'shared.png'
            photo.write_bytes(b'untouched shared image')
        client = m.app.test_client()
        with client.session_transaction() as session:
            session.update(user_id=self.user, role='manager', branch_id=self.local)
        self.assertEqual(client.put(f'/api/products/{second_id}', json={'remove_photo': True}).status_code, 200)
        self.assertTrue(photo.exists())
        with m.app.app_context():
            second = m.db.session.get(m.Product, second_id)
            second.photo_filename = 'shared.png'
            m.db.session.commit()
        self.assertEqual(client.delete(f'/api/products/{second_id}').status_code, 200)
        self.assertEqual(photo.read_bytes(), b'untouched shared image')
        with client.session_transaction() as session:
            session['branch_id'] = self.foreign
        self.assertEqual(client.put(f'/api/products/{self.product}', json={'remove_photo': True}).status_code, 200)
        self.assertFalse(photo.exists())


if __name__ == '__main__':
    unittest.main()