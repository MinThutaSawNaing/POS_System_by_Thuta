"""Regression tests for the scalable POS product catalog endpoint."""

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
            {'id', 'barcode', 'name', 'price', 'stock', 'tax_rate', 'photo_url'},
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


if __name__ == '__main__':
    unittest.main()