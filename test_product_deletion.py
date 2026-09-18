"""API regression tests for product deletion safeguards."""

import unittest
import uuid

from app import app, db, Branch, Product, Sale, SaleItem, User


class ProductDeletionTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self._ctx = app.app_context()
        self._ctx.push()
        self.branch = Branch.query.filter_by(is_active=True).first()
        self.user = User.query.filter_by(username='admin').first()
        self.product_ids = []
        self.sale_ids = []

    def tearDown(self):
        SaleItem.query.filter(SaleItem.sale_id.in_(self.sale_ids or [0])).delete(
            synchronize_session=False
        )
        Sale.query.filter(Sale.id.in_(self.sale_ids or [0])).delete(
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

    def test_manager_can_delete_unused_product(self):
        product = self._product()

        response = self._client().delete(f'/api/products/{product.id}')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['success'])
        self.assertIsNone(db.session.get(Product, product.id))

    def test_product_with_sales_history_is_not_deleted(self):
        product = self._product('Sold Product')
        sale = Sale(
            transaction_id=f'product-delete-{uuid.uuid4().hex}', total=100.0,
            tax=0.0, payment_method='cash', user_id=self.user.id,
            branch_id=self.branch.id,
        )
        db.session.add(sale)
        db.session.flush()
        db.session.add(SaleItem(
            sale_id=sale.id, product_id=product.id, quantity=1, price=100.0,
            tax=0.0,
        ))
        db.session.commit()
        self.sale_ids.append(sale.id)

        response = self._client().delete(f'/api/products/{product.id}')

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()['success'])
        self.assertIn('sales history', response.get_json()['message'])
        self.assertIsNotNone(db.session.get(Product, product.id))
        self.assertEqual(SaleItem.query.filter_by(product_id=product.id).count(), 1)


if __name__ == '__main__':
    unittest.main()