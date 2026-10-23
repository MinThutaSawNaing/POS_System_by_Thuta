"""Delivery fees charged with a sale (already-paid deliveries) — money flow.

An "already paid" delivery fee is real revenue: it must land in `sale.total`
(sales history, database, receipt) and must never be collected twice.
"""

import unittest
import uuid

from app import Branch, Delivery, Product, Sale, SaleItem, User, app, db, get_currency_suffix


class DeliveryFeeChargeTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        with app.app_context():
            self.user_id = User.query.filter_by(username='admin').first().id
            self.branch_id = Branch.query.filter_by(is_active=True).first().id
            self.product = Product(
                barcode='FEE-' + uuid.uuid4().hex[:10],
                name='Delivery Fee Test Product',
                price=1000.0,
                cost=500.0,
                stock=20,
                tax_rate=0.0,
                branch_id=self.branch_id,
            )
            db.session.add(self.product)
            db.session.commit()
            self.product_id = self.product.id
            self.currency_suffix = get_currency_suffix()

    def tearDown(self):
        with app.app_context():
            sale_ids = {
                item.sale_id for item in
                SaleItem.query.filter_by(product_id=self.product_id).all()
            }
            Delivery.query.filter(Delivery.sale_id.in_(sale_ids or {0})).delete(
                synchronize_session=False)
            SaleItem.query.filter(SaleItem.sale_id.in_(sale_ids or {0})).delete(
                synchronize_session=False)
            Sale.query.filter(Sale.id.in_(sale_ids or {0})).delete(
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

    def _payload(self, payment_status, *, fee=500.0, payment_method='cash',
                 cash_received=2600.0, quantity=2):
        payload = {
            'transaction_id': 'fee-' + uuid.uuid4().hex,
            'items': [{
                'product_id': self.product_id,
                'price': 1000.0,
                'quantity': quantity,
                'tax_rate': 0,
            }],
            'payment_method': payment_method,
            'delivery': {
                'enabled': True,
                'recipient_name': 'Fee Recipient',
                'recipient_phone': '09123456789',
                'delivery_address': 'Fee Street 1',
                'delivery_fee': fee,
                'payment_status': payment_status,
            },
        }
        if payment_method == 'cash':
            payload['cash_received'] = cash_received
        return payload

    def test_already_paid_fee_is_added_to_sale_total(self):
        response = self._client().post('/api/sales', json=self._payload('paid'))
        self.assertEqual(response.status_code, 201, response.get_json())
        transaction_id = response.get_json()['transaction_id']

        sale = self._client().get(f'/api/sales/{transaction_id}').get_json()
        # 2 x 1000 goods + 500 delivery fee, all paid at the counter.
        self.assertEqual(sale['total'], 2500.0)
        self.assertEqual(sale['delivery']['payment_status'], 'paid')
        self.assertTrue(sale['delivery']['fee_included_in_sale'])
        self.assertEqual(sale['delivery']['delivery_fee'], 500.0)
        self.assertEqual(sale['delivery']['order_total'], 2000.0)
        self.assertEqual(sale['delivery']['bill_total'], 2500.0)
        self.assertEqual(sale['delivery']['collect_total'], 0.0)
        self.assertEqual(sale['delivery']['paid_total'], 2500.0)

        history = self._client().get('/api/sales').get_json()['items']
        self.assertIn(
            (transaction_id, 2500.0),
            [(row['transaction_id'], row['total']) for row in history],
        )

    def test_already_paid_fee_is_printed_on_the_sale_receipt(self):
        response = self._client().post('/api/sales', json=self._payload('paid'))
        self.assertEqual(response.status_code, 201)
        transaction_id = response.get_json()['transaction_id']

        receipt = self._client().get(f'/api/sales/{transaction_id}/print')
        self.assertEqual(receipt.status_code, 200)
        html = receipt.get_data(as_text=True)
        self.assertIn('Delivery fee', html)
        self.assertIn(f'500.00 {self.currency_suffix}', html)
        self.assertIn(f'2,500.00 {self.currency_suffix}', html)

    def test_cash_on_delivery_fee_stays_out_of_the_sale_total(self):
        response = self._client().post(
            '/api/sales',
            json=self._payload('cash_on_delivery', cash_received=2000.0),
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        transaction_id = response.get_json()['transaction_id']

        sale = self._client().get(f'/api/sales/{transaction_id}').get_json()
        self.assertEqual(sale['total'], 2000.0)
        self.assertFalse(sale['delivery']['fee_included_in_sale'])
        self.assertEqual(sale['delivery']['collect_total'], 2500.0)

        slip = self._client().get(
            f"/api/deliveries/{sale['delivery']['id']}/print").get_data(as_text=True)
        self.assertIn(f'2,500.00 {self.currency_suffix}', slip)
        self.assertIn('CASH ON DELIVERY', slip)

    def test_credit_sale_cannot_be_marked_already_paid(self):
        response = self._client().post(
            '/api/sales', json=self._payload('paid', payment_method='debt'))
        self.assertEqual(response.status_code, 400)
        self.assertIn('credit sale', response.get_json()['message'].lower())

    def test_invalid_delivery_fee_is_rejected(self):
        response = self._client().post('/api/sales', json=self._payload('paid', fee='abc'))
        self.assertEqual(response.status_code, 400)
        self.assertIn('number', response.get_json()['message'].lower())

    def test_charged_fee_cannot_be_reverted_or_edited(self):
        response = self._client().post('/api/sales', json=self._payload('paid'))
        self.assertEqual(response.status_code, 201)
        transaction_id = response.get_json()['transaction_id']
        delivery_id = self._client().get(
            f'/api/sales/{transaction_id}').get_json()['delivery']['id']

        reverted = self._client().put(
            f'/api/deliveries/{delivery_id}', json={'payment_status': 'cash_on_delivery'})
        self.assertEqual(reverted.status_code, 400)
        self.assertIn('already charged', reverted.get_json()['message'])

        edited = self._client().put(
            f'/api/deliveries/{delivery_id}', json={'delivery_fee': 900})
        self.assertEqual(edited.status_code, 400)
        self.assertIn('cannot be edited', edited.get_json()['message'])

        with app.app_context():
            sale = Sale.query.filter_by(transaction_id=transaction_id).first()
            self.assertEqual(sale.total, 2500.0)


if __name__ == '__main__':
    unittest.main()
