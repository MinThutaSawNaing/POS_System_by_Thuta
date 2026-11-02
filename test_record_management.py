"""Isolated backend record-maintenance contracts (never use the live database)."""
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import event

import test_purchase_receiving as receiving_tests
from record_management import delete_sale, edit_purchase_order


class RecordManagementTests(unittest.TestCase):
    setUpClass = classmethod(receiving_tests.PurchaseReceivingTests.setUpClass.__func__)
    tearDownClass = classmethod(receiving_tests.PurchaseReceivingTests.tearDownClass.__func__)
    setUp = receiving_tests.PurchaseReceivingTests.setUp

    def client(self, role='manager', branch=None):
        m = self.m
        with m.app.app_context():
            m.db.session.get(m.User, self.user).role = role
            m.db.session.commit()
        client = m.app.test_client()
        with client.session_transaction() as session:
            session.update(user_id=self.user, role=role, branch_id=branch or self.local)
        return client

    def sale(self, transaction='RECORD-SALE', branch=None):
        m = self.m
        with m.app.app_context():
            sale = m.Sale(transaction_id=transaction, branch_id=branch or self.local,
                          user_id=self.user, total=10, tax=0, payment_method='cash')
            m.db.session.add(sale)
            m.db.session.flush()
            m.db.session.add(m.SaleItem(sale_id=sale.id, product_id=self.product,
                                      quantity=1, price=10, tax=0))
            m.db.session.commit()
            return sale.id

    def test_manager_and_boss_edit_global_po(self):
        for role in ('manager', 'boss'):
            client = self.client(role, self.foreign)
            response = client.put(f'/api/purchase_orders/{self.po}', json={
                'supplier_id': self.supplier, 'notes': role,
                'expected_delivery_date': '2027-03-02',
                'items': [{'id': self.item, 'product_id': self.product,
                           'quantity': 7, 'unit_cost': 2.5}]})
            self.assertEqual(response.status_code, 200, response.json)
            with self.m.app.app_context():
                self.assertEqual(self.m.db.session.get(self.m.PurchaseOrder, self.po).total_amount, 17.5)
                self.assertEqual(self.m.db.session.get(self.m.Product, self.product).stock, 0)

    def test_validation_is_atomic(self):
        client = self.client()
        bad = [[], [{'product_id': self.product, 'quantity': -1, 'unit_cost': 2}],
               [{'product_id': self.product, 'quantity': 2.5, 'unit_cost': 2}],
               [{'product_id': self.product, 'quantity': True, 'unit_cost': 2}],
               [{'product_id': self.product, 'quantity': 2, 'unit_cost': 'NaN'}],
               [{'id': 9999, 'product_id': self.product, 'quantity': 2, 'unit_cost': 2}],
               [{'product_id': 9999, 'quantity': 2, 'unit_cost': 2}]]
        for items in bad:
            response = client.put(f'/api/purchase_orders/{self.po}', json={'notes': 'must rollback', 'items': items})
            self.assertIn(response.status_code, (400, 404), response.json)
        for value in ('not-a-date', 123):
            self.assertEqual(client.put(f'/api/purchase_orders/{self.po}', json={
                'notes': 'must rollback', 'expected_delivery_date': value}).status_code, 400)
        with self.m.app.app_context():
            po = self.m.db.session.get(self.m.PurchaseOrder, self.po)
            self.assertIsNone(po.notes)
            self.assertEqual(po.total_amount, 30)
            self.assertEqual(self.m.db.session.get(self.m.PurchaseOrderItem, self.item).ordered_qty, 10)

    def test_cashier_denied_routes_and_service(self):
        self.sale()
        client = self.client('cashier')
        for path, method in ((f'/api/purchase_orders/{self.po}', 'put'),
                             (f'/api/purchase_orders/{self.po}', 'delete'),
                             ('/api/sales/RECORD-SALE', 'delete')):
            self.assertEqual(getattr(client, method)(path, json={'confirm': True, 'notes': 'no'}).status_code, 403)
        with self.m.app.app_context():
            result, status = edit_purchase_order(self.m.db, vars(self.m), self.po, {'notes': 'no'}, actor_id=self.user)
            self.assertEqual(status, 403)
            self.assertFalse(result['success'])
            result, status = delete_sale(self.m.db, vars(self.m), 'RECORD-SALE',
                branch_id=self.local, confirm=True, actor_id=self.user)
            self.assertEqual(status, 403)

    def test_direct_service_audit_and_malformed_json(self):
        m = self.m
        with m.app.app_context():
            result, status = edit_purchase_order(m.db, vars(m), self.po,
                {'notes': 'trusted AI context'}, actor_id=self.user)
            self.assertEqual(status, 200, result)
            log = m.AuditLog.query.filter_by(entity_type='PurchaseOrder', action='update').one()
            self.assertEqual(log.actor_user_id, self.user)
            self.assertEqual(log.actor_role, 'manager')
        client = self.client()
        for value in ([], None, 'text', {}):
            response = client.put(f'/api/purchase_orders/{self.po}', json=value)
            self.assertEqual(response.status_code, 400, response.json)

    def test_received_metadata_and_history_delete_preserve_receipts(self):
        client = self.client()
        response = client.post(f'/api/purchase_orders/{self.po}/receive', json={
            'items': [{'purchase_order_item_id': self.item, 'received_qty': 10}],
            'idempotency_key': 'record-receipt'})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(client.put(f'/api/purchase_orders/{self.po}', json={
            'notes': 'metadata', 'supplier_id': self.supplier}).status_code, 200)
        with self.m.app.app_context():
            other = self.m.Supplier(name='Different supplier', branch_id=self.local)
            self.m.db.session.add(other)
            self.m.db.session.commit()
            other_id = other.id
        self.assertEqual(client.put(f'/api/purchase_orders/{self.po}', json={
            'supplier_id': other_id}).status_code, 409)
        self.assertEqual(client.put(f'/api/purchase_orders/{self.po}', json={
            'items': [{'product_id': self.product, 'quantity': 1, 'unit_cost': 0}]}).status_code, 409)
        for confirm in (None, False, 'true', 1):
            self.assertEqual(client.delete(f'/api/purchase_orders/{self.po}', json={'confirm': confirm}).status_code, 400)
        self.assertEqual(client.delete(f'/api/purchase_orders/{self.po}', json={'confirm': True}).status_code, 200)
        with self.m.app.app_context():
            self.assertIsNone(self.m.db.session.get(self.m.PurchaseOrder, self.po))
            self.assertEqual(self.m.PurchaseOrderItem.query.count(), 0)
            self.assertEqual(self.m.WarehouseInventory.query.one().quantity, 10)
            self.assertEqual(self.m.ReceiptRequest.query.count(), 1)
            self.assertEqual(self.m.db.session.get(self.m.Product, self.product).stock, 0)
        replay = client.post(f'/api/purchase_orders/{self.po}/receive', json={
            'items': [{'purchase_order_item_id': self.item, 'received_qty': 10}],
            'idempotency_key': 'record-receipt'})
        self.assertEqual(replay.status_code, 200, replay.json)
        self.assertTrue(replay.json['replayed'])

    def test_sale_roles_confirmation_branch_and_no_stock_reversal(self):
        for role in ('manager', 'boss'):
            transaction = f'RECORD-{role}'
            self.sale(transaction)
            client = self.client(role, self.foreign)
            self.assertEqual(client.delete(f'/api/sales/{transaction}', json={'confirm': True}).status_code, 404)
            client = self.client(role)
            self.assertEqual(client.delete(f'/api/sales/{transaction}', json={}).status_code, 400)
            response = client.delete(f'/api/sales/{transaction}', json={'confirm': True})
            self.assertEqual(response.status_code, 200, response.json)
            replay = client.post('/api/sales', json={'transaction_id': transaction,
                'items': [{'product_id': self.product, 'quantity': 1, 'price': 10}],
                'payment_method': 'cash', 'cash_received': 10})
            self.assertEqual(replay.status_code, 409, replay.json)
        with self.m.app.app_context():
            self.assertEqual(self.m.Sale.query.count(), 0)
            self.assertEqual(self.m.SaleItem.query.count(), 0)
            self.assertEqual(self.m.DeletedSaleRequest.query.count(), 2)
            self.assertEqual(self.m.db.session.get(self.m.Product, self.product).stock, 0)
            self.assertGreater(self.m.AuditLog.query.filter_by(entity_type='Sale', action='delete').count(), 0)

    def test_replay_and_delete_serialize_before_idempotency_reads(self):
        """Pause a replay between guard reads; deletion must not slip between them."""
        m = self.m
        self.sale('RACING-REPLAY')
        replay_client, delete_client = self.client(), self.client()
        with m.app.app_context():
            product = m.db.session.get(m.Product, self.product)
            product.branch_id, product.stock = self.local, 10
            m.db.session.commit()
            engine = m.db.engine
        guard_read, delete_lock_attempt, deletion_finished = (threading.Event() for _ in range(3))
        deletion_slipped = []

        def after_read(connection, cursor, statement, parameters, context, executemany):
            if (threading.current_thread().name.startswith('replay') and
                    statement.lstrip().upper().startswith('SELECT') and
                    'deleted_sale_request' in statement.lower()):
                guard_read.set()
                if not delete_lock_attempt.wait(5):
                    raise AssertionError('Deletion did not attempt its writer lock')
                # With the bug, deletion commits while checkout is paused here.
                # With the fix it must wait for checkout to finish its replay.
                deletion_slipped.append(deletion_finished.wait(0.5))

        def before_write(connection, cursor, statement, parameters, context, executemany):
            if (threading.current_thread().name.startswith('delete') and
                    statement.strip().upper() == 'BEGIN IMMEDIATE'):
                delete_lock_attempt.set()

        def replay():
            response = replay_client.post('/api/sales', json={
                'transaction_id': 'RACING-REPLAY', 'payment_method': 'cash',
                'cash_received': 10,
                'items': [{'product_id': self.product, 'quantity': 1, 'price': 10}]})
            return response.status_code, response.json

        def delete():
            if not guard_read.wait(5):
                raise AssertionError('Replay did not read the tombstone guard')
            try:
                response = delete_client.delete('/api/sales/RACING-REPLAY', json={'confirm': True})
                return response.status_code, response.json
            finally:
                deletion_finished.set()

        event.listen(engine, 'after_cursor_execute', after_read)
        event.listen(engine, 'before_cursor_execute', before_write)
        try:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix='replay') as replay_pool, \
                    ThreadPoolExecutor(max_workers=1, thread_name_prefix='delete') as delete_pool:
                replay_future = replay_pool.submit(replay)
                delete_future = delete_pool.submit(delete)
                replay_status, replay_payload = replay_future.result(timeout=15)
                delete_status, delete_payload = delete_future.result(timeout=15)
            self.assertEqual(deletion_slipped, [False])
            self.assertEqual(replay_status, 200, replay_payload)
            self.assertTrue(replay_payload['duplicate'])
            self.assertEqual(delete_status, 200, delete_payload)
        finally:
            event.remove(engine, 'after_cursor_execute', after_read)
            event.remove(engine, 'before_cursor_execute', before_write)
        with m.app.app_context():
            self.assertEqual(m.Sale.query.filter_by(transaction_id='RACING-REPLAY').count(), 0)
            self.assertEqual(m.DeletedSaleRequest.query.count(), 1)
            self.assertEqual(m.db.session.get(m.Product, self.product).stock, 10)

    def test_linked_workflows_block_including_adjustment_and_intent_alias(self):
        m = self.m
        for kind in ('return', 'adjustment', 'debt', 'delivery', 'intent', 'intent_alias', 'return_item'):
            transaction = f'LINK-{kind}'
            sale_id = self.sale(transaction)
            with m.app.app_context():
                if kind in ('return', 'adjustment', 'return_item'):
                    other_id = self.sale(f'OTHER-{kind}')
                    workflow = m.ReturnExchange(workflow_id=kind, mode='return', user_id=self.user,
                        original_sale_id=sale_id if kind == 'return' else other_id,
                        adjustment_sale_id=sale_id if kind == 'adjustment' else None)
                    m.db.session.add(workflow)
                    m.db.session.flush()
                    if kind == 'return_item':
                        item = m.SaleItem.query.filter_by(sale_id=sale_id).one()
                        m.db.session.add(m.ReturnExchangeItem(return_exchange_id=workflow.id,
                            original_sale_item_id=item.id, product_id=self.product,
                            movement='return', quantity=1, unit_price=10, line_total=10, line_tax=0))
                elif kind == 'debt':
                    customer = m.Customer(name='Debtor', branch_id=self.local)
                    m.db.session.add(customer)
                    m.db.session.flush()
                    m.db.session.add(m.Debt(customer_id=customer.id, sale_id=sale_id,
                                          amount=10, balance=10, branch_id=self.local))
                elif kind == 'delivery':
                    m.db.session.add(m.Delivery(delivery_number=kind, sale_id=sale_id, branch_id=self.local))
                else:
                    from record_management import _model
                    Intent = _model(vars(m), 'PaymentIntent')
                    m.db.session.add(Intent(id=kind, order_id=kind,
                        transaction_id=transaction if kind == 'intent' else 'alias-source',
                        sale_transaction_id=transaction if kind == 'intent_alias' else None,
                        branch_id=self.local, user_id=self.user, amount='10',
                        checkout_json='{}', checkout_hash='hash'))
                m.db.session.commit()
            response = self.client().delete(f'/api/sales/{transaction}', json={'confirm': True})
            self.assertEqual(response.status_code, 409, response.json)
            self.assertIn('linked', response.json['message'])
            with m.app.app_context():
                self.assertIsNotNone(m.db.session.get(m.Sale, sale_id))
                self.assertEqual(m.SaleItem.query.filter_by(sale_id=sale_id).count(), 1)