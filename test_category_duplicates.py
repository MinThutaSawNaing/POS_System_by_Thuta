"""Subcategory duplicate names: reuse across shelves needs a confirmation."""

import unittest
import uuid

from app import app, db, Branch, Category, User


class CategoryDuplicateNameTests(unittest.TestCase):
    """``256GB`` may exist for two brands, but only after the manager agrees."""

    def setUp(self):
        app.config.update(TESTING=True)
        self._ctx = app.app_context()
        self._ctx.push()
        suffix = uuid.uuid4().hex[:8]
        self.branch = Branch(name=f'Category duplicates {suffix}', code=f'CD{suffix}',
                             is_active=True, is_default=False)
        db.session.add(self.branch)
        db.session.commit()
        self.user = User.query.filter_by(username='admin').first()
        self.category_ids = []

    def tearDown(self):
        # Children before parents so the self-referencing foreign key stays happy.
        for category_id in reversed(self.category_ids):
            category = db.session.get(Category, category_id)
            if category is not None:
                db.session.delete(category)
                db.session.commit()
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

    def _category(self, client, name, parent_id=None):
        response = client.post('/api/categories', json={'name': name, 'parent_id': parent_id})
        self.assertEqual(response.status_code, 201, response.get_json())
        category = response.get_json()['category']
        self.category_ids.append(category['id'])
        return category

    def test_duplicate_leaf_name_asks_first_and_then_saves(self):
        client = self._client()
        electronics = self._category(client, 'Electronics')
        ios = self._category(client, 'IOS', electronics['id'])
        iphone = self._category(client, 'Iphone', ios['id'])
        iphone_series = self._category(client, 'Iphone18series', iphone['id'])
        first = self._category(client, '256GB', iphone_series['id'])

        huawei = self._category(client, 'Huawei', electronics['id'])
        phones = self._category(client, 'Phones', huawei['id'])
        p90 = self._category(client, 'P90', phones['id'])

        pending = client.post('/api/categories', json={'name': '256GB', 'parent_id': p90['id']})
        self.assertEqual(pending.status_code, 409)
        body = pending.get_json()
        self.assertTrue(body['duplicate'])
        self.assertIn('(Electronics / IOS / Iphone / Iphone18series)', body['message'])
        self.assertIn('Are you willing to save it?', body['message'])
        self.assertEqual(body['conflicts'][0]['id'], first['id'])
        self.assertIsNone(Category.query.filter_by(
            name='256GB', parent_id=p90['id'], branch_id=self.branch.id).first())

        confirmed = client.post('/api/categories', json={
            'name': '256GB', 'parent_id': p90['id'], 'confirm_duplicate': True})
        self.assertEqual(confirmed.status_code, 201, confirmed.get_json())
        saved = confirmed.get_json()['category']
        self.category_ids.append(saved['id'])
        self.assertEqual(saved['parent_name'], 'P90')
        self.assertEqual(saved['path'], 'Electronics / Huawei / Phones / P90 / 256GB')
        self.assertEqual(Category.query.filter_by(
            name='256GB', branch_id=self.branch.id).count(), 2)

    def test_same_shelf_and_top_level_duplicates_stay_blocked(self):
        client = self._client()
        electronics = self._category(client, 'Electronics')
        ios = self._category(client, 'IOS', electronics['id'])
        self._category(client, '256GB', ios['id'])

        # Two ``256GB`` leaves on the same shelf would be indistinguishable.
        same_shelf = client.post('/api/categories', json={
            'name': '256GB', 'parent_id': ios['id'], 'confirm_duplicate': True})
        self.assertEqual(same_shelf.status_code, 400)
        self.assertFalse(same_shelf.get_json()['duplicate'])
        self.assertIn('IOS', same_shelf.get_json()['message'])

        # Top-level shelves stay unique per branch even with the confirm flag.
        top_level = client.post('/api/categories', json={
            'name': 'Electronics', 'confirm_duplicate': True})
        self.assertEqual(top_level.status_code, 400)
        self.assertIn('already exists', top_level.get_json()['message'])

    def test_rename_onto_another_shelf_asks_before_saving(self):
        client = self._client()
        electronics = self._category(client, 'Electronics')
        ios = self._category(client, 'IOS', electronics['id'])
        huawei = self._category(client, 'Huawei', electronics['id'])
        self._category(client, '256GB', ios['id'])
        other = self._category(client, '128GB', huawei['id'])

        pending = client.put(f"/api/categories/{other['id']}",
                             json={'name': '256GB', 'parent_id': huawei['id']})
        self.assertEqual(pending.status_code, 409)
        self.assertTrue(pending.get_json()['duplicate'])
        self.assertEqual(db.session.get(Category, other['id']).name, '128GB')

        confirmed = client.put(f"/api/categories/{other['id']}", json={
            'name': '256GB', 'parent_id': huawei['id'], 'confirm_duplicate': 'true'})
        self.assertEqual(confirmed.status_code, 200, confirmed.get_json())
        self.assertEqual(db.session.get(Category, other['id']).name, '256GB')


if __name__ == '__main__':
    unittest.main()
