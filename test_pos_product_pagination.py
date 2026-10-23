"""Regression tests for POS cursor and Products management pagination."""

import unittest
import uuid

from app import app, db, Branch, Product, User


class PosProductPaginationTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self._ctx = app.app_context()
        self._ctx.push()
        suffix = uuid.uuid4().hex[:8]
        self.branch = Branch(name=f'POS Pagination {suffix}', code=f'PP{suffix}',
                             is_active=True, is_default=False)
        db.session.add(self.branch)
        db.session.commit()
        self.user = User.query.filter_by(username='admin').first()
        self.product_ids = []

    def tearDown(self):
        Product.query.filter(Product.id.in_(self.product_ids or [0])).delete(
            synchronize_session=False
        )
        db.session.delete(self.branch)
        db.session.commit()
        self._ctx.pop()

    def _client(self):
        client = app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.user.id
            session['role'] = 'manager'
            session['branch_id'] = self.branch.id
        return client

    def test_pos_catalog_uses_cursor_pagination_without_total_count(self):
        products = [
            Product(
                name=f'POS Catalog {uuid.uuid4().hex}', price=10.0, stock=5,
                tax_rate=0.0, branch_id=self.branch.id,
            )
            for _ in range(51)
        ]
        db.session.add_all(products)
        db.session.commit()
        self.product_ids = [product.id for product in products]

        first = self._client().get('/api/products?view=pos&per_page=50')

        self.assertEqual(first.status_code, 200)
        first_data = first.get_json()
        self.assertEqual(len(first_data['items']), 50)
        self.assertTrue(first_data['has_more'])
        self.assertNotIn('total', first_data)
        self.assertNotIn('total_pages', first_data)
        self.assertIsNotNone(first_data['next_cursor'])
        self.assertEqual(
            set(first_data['items'][0]),
            {'id', 'barcode', 'name', 'price', 'stock', 'tax_rate', 'unit_symbol', 'photo_url'},
        )

        second = self._client().get(
            f"/api/products?view=pos&per_page=50&cursor={first_data['next_cursor']}"
        )

        self.assertEqual(second.status_code, 200)
        second_data = second.get_json()
        self.assertEqual(len(second_data['items']), 1)
        self.assertFalse(second_data['has_more'])
        self.assertIsNone(second_data['next_cursor'])
        self.assertFalse({item['id'] for item in first_data['items']} &
                         {item['id'] for item in second_data['items']})

    def test_management_catalog_serves_thirty_products_then_remainder(self):
        products = [
            Product(name=f'Management Widget {index}', price=10, stock=5,
                    tax_rate=0, branch_id=self.branch.id)
            for index in range(31)
        ]
        db.session.add_all(products)
        db.session.commit()
        self.product_ids = [product.id for product in products]
        client = self._client()

        first = client.get('/api/products?page=1&per_page=30&q=Management%20Widget')
        self.assertEqual(first.status_code, 200)
        first_data = first.get_json()
        self.assertEqual(len(first_data['items']), 30)
        self.assertEqual(first_data['per_page'], 30)
        self.assertEqual(first_data['total'], 31)
        self.assertEqual(first_data['total_pages'], 2)
        self.assertEqual([item['id'] for item in first_data['items']],
                         list(reversed(self.product_ids[1:])))

        second = client.get('/api/products?page=2&per_page=30&q=Management%20Widget')
        self.assertEqual(second.status_code, 200)
        second_data = second.get_json()
        self.assertEqual(second_data['page'], 2)
        self.assertEqual([item['id'] for item in second_data['items']],
                         self.product_ids[:1])


if __name__ == '__main__':
    unittest.main()