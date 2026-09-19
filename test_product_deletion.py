"""API regression tests for product deletion safeguards."""

import unittest
import uuid
from datetime import datetime

from app import (
    app, db, Branch, Product, Promotion, Sale, SaleItem, User,
)


class ProductDeletionTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self._ctx = app.app_context()
        self._ctx.push()
        self.branch = Branch.query.filter_by(is_active=True).first()
        self.user = User.query.filter_by(username='admin').first()
        self.product_ids = []
        self.sale_ids = []
        self.sale_item_ids = []
        self.promotion_ids = []

    def tearDown(self):
        SaleItem.query.filter(SaleItem.sale_id.in_(self.sale_ids or [0])).delete(
            synchronize_session=False
        )
        SaleItem.query.filter(SaleItem.id.in_(self.sale_item_ids or [0])).delete(
            synchronize_session=False
        )
        Sale.query.filter(Sale.id.in_(self.sale_ids or [0])).delete(
            synchronize_session=False
        )
        Promotion.query.filter(Promotion.id.in_(self.promotion_ids or [0])).delete(
            synchronize_session=False
        )
        Product.query.filter(Product.id.in_(self.product_ids or [0])).delete(
            synchronize_session=False
        )
        db.session.commit()
        self._ctx.pop()

    def _client(self):
        client = app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.user.id
            session['role'] = 'manager'
            session['branch_id'] = self.branch.id
        return client

    def _product(self, name='Delete Test Product'):
        product = Product(
            name=f'{name} {uuid.uuid4().hex}', price=100.0, stock=1,
            tax_rate=0.0, branch_id=self.branch.id,
        )
        db.session.add(product)
        db.session.commit()
        self.product_ids.append(product.id)
        return product

    def _sale_with_item(self, product, quantity=1, price=100.0, tax=0.0):
        sale = Sale(
            transaction_id=f'product-delete-{uuid.uuid4().hex}',
            total=price * quantity, tax=tax, payment_method='cash',
            user_id=self.user.id, branch_id=self.branch.id,
        )
        db.session.add(sale)
        db.session.flush()
        item = SaleItem(
            sale_id=sale.id, product_id=product.id, quantity=quantity, price=price,
            tax=tax,
        )
        db.session.add(item)
        db.session.commit()
        self.sale_ids.append(sale.id)
        self.sale_item_ids.append(item.id)
        return sale, item

    def test_manager_can_delete_unused_product(self):
        product = self._product()

        response = self._client().delete(f'/api/products/{product.id}')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['success'])
        self.assertIsNone(db.session.get(Product, product.id))

    def test_product_with_sales_history_is_not_deleted(self):
        product = self._product('Sold Product')
        self._sale_with_item(product)

        response = self._client().delete(f'/api/products/{product.id}')

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()['success'])
        self.assertIn('sales history', response.get_json()['message'])
        self.assertIsNotNone(db.session.get(Product, product.id))
        self.assertEqual(SaleItem.query.filter_by(product_id=product.id).count(), 1)

    def test_sales_history_response_asks_for_confirmation(self):
        product = self._product('Sold Product')
        self._sale_with_item(product)

        response = self._client().delete(f'/api/products/{product.id}')

        self.assertEqual(response.status_code, 400)
        body = response.get_json()
        self.assertTrue(body['has_sales_history'])
        self.assertTrue(body['requires_confirmation'])
        self.assertEqual(body['sales_history_count'], 1)

    def test_confirming_sales_history_deletes_product_and_keeps_sale(self):
        product = self._product('Sold Product')
        sale, item = self._sale_with_item(product)
        client = self._client()
        # The extra confirmation dialog is only skipped once the user accepts.
        client.delete(f'/api/products/{product.id}')

        response = client.delete(f'/api/products/{product.id}?force=1')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['success'])
        self.assertIsNone(db.session.get(Product, product.id))
        item = db.session.get(SaleItem, item.id)
        self.assertIsNone(item.product_id)
        self.assertEqual(item.quantity, 1)
        self.assertEqual(item.price, 100.0)
        self.assertEqual(db.session.get(Sale, sale.id).total, 100.0)

    def test_confirmed_deletion_keeps_sales_history_view_working(self):
        product = self._product('Sold Product')
        sale, item = self._sale_with_item(product)
        client = self._client()

        client.delete(f'/api/products/{product.id}?force=1')

        response = client.get(f'/api/sales/{sale.transaction_id}')

        self.assertEqual(response.status_code, 200)
        items = response.get_json()['items']
        line = next(row for row in items if row['sale_item_id'] == item.id)
        self.assertEqual(line['name'], 'Deleted product')
        self.assertEqual(line['price'], 100.0)
        self.assertEqual(line['available_return_quantity'], 0)

    def test_forced_delete_is_still_blocked_by_other_references(self):
        product = self._product('Promoted Product')
        promotion = Promotion(
            product_id=product.id, discount_type='percent', discount_value=10,
            start_date=datetime.utcnow(), end_date=datetime.utcnow(),
        )
        db.session.add(promotion)
        db.session.commit()
        self.promotion_ids.append(promotion.id)

        response = self._client().delete(f'/api/products/{product.id}?force=1')

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()['success'])
        self.assertIn('promotion records', response.get_json()['message'])
        self.assertIsNotNone(db.session.get(Product, product.id))


if __name__ == '__main__':
    unittest.main()
