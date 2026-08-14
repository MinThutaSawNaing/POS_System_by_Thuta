import unittest

from app import app


class AuthenticationSessionTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)

    def test_remembered_login_uses_permanent_session(self):
        with app.test_client() as client:
            response = client.post('/login', data={
                'username': 'admin', 'password': 'admin123', 'remember': 'on'
            })
            self.assertEqual(response.status_code, 302)
            with client.session_transaction() as current_session:
                self.assertTrue(current_session.permanent)

    def test_regular_login_is_not_permanent_and_logout_clears_session(self):
        with app.test_client() as client:
            response = client.post('/login', data={'username': 'admin', 'password': 'admin123'})
            self.assertEqual(response.status_code, 302)
            with client.session_transaction() as current_session:
                self.assertFalse(current_session.permanent)

            client.get('/logout')
            with client.session_transaction() as current_session:
                self.assertNotIn('user_id', current_session)


if __name__ == '__main__':
    unittest.main()