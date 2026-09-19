"""Idempotency tests for offline sale support (client-supplied transaction_id)."""

import unittest
import uuid

from app import app, db, Branch, Product, Sale, SaleItem, User


class OfflineSaleTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        with app.app_context():
            self.user_id = User.query.filter_by(username='admin').first().id
            self.branch_id = Branch.query.filter_by(is_active=True).first().id
            self.client_txn_id = 'offline-' + uuid.uuid4().hex
            self.product = Product(
                barcode='OFF-' + uuid.uuid4().hex[:10],
                name='Offline Test Product',
                price=1000.0,
                cost=500.0,
                stock=10,
                tax_rate=0.0,
                branch_id=self.branch_id,
            )
            db.session.add(self.product)
            db.session.commit()
            self.product_id = self.product.id

    def tearDown(self):
        with app.app_context():
            # Every sale this test creates belongs to its throwaway product,
            # and a server-generated transaction_id cannot be matched by
            # prefix, so collect sale ids from the product's items first.
            # Sale rows must take their sale items with them: SQLite reuses row
            # ids, so a leaked sale_item row would otherwise attach itself to a
            # completely unrelated sale created later.
            stale_sale_ids = {
                item.sale_id for item in
                SaleItem.query.filter_by(product_id=self.product_id).all()
            }
            stale_sale_ids.update(
                sale.id for sale in
                Sale.query.filter(Sale.transaction_id.like('offline-%')).all()
            )
            SaleItem.query.filter(
                SaleItem.sale_id.in_(stale_sale_ids or {0})
            ).delete(synchronize_session=False)
            Sale.query.filter(Sale.id.in_(stale_sale_ids or {0})).delete(
                synchronize_session=False)
            product = db.session.get(Product, self.product_id)
            if product:
                db.session.delete(product)
            db.session.commit()

    def _client(self):
        client = app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.user_id
            session['role'] = 'manager'
            session['branch_id'] = self.branch_id
        return client

    def _sale_payload(self, transaction_id=None):
        payload = {
            'items': [{'product_id': self.product_id, 'price': 1000.0, 'quantity': 1, 'tax_rate': 0}],
            'payment_method': 'cash',
            'cash_received': 1000.0,
        }
        if transaction_id:
            payload['transaction_id'] = transaction_id
        return payload

    def test_sale_with_new_transaction_id_creates_sale(self):
        response = self._client().post('/api/sales', json=self._sale_payload(self.client_txn_id))
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.get_json()['success'])
        self.assertEqual(response.get_json()['transaction_id'], self.client_txn_id)
        with app.app_context():
            self.assertEqual(
                Sale.query.filter_by(transaction_id=self.client_txn_id).count(), 1)

    def test_sale_replay_same_transaction_id_returns_existing(self):
        client = self._client()
        first = client.post('/api/sales', json=self._sale_payload(self.client_txn_id))
        self.assertEqual(first.status_code, 201)
        replay = client.post('/api/sales', json=self._sale_payload(self.client_txn_id))
        self.assertEqual(replay.status_code, 200)
        data = replay.get_json()
        self.assertTrue(data['success'])
        self.assertTrue(data.get('duplicate'))
        self.assertEqual(data['transaction_id'], self.client_txn_id)
        with app.app_context():
            self.assertEqual(
                Sale.query.filter_by(transaction_id=self.client_txn_id).count(), 1)
            product = db.session.get(Product, self.product_id)
            self.assertEqual(product.stock, 9)  # replay must not decrement stock again

    def test_sale_without_transaction_id_unchanged(self):
        response = self._client().post('/api/sales', json=self._sale_payload())
        self.assertEqual(response.status_code, 201)
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertNotIn('offline-', data['transaction_id'])  # server-generated uuid

    def test_sale_insufficient_stock_rejected_and_not_persisted(self):
        response = self._client().post('/api/sales', json={
            'transaction_id': self.client_txn_id,
            'items': [{'product_id': self.product_id, 'price': 1000.0, 'quantity': 99, 'tax_rate': 0}],
            'payment_method': 'cash',
            'cash_received': 99000.0,
        })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()['success'])
        with app.app_context():
            self.assertEqual(
                Sale.query.filter_by(transaction_id=self.client_txn_id).count(), 0)

    def test_sale_replay_response_has_created_at_iso(self):
        # First POST creates the sale; capture the original created_at from the
        # persisted row. The replay response must echo back the same timestamp
        # so clients can rely on idempotent timestamps across retries.
        client = self._client()
        first = client.post('/api/sales', json=self._sale_payload(self.client_txn_id))
        self.assertEqual(first.status_code, 201)
        with app.app_context():
            original_sale = Sale.query.filter_by(transaction_id=self.client_txn_id).first()
            self.assertIsNotNone(original_sale)
            self.assertIsNotNone(original_sale.date)
            original_created_at = original_sale.date.isoformat()

        replay = client.post('/api/sales', json=self._sale_payload(self.client_txn_id))
        self.assertEqual(replay.status_code, 200)
        replay_data = replay.get_json()
        self.assertIn('created_at', replay_data)
        self.assertIsInstance(replay_data['created_at'], str)
        self.assertTrue(replay_data['created_at'],
                        'replay response created_at must be a non-empty string')
        # Idempotency: the replay must report the SAME created_at as the
        # original sale (not a fresh "now"), so a retried client never sees
        # the sale's timestamp move forward.
        self.assertEqual(replay_data['created_at'], original_created_at)

    def test_reports_and_sales_history_serialize_non_cash_sale(self):
        """A nullable cash_received value must not make the shared reports API
        return 500. Sales History sends page/per_page, while Reports sends the
        same route unpaginated; both response shapes must remain usable."""
        report_txn_id = 'offline-report-' + uuid.uuid4().hex
        with app.app_context():
            db.session.add(Sale(
                transaction_id=report_txn_id,
                total=1000.0,
                tax=0.0,
                cash_received=None,
                refund_amount=0.0,
                payment_method='debt',
                user_id=self.user_id,
                branch_id=self.branch_id,
            ))
            db.session.commit()

        client = self._client()
        # Sales History tab: paginated response.
        paginated = client.get('/api/reports/sales?page=1&per_page=20')
        self.assertEqual(paginated.status_code, 200)
        paginated_data = paginated.get_json()
        self.assertIsInstance(paginated_data, dict)
        paginated_sale = next(s for s in paginated_data['items'] if s['transaction_id'] == report_txn_id)
        self.assertEqual(paginated_sale['cash_received'], 0.0)

        # Reports tab: unpaginated response.
        report = client.get('/api/reports/sales')
        self.assertEqual(report.status_code, 200)
        report_data = report.get_json()
        self.assertIsInstance(report_data, list)
        report_sale = next(s for s in report_data if s['transaction_id'] == report_txn_id)
        self.assertEqual(report_sale['cash_received'], 0.0)

    def test_synced_offline_sale_prints_server_receipt(self):
        """API layer: after an offline sale syncs, the server receipt the client
        falls back to must render with the real items and total."""
        client = self._client()
        created = client.post('/api/sales', json=self._sale_payload(self.client_txn_id))
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.get_json()['transaction_id'], self.client_txn_id)

        receipt = client.get(f'/api/sales/{self.client_txn_id}/print')
        self.assertEqual(receipt.status_code, 200)
        self.assertIn('text/html', receipt.headers.get('Content-Type', ''))
        body = receipt.get_data(as_text=True)
        self.assertIn('Offline Test Product', body)
        # Server money formatting includes thousands separators.
        self.assertIn('1,000.00', body)
        self.assertIn('TOTAL', body)
        self.assertNotIn('Sale not found', body)

    def test_sale_replay_message_indicates_already_synced(self):
        # The replay response's message must indicate the sale was already
        # synced, so the client can recognise the duplicate without parsing
        # the duplicate flag.
        client = self._client()
        first = client.post('/api/sales', json=self._sale_payload(self.client_txn_id))
        self.assertEqual(first.status_code, 201)
        replay = client.post('/api/sales', json=self._sale_payload(self.client_txn_id))
        self.assertEqual(replay.status_code, 200)
        replay_data = replay.get_json()
        self.assertIn('message', replay_data)
        self.assertIsInstance(replay_data['message'], str)
        self.assertIn('sync', replay_data['message'].lower())


if __name__ == '__main__':
    unittest.main()
