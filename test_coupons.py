"""Backend contract tests for the Coupons feature (CRUD, validate, checkout)."""

import unittest
import uuid
from datetime import datetime, timedelta

import pytz

from app import Branch, Coupon, Product, Sale, SaleItem, User, app, db


class CouponsTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.context = app.app_context()
        self.context.push()
        self.user = User.query.filter_by(username='admin').first()
        self.branch = Branch.query.filter_by(is_active=True).first()
        self.product = Product(
            name='Coupon test product', price=10000, stock=50,
            tax_rate=0, branch_id=self.branch.id,
            barcode='CPN-' + uuid.uuid4().hex[:12],
        )
        db.session.add(self.product)
        db.session.commit()

    def tearDown(self):
        sale_ids = [row.sale_id for row in SaleItem.query.filter_by(product_id=self.product.id).all()]
        SaleItem.query.filter(SaleItem.sale_id.in_(sale_ids or [0])).delete(synchronize_session=False)
        Sale.query.filter(Sale.id.in_(sale_ids or [0])).delete(synchronize_session=False)
        for code in (
            'DISCOUNT10', 'DISCOUNT5', 'ONETIME', 'MINSPEND', 'EXPIRED',
            'EXHAUSTED', 'TAKE5', 'RENAMED99',
        ):
            Coupon.query.filter_by(code=code).delete(synchronize_session=False)
        db.session.delete(self.product)
        db.session.commit()
        self.context.pop()

    def client(self, role='manager'):
        client = app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.user.id
            session['role'] = role
            session['branch_id'] = self.branch.id
        return client

    def create_coupon(self, code='DISCOUNT10', **overrides):
        payload = {
            'code': code,
            'name': 'Ten percent off',
            'discount_type': 'percent',
            'discount_value': 10,
            'coupon_kind': 'time_based',
            'is_active': 'true',
        }
        payload.update(overrides)
        return self.client().post('/api/coupons', json=payload)

    def sale_payload(self, coupon_code=None, price=10000):
        payload = {
            'items': [{'product_id': self.product.id, 'price': price, 'quantity': 1, 'tax_rate': 0}],
            'payment_method': 'cash',
            'cash_received': price,
        }
        if coupon_code:
            payload['coupon_code'] = coupon_code
        return payload

    def test_cashier_cannot_manage_coupons(self):
        response = self.client(role='cashier').get('/api/coupons')
        self.assertEqual(response.status_code, 403)
        response = self.client(role='cashier').post('/api/coupons', json={'code': 'X'})
        self.assertEqual(response.status_code, 403)

    def test_create_list_and_fetch_single(self):
        response = self.create_coupon()
        self.assertEqual(response.status_code, 201)
        listing = self.client().get('/api/coupons')
        self.assertEqual(listing.status_code, 200)
        matching = [item for item in listing.get_json() if item['code'] == 'DISCOUNT10']
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]['discount_value'], 10.0)
        self.assertEqual(matching[0]['coupon_kind'], 'time_based')
        detail = self.client().get('/api/coupons/%d' % matching[0]['id'])
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.get_json()['code'], 'DISCOUNT10')

    def test_duplicate_code_rejected_and_code_uppercased(self):
        response = self.create_coupon(code='discount5')
        self.assertEqual(response.status_code, 201)
        response = self.create_coupon(code='DISCOUNT5')
        self.assertEqual(response.status_code, 400)
        self.assertIn('already exists', response.get_json()['message'])

    def test_one_time_kind_forces_single_use(self):
        response = self.create_coupon(code='ONETIME', coupon_kind='one_time', usage_limit=100)
        self.assertEqual(response.status_code, 201)
        listing = self.client().get('/api/coupons')
        details = [item for item in listing.get_json() if item['code'] == 'ONETIME'][0]
        self.assertEqual(details['usage_limit'], 1)

    def test_validate_enforces_min_subtotal_expiry_and_usage_limit(self):
        past = (datetime.now(pytz.timezone('Asia/Yangon')) - timedelta(days=1)).isoformat()
        self.create_coupon(code='MINSPEND', min_subtotal=5000)
        response = self.client().post('/api/coupons/validate',
                                      json={'code': 'MINSPEND', 'subtotal': 3000})
        self.assertEqual(response.status_code, 400)
        self.assertIn('Minimum subtotal', response.get_json()['message'])
        self.create_coupon(code='EXPIRED', discount_type='fixed', discount_value=100,
                           end_date=past)
        response = self.client().post('/api/coupons/validate',
                                      json={'code': 'EXPIRED', 'subtotal': 10000})
        self.assertEqual(response.status_code, 400)
        self.create_coupon(code='EXHAUSTED', coupon_kind='limited',
                           discount_type='fixed', discount_value=100, usage_limit=3)
        exhausted = Coupon.query.filter_by(code='EXHAUSTED').first()
        exhausted.used_count = 3
        db.session.commit()
        response = self.client().post('/api/coupons/validate',
                                      json={'code': 'EXHAUSTED', 'subtotal': 10000})
        self.assertEqual(response.status_code, 400)
        self.assertIn('usage limit', response.get_json()['message'])

    def test_update_and_delete_coupon(self):
        self.create_coupon(code='RENAMED99')
        listing = self.client().get('/api/coupons')
        coupon_id = [item for item in listing.get_json() if item['code'] == 'RENAMED99'][0]['id']
        response = self.client().put('/api/coupons/%d' % coupon_id,
                                     json={'discount_value': 25, 'name': 'Quarter off'})
        self.assertEqual(response.status_code, 200)
        detail = self.client().get('/api/coupons/%d' % coupon_id)
        self.assertEqual(detail.get_json()['discount_value'], 25.0)
        response = self.client().delete('/api/coupons/%d' % coupon_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client().get('/api/coupons/%d' % coupon_id).status_code, 404)

    def test_sale_applies_coupon_discount_and_records_usage(self):
        self.create_coupon(code='DISCOUNT10', discount_type='percent', discount_value=10,
                           coupon_kind='one_time')
        response = self.client().post('/api/sales', json=self.sale_payload('DISCOUNT10'))
        self.assertEqual(response.status_code, 201, response.get_json())
        transaction_id = response.get_json()['transaction_id']
        sale = Sale.query.filter_by(transaction_id=transaction_id).first()
        self.assertEqual(sale.total, 9000.0)
        self.assertEqual(sale.coupon_code, 'DISCOUNT10')
        coupon = Coupon.query.filter_by(code='DISCOUNT10').first()
        self.assertEqual(coupon.used_count, 1)
        # one_time coupon must now reject a second checkout
        response = self.client().post('/api/sales', json=self.sale_payload('DISCOUNT10'))
        self.assertEqual(response.status_code, 400)
        self.assertIn('limit', response.get_json()['message'])

    def test_sale_rejects_unknown_coupon(self):
        response = self.client().post('/api/sales', json=self.sale_payload('NOPE1'))
        self.assertEqual(response.status_code, 400)
        self.assertIn('Invalid coupon code', response.get_json()['message'])


if __name__ == '__main__':
    unittest.main()


    def test_validate_returns_discount_for_percent_and_fixed(self):
        self.create_coupon(code='DISCOUNT10', discount_type='percent', discount_value=10)
        self.create_coupon(code='TAKE5', discount_type='fixed', discount_value=500)
        response = self.client().post('/api/coupons/validate',
                                      json={'code': 'DISCOUNT10', 'subtotal': 10000})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['discount'], '1000.00')
        response = self.client().post('/api/coupons/validate',
                                      json={'code': 'take5', 'subtotal': 10000})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['discount'], '500.00')
