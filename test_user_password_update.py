"""Regression tests for changing an existing user's password.

A password update must hash the new value exactly once, store it intact (modern
Werkzeug hashes are longer than the historical 120-character column), and let
the account sign in with the new password while rejecting the old one.
"""

import unittest
import uuid

from werkzeug.security import check_password_hash, generate_password_hash

from app import Branch, User, app, db


class UserPasswordUpdateTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        with app.app_context():
            self.admin = User.query.filter_by(role='manager').first()
            self.branch = Branch.query.filter_by(is_active=True).first()
            self.username = f'password_target_{uuid.uuid4().hex[:8]}'
            self.old_password = 'OldPass!123'
            user = User(username=self.username,
                        password=generate_password_hash(self.old_password),
                        role='cashier', branch_id=self.branch.id)
            db.session.add(user)
            db.session.commit()
            self.user_id = user.id
            self.admin_id = self.admin.id
            self.admin_username = self.admin.username
            self.branch_id = self.branch.id

    def tearDown(self):
        with app.app_context():
            user = db.session.get(User, self.user_id)
            if user:
                db.session.delete(user)
                db.session.commit()

    def _manager_client(self):
        client = app.test_client()
        with client.session_transaction() as session:
            session.update(user_id=self.admin_id, username=self.admin_username,
                           role='manager', branch_id=self.branch_id)
        return client

    def _update(self, password):
        return self._manager_client().put(f'/api/users/{self.user_id}', json={
            'username': self.username, 'role': 'cashier', 'password': password,
        })

    def test_updated_password_is_stored_once_and_signs_in(self):
        new_password = 'BrandNewPass!456'
        response = self._update(new_password)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

        with app.app_context():
            user = db.session.get(User, self.user_id)
            self.assertTrue(check_password_hash(user.password, new_password))
            self.assertFalse(check_password_hash(user.password, self.old_password))
            # Exactly one hash layer: the stored value is not a hash of a hash.
            self.assertTrue(user.password.startswith(('scrypt:', 'pbkdf2:')))
            self.assertGreater(len(user.password), 120)
            self.assertEqual(user.email_auth_version, 1)

        signed_in = app.test_client().post('/login', data={
            'username': self.username, 'password': new_password})
        self.assertEqual(signed_in.status_code, 302)
        rejected = app.test_client().post('/login', data={
            'username': self.username, 'password': self.old_password})
        self.assertEqual(rejected.status_code, 200)
        self.assertIn('Invalid credentials', rejected.get_data(as_text=True))

    def test_invalid_password_type_is_rejected_without_mutating_the_hash(self):
        with app.app_context():
            before = db.session.get(User, self.user_id).password
        response = self._update(12345)
        self.assertEqual(response.status_code, 400)
        with app.app_context():
            user = db.session.get(User, self.user_id)
            self.assertEqual(user.password, before)
            self.assertTrue(check_password_hash(user.password, self.old_password))

    def test_malformed_stored_hash_is_rejected_cleanly(self):
        # Simulates a legacy plaintext/truncated value: sign-in must report bad
        # credentials (so a manager can reset it) rather than raising a 500.
        with app.app_context():
            user = db.session.get(User, self.user_id)
            user.password = 'unused'
            db.session.commit()
        response = app.test_client().post('/login', data={
            'username': self.username, 'password': 'unused'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Invalid credentials', response.get_data(as_text=True))

    def test_password_column_accepts_modern_hash_lengths(self):
        self.assertGreaterEqual(User.__table__.c.password.type.length, 162)


if __name__ == '__main__':
    unittest.main()
