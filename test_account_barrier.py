"""Tests for the vendor-only account-creation barrier.

Creating user accounts is gated behind a master credential whose password is
stored in the database only as a salted hash, guarded by a numeric captcha and a
per-client rate limiter. The gate is enforced by the API (not just the modal),
so a direct POST to /api/users cannot bypass it.
"""

import os
import re
import unittest
import uuid
from unittest import mock

from sqlalchemy import func
from werkzeug.security import check_password_hash, generate_password_hash

import app as app_module
from app import (ACCOUNT_BARRIER_PASSWORD_SETTING, ACCOUNT_BARRIER_USERNAME_SETTING,
                 AppSetting, AuditLog, Branch, User, app, db, seed_account_barrier_credential,
                 set_setting)

# Test-only credential. The real deployment credential is provisioned from the
# untracked .env (or the environment) and is never committed; each test seeds
# its own credential below so nothing sensitive lives in source control.
BARRIER_USERNAME = 'test_master'
BARRIER_PASSWORD = 'TestOnlyMaster!123'


class AccountBarrierTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        app_module._barrier_failures.clear()
        app_module._barrier_captchas.clear()
        self.created_user_ids = []
        with app.app_context():
            self._previous = {}
            for key in (ACCOUNT_BARRIER_USERNAME_SETTING, ACCOUNT_BARRIER_PASSWORD_SETTING):
                row = AppSetting.query.filter_by(key=key).first()
                self._previous[key] = row.value if row else None
            set_setting(ACCOUNT_BARRIER_USERNAME_SETTING, BARRIER_USERNAME)
            set_setting(ACCOUNT_BARRIER_PASSWORD_SETTING,
                        generate_password_hash(BARRIER_PASSWORD))
            self.manager = User.query.filter_by(role='manager').first()
            self.branch = Branch.query.filter_by(is_active=True).first()
            self.manager_id = self.manager.id
            self.manager_username = self.manager.username
            self.branch_id = self.branch.id
            self.max_log_id = db.session.query(func.max(AuditLog.id)).scalar() or 0
            self.role_target = User(
                username=f'barrier_role_target_{uuid.uuid4().hex[:8]}',
                password=generate_password_hash('UnusedTargetPass!123'),
                role='cashier',
            )
            db.session.add(self.role_target)
            db.session.commit()
            self.created_user_ids.append(self.role_target.id)
            self.role_target_id = self.role_target.id
            self.role_target_username = self.role_target.username

    def tearDown(self):
        app_module._barrier_failures.clear()
        app_module._barrier_captchas.clear()
        with app.app_context():
            if self.created_user_ids:
                db.session.execute(
                    User.__table__.delete().where(User.id.in_(self.created_user_ids)))
            # Audit rows are append-only through the ORM; fixtures remove the
            # unlock events this test created via the maintenance escape hatch.
            db.session.info['_allow_audit_log_maintenance'] = True
            try:
                db.session.execute(
                    AuditLog.__table__.delete().where(AuditLog.id > self.max_log_id))
            finally:
                db.session.info.pop('_allow_audit_log_maintenance', None)
            for key, value in self._previous.items():
                row = AppSetting.query.filter_by(key=key).first()
                if value is None:
                    if row:
                        db.session.delete(row)
                elif row:
                    row.value = value
                else:
                    db.session.add(AppSetting(key=key, value=value))
            db.session.commit()

    def _client(self, role='manager'):
        client = app.test_client()
        with client.session_transaction() as current:
            current['user_id'] = self.manager_id
            current['username'] = self.manager_username
            current['role'] = role
            current['branch_id'] = self.branch_id
        return client

    def _solve_captcha(self, client):
        challenge = client.get('/api/account_barrier/challenge')
        self.assertEqual(challenge.status_code, 200, challenge.get_data(as_text=True))
        question = challenge.get_json()['question']
        match = re.match(r'What is (\d+) \+ (\d+)\?', question)
        self.assertIsNotNone(match, f'unexpected captcha question: {question!r}')
        return int(match.group(1)) + int(match.group(2))

    def test_barrier_endpoints_require_manager(self):
        client = self._client(role='cashier')
        self.assertEqual(client.get('/api/account_barrier/challenge').status_code, 403)
        self.assertEqual(
            client.post('/api/account_barrier/unlock', json={}).status_code, 403)

    def test_create_user_is_locked_until_unlocked(self):
        client = self._client()
        response = client.post('/api/users', json={
            'username': f'barrier_{uuid.uuid4().hex[:8]}', 'password': 'x', 'role': 'cashier'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json().get('code'), 'account_barrier_locked')

    def test_role_change_is_locked_and_does_not_mutate_the_user(self):
        client = self._client()
        response = client.put(f'/api/users/{self.role_target_id}', json={
            'username': self.role_target_username,
            'role': 'manager',
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json().get('code'), 'account_barrier_locked')
        with app.app_context():
            target = db.session.get(User, self.role_target_id)
            self.assertEqual(target.role, 'cashier')

    def test_non_role_edits_remain_available_while_the_barrier_is_locked(self):
        client = self._client()
        new_username = f'barrier_rename_{uuid.uuid4().hex[:8]}'
        response = client.put(f'/api/users/{self.role_target_id}', json={
            'username': new_username,
            'role': 'cashier',
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        with app.app_context():
            target = db.session.get(User, self.role_target_id)
            self.assertEqual(target.username, new_username)
            self.assertEqual(target.role, 'cashier')

    def test_unlocked_barrier_allows_a_role_change(self):
        client = self._client()
        answer = self._solve_captcha(client)
        unlock = client.post('/api/account_barrier/unlock', json={
            'username': BARRIER_USERNAME,
            'password': BARRIER_PASSWORD,
            'captcha_answer': answer,
        })
        self.assertEqual(unlock.status_code, 200, unlock.get_data(as_text=True))
        response = client.put(f'/api/users/{self.role_target_id}', json={
            'username': self.role_target_username,
            'role': 'manager',
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        with app.app_context():
            self.assertEqual(db.session.get(User, self.role_target_id).role, 'manager')

    def test_challenge_question_is_a_numeric_sum(self):
        client = self._client()
        challenge = client.get('/api/account_barrier/challenge').get_json()
        self.assertTrue(challenge['success'])
        self.assertTrue(challenge['configured'])
        self.assertRegex(challenge['question'], r'^What is \d \+ \d\?$')

    def test_unlock_with_master_credential_allows_creating_users(self):
        client = self._client()
        answer = self._solve_captcha(client)
        unlock = client.post('/api/account_barrier/unlock', json={
            'username': BARRIER_USERNAME, 'password': BARRIER_PASSWORD,
            'captcha_answer': answer})
        self.assertEqual(unlock.status_code, 200, unlock.get_data(as_text=True))
        self.assertTrue(unlock.get_json()['success'])

        username = f'barrier_ok_{uuid.uuid4().hex[:8]}'
        created = client.post('/api/users', json={
            'username': username, 'password': 'SecretPass123!', 'role': 'cashier'})
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        with app.app_context():
            user = User.query.filter_by(username=username).first()
            self.assertIsNotNone(user)
            self.created_user_ids.append(user.id)
            # The account password is stored hashed too.
            self.assertNotEqual(user.password, 'SecretPass123!')
            self.assertTrue(check_password_hash(user.password, 'SecretPass123!'))

    def test_unlock_rejects_wrong_password(self):
        client = self._client()
        answer = self._solve_captcha(client)
        response = client.post('/api/account_barrier/unlock', json={
            'username': BARRIER_USERNAME, 'password': 'not-the-password',
            'captcha_answer': answer})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()['success'])
        self.assertTrue(response.get_json()['refresh_captcha'])

    def test_unlock_rejects_wrong_captcha(self):
        client = self._client()
        answer = self._solve_captcha(client)
        response = client.post('/api/account_barrier/unlock', json={
            'username': BARRIER_USERNAME, 'password': BARRIER_PASSWORD,
            'captcha_answer': int(answer) + 1})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.get_json()['refresh_captcha'])
        # The correct credential is not accepted when the sum is wrong.
        with client.session_transaction() as current:
            self.assertNotIn('account_barrier_unlocked', current)

    def test_barrier_password_is_hashed_in_database(self):
        with app.app_context():
            row = AppSetting.query.filter_by(key=ACCOUNT_BARRIER_PASSWORD_SETTING).first()
            self.assertIsNotNone(row)
            self.assertNotEqual(row.value, BARRIER_PASSWORD)
            self.assertNotIn(BARRIER_PASSWORD, row.value)
            self.assertTrue(check_password_hash(row.value, BARRIER_PASSWORD))

    def test_rate_limiter_blocks_after_repeated_failures(self):
        client = self._client()
        for _ in range(app_module._BARRIER_MAX_ATTEMPTS):
            answer = self._solve_captcha(client)
            response = client.post('/api/account_barrier/unlock', json={
                'username': BARRIER_USERNAME, 'password': 'wrong',
                'captcha_answer': answer})
            self.assertEqual(response.status_code, 400)

        # Even a correct attempt is refused while the caller is throttled.
        blocked = client.post('/api/account_barrier/unlock', json={
            'username': BARRIER_USERNAME, 'password': BARRIER_PASSWORD,
            'captcha_answer': 0})
        self.assertEqual(blocked.status_code, 429)
        self.assertGreaterEqual(blocked.get_json().get('retry_after', 0), 1)
        self.assertEqual(client.get('/api/account_barrier/challenge').status_code, 429)

    def test_seed_hashes_password_and_is_idempotent(self):
        env = {'POS_ACCOUNT_BARRIER_USERNAME': 'seeded_user',
               'POS_ACCOUNT_BARRIER_PASSWORD': 'SeededPass!123'}
        with app.app_context():
            AppSetting.query.filter(AppSetting.key.in_([
                ACCOUNT_BARRIER_USERNAME_SETTING,
                ACCOUNT_BARRIER_PASSWORD_SETTING])).delete()
            db.session.commit()

            with mock.patch.dict(os.environ, env):
                seed_account_barrier_credential()
            first = AppSetting.query.filter_by(key=ACCOUNT_BARRIER_PASSWORD_SETTING).first()
            self.assertNotEqual(first.value, 'SeededPass!123')
            self.assertNotIn('SeededPass!123', first.value)
            self.assertTrue(check_password_hash(first.value, 'SeededPass!123'))

            # Re-seeding the same credential must not rewrite the stored hash.
            with mock.patch.dict(os.environ, env):
                seed_account_barrier_credential()
            second = AppSetting.query.filter_by(key=ACCOUNT_BARRIER_PASSWORD_SETTING).first()
            self.assertEqual(first.value, second.value)

    def test_seed_leaves_unconfigured_barrier_locked(self):
        with app.app_context():
            AppSetting.query.filter(AppSetting.key.in_([
                ACCOUNT_BARRIER_USERNAME_SETTING,
                ACCOUNT_BARRIER_PASSWORD_SETTING])).delete()
            db.session.commit()
            with mock.patch.dict(os.environ, {'POS_ACCOUNT_BARRIER_USERNAME': '',
                                              'POS_ACCOUNT_BARRIER_PASSWORD': ''}):
                seed_account_barrier_credential()
        client = self._client()
        challenge = client.get('/api/account_barrier/challenge')
        self.assertEqual(challenge.status_code, 403)
        self.assertFalse(challenge.get_json()['configured'])

    def test_settings_api_cannot_overwrite_the_barrier_credential(self):
        """The settings endpoint whitelists keys, so it cannot reset the barrier."""
        client = self._client()
        with app.app_context():
            before_hash = AppSetting.query.filter_by(
                key=ACCOUNT_BARRIER_PASSWORD_SETTING).first().value
        response = client.put('/api/settings', json={
            'account_barrier_username': 'hacker',
            'account_barrier_password_hash': generate_password_hash('hacked'),
        })
        self.assertEqual(response.status_code, 400)
        with app.app_context():
            after_hash = AppSetting.query.filter_by(
                key=ACCOUNT_BARRIER_PASSWORD_SETTING).first().value
            self.assertEqual(after_hash, before_hash)
            self.assertFalse(app_module._verify_account_barrier('hacker', 'hacked'))
            self.assertTrue(
                app_module._verify_account_barrier(BARRIER_USERNAME, BARRIER_PASSWORD))


class AgentBarrierBypassTests(unittest.TestCase):
    """The Loli agent must have no path to user accounts or the barrier.

    These are regression guards: if a future tool exposes user creation or the
    credential store, the build fails before it can ship.
    """

    def test_tool_module_has_no_user_or_credential_plumbing(self):
        import ai_tools
        with open(ai_tools.__file__, encoding='utf-8') as handle:
            source = handle.read()
        for token in ('AppSetting', 'generate_password_hash', 'check_password_hash',
                      'User.query', 'User(', 'account_barrier', 'set_setting'):
            self.assertNotIn(token, source,
                             f'ai_tools.py must not reference {token!r}')

    def test_no_registered_tool_targets_account_administration(self):
        from ai_tools import get_all_tools
        banned = ('user', 'account', 'barrier', 'password', 'credential', 'login')
        offenders = sorted(name for name in get_all_tools()
                           if any(token in name.lower() for token in banned))
        self.assertEqual(offenders, [])

    def test_settings_model_is_not_exposed_to_the_agent(self):
        from app import AI_MODELS
        # Credentials live in AppSetting; no tool may be able to reach it.
        self.assertNotIn('AppSetting', AI_MODELS)
        # User stays only so the autonomy gate can read the acting user's role.
        self.assertIn('User', AI_MODELS)

    def test_every_tool_method_is_free_of_user_writes(self):
        import inspect
        from ai_tools import AITools, get_all_tools
        container = AITools(db=None, models={})
        for name in get_all_tools():
            method = getattr(container, name, None)
            if method is None:
                continue
            source = inspect.getsource(method)
            for token in ('AppSetting', 'generate_password_hash', 'User('):
                self.assertNotIn(token, source,
                                 f'tool {name!r} references {token!r}')

    def test_dashboard_reuses_the_server_barrier_for_role_changes(self):
        from pathlib import Path
        source = (Path(__file__).parent / 'templates' / 'dashboard.html').read_text(
            encoding='utf-8')
        update_user = source[source.index('function updateUser()'):source.index(
            'function deleteUser(')]
        self.assertIn('data.code === "account_barrier_locked"', update_user)
        self.assertIn('openAccountBarrier(\n                  updateUser,', update_user)

    def test_dashboard_explains_role_restriction_and_support_contact(self):
        from pathlib import Path
        source = (Path(__file__).parent / 'templates' / 'dashboard.html').read_text(
            encoding='utf-8')
        edit_modal = source[source.index('<!-- Edit User Modal -->'):source.index(
            '<!-- Add Customer Modal -->')]
        self.assertIn('Role changes are restricted.', edit_modal)
        self.assertIn('WinterArcMyanmar', edit_modal)
        self.assertIn('Creating user accounts and changing user roles are restricted.', source)
        self.assertIn(
            'Changing user roles is restricted. Unlock with the master credential '
            'or please contact WinterArcMyanmar.', source)


if __name__ == '__main__':
    unittest.main()
