import unittest
from unittest.mock import Mock, patch

from app import app, db, Branch, MemoryAudit, MemoryRegistry, User


class MemoryApiTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        with app.app_context():
            self.user_id = User.query.filter_by(username='admin').first().id
            self.branch_id = Branch.query.filter_by(is_active=True).first().id
            MemoryAudit.query.delete()
            MemoryRegistry.query.delete()
            db.session.commit()

    def tearDown(self):
        with app.app_context():
            MemoryAudit.query.delete()
            MemoryRegistry.query.delete()
            db.session.commit()

    def _client(self, role='manager', user_id=None):
        client = app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = user_id or self.user_id
            session['role'] = role
            session['branch_id'] = self.branch_id
        return client

    def test_private_memory_save_and_list_is_scoped_to_current_user(self):
        service = Mock()
        service.remember.return_value = {'saved': True, 'memory_id': 'mem-private', 'summary': 'Safe label'}
        client = self._client()
        with patch('app.get_persistent_memory_service', return_value=service):
            response = client.post('/api/agent/memories', json={'content': 'My preferred report is daily'})
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.get_json()['memory']['summary'], 'Safe label')
            response = client.get('/api/agent/memories')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['id'] for item in response.get_json()['memories']], ['mem-private'])

    def test_cashier_cannot_create_branch_shared_memory(self):
        client = self._client(role='cashier')
        response = client.post('/api/agent/memories', json={
            'content': 'Use the branch default tax rate', 'scope': 'branch_shared'
        })
        self.assertEqual(response.status_code, 403)

    def test_delete_requires_a_visible_owned_memory_and_backend_success(self):
        with app.app_context():
            db.session.add(MemoryRegistry(memory_id='owned', user_id=self.user_id,
                                          branch_id=self.branch_id, scope='private',
                                          summary='Safe label', source='manual'))
            db.session.commit()
        service = Mock()
        service.forget.return_value = {'deleted': True}
        with patch('app.get_persistent_memory_service', return_value=service):
            response = self._client().delete('/api/agent/memories/owned')
        self.assertEqual(response.status_code, 200)
        with app.app_context():
            self.assertIsNone(MemoryRegistry.query.filter_by(memory_id='owned').first())

    def test_forget_all_removes_only_current_users_private_memories(self):
        with app.app_context():
            db.session.add_all([
                MemoryRegistry(memory_id='private', user_id=self.user_id, branch_id=self.branch_id,
                               scope='private', summary='Private', source='manual'),
                MemoryRegistry(memory_id='shared', user_id=self.user_id, branch_id=self.branch_id,
                               scope='branch_shared', summary='Shared', source='manual'),
            ])
            db.session.commit()
        service = Mock()
        service.forget.return_value = {'deleted': True}
        with patch('app.get_persistent_memory_service', return_value=service):
            response = self._client().post('/api/agent/memories/forget-all')
        self.assertEqual(response.status_code, 200)
        with app.app_context():
            self.assertIsNone(MemoryRegistry.query.filter_by(memory_id='private').first())
            self.assertIsNotNone(MemoryRegistry.query.filter_by(memory_id='shared').first())


if __name__ == '__main__':
    unittest.main()