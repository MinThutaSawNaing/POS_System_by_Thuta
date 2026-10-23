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

    def receive(self, quantity, role='manager', **changes):
        client = self.m.app.test_client()
        with client.session_transaction() as session:
            session.update(user_id=self.user, role=role, branch_id=self.local)
        data = {'items': [{'purchase_order_item_id': self.item, 'received_qty': quantity}]}
        data.update(changes)
        return client.post(f'/api/purchase_orders/{self.po}/receive', json=data)

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