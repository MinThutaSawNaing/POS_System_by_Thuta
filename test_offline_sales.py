"""Idempotency tests for offline sale support (client-supplied transaction_id)."""

import unittest
import uuid

from app import app, db, Branch, Product, Sale, User


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
            Sale.query.filter(Sale.transaction_id.like('offline-%')).delete()
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


if __name__ == '__main__':
    unittest.main()
