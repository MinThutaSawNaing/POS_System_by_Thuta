"""Backend contract tests for split POS payments."""

import json
import unittest
import uuid

from app import AppSetting, Branch, Product, Sale, SaleItem, User, app, db


class SplitPaymentTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.context = app.app_context()
        self.context.push()
        self.user = User.query.filter_by(username='admin').first()
        self.branch = Branch.query.filter_by(is_active=True).first()
        self.product = Product(
            name='Split payment test product', price=10000, stock=10,
            tax_rate=0, branch_id=self.branch.id,
            barcode='SPLIT-' + uuid.uuid4().hex[:10],
        )
        db.session.add(self.product)
        db.session.commit()

    def tearDown(self):
        sale_ids = [row.sale_id for row in SaleItem.query.filter_by(product_id=self.product.id).all()]
        SaleItem.query.filter(SaleItem.sale_id.in_(sale_ids or [0])).delete(synchronize_session=False)
        Sale.query.filter(Sale.id.in_(sale_ids or [0])).delete(synchronize_session=False)
        db.session.delete(self.product)
        db.session.commit()
        self.context.pop()

    def client(self):
        client = app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.user.id
            session['role'] = 'manager'
            session['branch_id'] = self.branch.id
        return client

    def payload(self, breakdown):
        return {
            'items': [{'product_id': self.product.id, 'price': 10000, 'quantity': 1, 'tax_rate': 0}],
            'payment_method': 'split_payment',
            'payment_breakdown': breakdown,
        }

    def test_cash_and_mobile_split_is_saved_and_returned(self):
        response = self.client().post('/api/sales', json=self.payload({'cash': 5000, 'mobile_payment': 5000}))
        self.assertEqual(response.status_code, 201)
        with app.app_context():
            sale = Sale.query.filter_by(transaction_id=response.get_json()['transaction_id']).first()
            self.assertEqual(sale.payment_method, 'split_payment')
            self.assertEqual(sale.cash_received, 5000)
            self.assertEqual(json.loads(sale.payment_breakdown), {'cash': 5000.0, 'mobile_payment': 5000.0})
        details = self.client().get('/api/sales/' + response.get_json()['transaction_id'])
        self.assertEqual(details.status_code, 200)
        self.assertEqual(details.get_json()['payment_breakdown']['mobile_payment'], 5000.0)

    def test_split_must_equal_total_and_use_distinct_methods(self):
        client = self.client()
        for breakdown in ({'cash': 5000, 'mobile_payment': 4000}, {'cash': 5000}, {'cash': 5000, 'cash': 5000}):
            response = client.post('/api/sales', json=self.payload(breakdown))
            self.assertEqual(response.status_code, 400)

    def test_split_rejects_debt_and_negative_amounts(self):
        client = self.client()
        response = client.post('/api/sales', json=self.payload({'cash': 5000, 'debt': 5000}))
        self.assertEqual(response.status_code, 400)
        response = client.post('/api/sales', json={**self.payload({'cash': 5000, 'mobile_payment': 5000}), 'customer_id': 1})
        self.assertEqual(response.status_code, 400)
        response = client.post('/api/sales', json=self.payload({'cash': -1, 'mobile_payment': 10001}))
        self.assertEqual(response.status_code, 400)

    def test_split_rejects_keys_that_normalize_to_the_same_method(self):
        response = self.client().post('/api/sales', json=self.payload({'cash': 5000, ' CASH ': 5000}))
        self.assertEqual(response.status_code, 400)


if __name__ == '__main__':
    unittest.main()