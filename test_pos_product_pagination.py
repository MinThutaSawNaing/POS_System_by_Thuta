"""Regression tests for POS cursor and Products management pagination."""

import unittest
import uuid

from app import app, db, Branch, Category, Product, User


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
        self.category_ids = []

    def tearDown(self):
        Product.query.filter(Product.id.in_(self.product_ids or [0])).delete(
            synchronize_session=False
        )
        Category.query.filter(Category.id.in_(self.category_ids or [0])).delete(
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

    def test_subcategories_creation_validation_and_pos_filter(self):
        client = self._client()
        parent = client.post('/api/categories', json={'name': 'Shoes'}).get_json()['category']
        self.category_ids.append(parent['id'])
        child_response = client.post('/api/categories', json={'name': 'Nike', 'parent_id': parent['id']})
        self.assertEqual(child_response.status_code, 201)
        child = child_response.get_json()['category']
        self.category_ids.append(child['id'])
        self.assertEqual(child['parent_name'], 'Shoes')
        self.assertEqual(child['parent_id'], parent['id'])
        self.assertEqual(client.post('/api/categories', json={'name': 'Too deep', 'parent_id': child['id']}).status_code, 400)
        self.assertEqual(client.put(f"/api/categories/{parent['id']}", json={'name': 'Shoes', 'parent_id': child['id']}).status_code, 400)
        self.assertEqual(client.delete(f"/api/categories/{parent['id']}").status_code, 400)
        item = Product(name='Nike item', price=5, stock=2, branch_id=self.branch.id, category_id=child['id'])
        db.session.add(item)
        db.session.commit()
        self.product_ids.append(item.id)
        for category in (parent, child):
            result = client.get(f"/api/products?view=pos&category_id={category['id']}").get_json()
            self.assertEqual([row['id'] for row in result['items']], [item.id])
        self.assertEqual(client.put(f"/api/categories/{child['id']}", json={'name': 'Nike', 'parent_id': None}).status_code, 200)
        self.assertEqual(client.delete(f"/api/categories/{parent['id']}").status_code, 200)

    def test_parent_must_belong_to_active_branch(self):
        branch = Branch(name=f'Foreign {uuid.uuid4().hex}', code=f'F{uuid.uuid4().hex[:8]}', is_active=True)
        db.session.add(branch)
        db.session.flush()
        foreign = Category(name='Foreign parent', branch_id=branch.id)
        db.session.add(foreign)
        db.session.commit()
        try:
            result = self._client().post('/api/categories', json={'name': 'Foreign child', 'parent_id': foreign.id})
            self.assertEqual(result.status_code, 400)
            self.assertIsNone(Category.query.filter_by(name='Foreign child', branch_id=self.branch.id).first())
        finally:
            db.session.delete(foreign)
            db.session.delete(branch)
            db.session.commit()

    def test_pos_catalog_filters_by_category_for_the_quick_buttons(self):
        drinks = Category(name=f'Drinks {uuid.uuid4().hex[:6]}', color='#0d6efd',
                          is_active=True, branch_id=self.branch.id)
        bakery = Category(name=f'Bakery {uuid.uuid4().hex[:6]}', color='#fd7e14',
                          is_active=True, branch_id=self.branch.id)
        db.session.add_all([drinks, bakery])
        db.session.commit()
        self.category_ids = [drinks.id, bakery.id]

        cola = Product(name=f'Cola {uuid.uuid4().hex}', price=1.5, stock=12,
                       tax_rate=0, branch_id=self.branch.id, category_id=drinks.id)
        bun = Product(name=f'Bun {uuid.uuid4().hex}', price=2.0, stock=8,
                      tax_rate=0, branch_id=self.branch.id, category_id=bakery.id)
        db.session.add_all([cola, bun])
        db.session.commit()
        self.product_ids = [cola.id, bun.id]
        client = self._client()

        filtered = client.get(f'/api/products?view=pos&category_id={drinks.id}')

        self.assertEqual(filtered.status_code, 200)
        data = filtered.get_json()
        self.assertEqual([item['id'] for item in data['items']], [cola.id])
        self.assertFalse(data['has_more'])
        self.assertIsNone(data['next_cursor'])

        # A stale chip (deleted category) or a hand-edited query must be rejected
        # instead of quietly listing the entire branch.
        invalid = client.get('/api/products?view=pos&category_id=not-a-number')
        self.assertEqual(invalid.status_code, 400)

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