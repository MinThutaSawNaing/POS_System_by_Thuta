"""Settings-managed MyanMyanPay credentials: global, encrypted, never echoed.

Run through run_tests.py so the database and environment are disposable.
"""

import os
import unittest
import uuid
from unittest.mock import patch

import app as app_module
from app import (AppSetting, AuditLog, Branch, PAYMENT_ENABLED_SETTING,
                 PAYMENT_SETTING_ENV, apply_payment_settings, app, db,
                 decode_audit_changes, get_default_branch_id, get_setting)
from myanmyanpay import MyanMyanPayClient


ROOT = '/api/settings/myanmyanpay'
PUBLISHABLE = 'pk_test_settings_123'
SECRET = 'sk_test_settings_123'
CONFIG = {
    'app_id': 'MM-app-1',
    'publishable_key': PUBLISHABLE,
    'secret_key': SECRET,
    'api_base_url': 'https://api.myanmyanpay.com',
    'callback_url': 'https://pos.example/webhooks/myanmyanpay',
}
DEPLOYMENT_ENV = {
    'MMPAY_ENABLED': 'false',
    'MMPAY_APP_ID': 'env-app',
    'MMPAY_PUBLISHABLE_KEY': 'pk_env_live_1',
    'MMPAY_SECRET_KEY': 'sk_env_live_1',
    'MMPAY_API_BASE_URL': 'https://env.example/api',
    'MMPAY_CALLBACK_URL': 'https://env.example/webhooks/myanmyanpay',
}


class PaymentSettingsTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.context = app.app_context()
        self.context.push()
        self.client = app.test_client()
        self.branch_id = get_default_branch_id()
        with self.client.session_transaction() as session:
            session['user_id'] = 1
            session['role'] = 'manager'
            session['branch_id'] = self.branch_id
        self.other_branch = Branch(name='Payments settings test',
                                   code='PAYSET-' + uuid.uuid4().hex[:8],
                                   is_active=True)
        db.session.add(self.other_branch)
        db.session.commit()
        self.env = patch.dict(os.environ, DEPLOYMENT_ENV)
        self.env.start()
        # Treat the test environment as the deployment baseline; the real
        # baseline is captured once at startup for the running server.
        app_module._payment_env_baseline = None
        self.currency = patch('app.get_currency_code', return_value='MMK')
        self.currency.start()
        self.reset_settings()

    def tearDown(self):
        self.reset_settings()
        db.session.delete(self.other_branch)
        db.session.commit()
        self.currency.stop()
        self.env.stop()
        self.context.pop()

    def reset_settings(self):
        for key in (*PAYMENT_SETTING_ENV, PAYMENT_ENABLED_SETTING):
            row = AppSetting.query.filter_by(key=key).first()
            if row:
                db.session.delete(row)
        db.session.commit()
        app_module._payment_env_baseline = None
        apply_payment_settings()

    def save(self, **changes):
        return self.client.put(ROOT, json={**CONFIG, 'enabled': True, **changes})

    def status(self):
        return self.client.get(ROOT).get_json()

    def test_manager_saves_encrypted_credentials_that_are_never_returned(self):
        response = self.save()
        self.assertEqual(response.status_code, 200, response.get_json())

        publishable_row = AppSetting.query.filter_by(key='mmpay_publishable_key').first()
        secret_row = AppSetting.query.filter_by(key='mmpay_secret_key').first()
        self.assertTrue(publishable_row.value.startswith('gAAAA'))
        self.assertTrue(secret_row.value.startswith('gAAAA'))
        self.assertNotEqual(publishable_row.value, PUBLISHABLE)
        self.assertNotEqual(secret_row.value, SECRET)
        self.assertEqual(get_setting('mmpay_secret_key'), SECRET)

        payload = self.status()
        self.assertTrue(payload['enabled'])
        self.assertTrue(payload['configured'])
        self.assertTrue(payload['sandbox'])
        self.assertEqual(payload['sources']['secret_key'], 'settings')
        self.assertNotIn(SECRET, str(payload))
        self.assertNotIn(PUBLISHABLE, str(payload))

    def test_saved_credentials_apply_to_every_branch_immediately(self):
        self.assertEqual(self.save().status_code, 200)

        client = MyanMyanPayClient.from_env()
        self.assertTrue(client.enabled)
        self.assertTrue(client.sandbox)

        with self.client.session_transaction() as session:
            session['branch_id'] = self.other_branch.id
        payload = self.status()
        self.assertTrue(payload['configured'])
        self.assertEqual(payload['sources']['publishable_key'], 'settings')

        from payment_routes import payment_enabled
        self.assertTrue(payment_enabled())

    def test_cashier_cannot_read_or_change_payment_settings(self):
        with self.client.session_transaction() as session:
            session['role'] = 'cashier'
        self.assertEqual(self.client.get(ROOT).status_code, 403)
        self.assertEqual(self.client.put(ROOT, json=CONFIG).status_code, 403)
        self.assertIsNone(AppSetting.query.filter_by(key='mmpay_secret_key').first())

    def test_mixed_key_environments_and_unsafe_urls_are_rejected(self):
        cases = (
            {'publishable_key': 'pk_live_abc', 'secret_key': 'sk_test_abc'},
            {'publishable_key': 'pk_test_abc', 'secret_key': 'sk_live_abc'},
            {'callback_url': 'http://pos.example/webhooks/myanmyanpay'},
            {'callback_url': 'https://pos.example/other-path'},
            {'callback_url': 'https://user:pass@pos.example/webhooks/myanmyanpay'},
            {'api_base_url': 'http://api.myanmyanpay.com'},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                response = self.save(**changes)
                self.assertEqual(response.status_code, 400, response.get_json())
        self.assertIsNone(AppSetting.query.filter_by(key='mmpay_secret_key').first())

    def test_blank_secret_fields_keep_the_stored_keys(self):
        self.assertEqual(self.save().status_code, 200)
        response = self.client.put(ROOT, json={
            'app_id': 'MM-app-2',
            'publishable_key': '',
            'secret_key': '',
            'enabled': True,
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(get_setting('mmpay_secret_key'), SECRET)
        self.assertEqual(get_setting('mmpay_app_id'), 'MM-app-2')
        self.assertTrue(MyanMyanPayClient.from_env().enabled)

    def test_clear_falls_back_to_the_deployment_environment(self):
        self.assertEqual(self.save().status_code, 200)
        response = self.client.put(ROOT, json={'clear': True})
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertFalse(payload['saved_in_settings'])
        self.assertEqual(payload['sources']['secret_key'], 'environment')
        self.assertEqual(os.environ['MMPAY_SECRET_KEY'],
                         DEPLOYMENT_ENV['MMPAY_SECRET_KEY'])
        self.assertIsNone(AppSetting.query.filter_by(key='mmpay_secret_key').first())

    def test_save_is_audited_without_secret_values(self):
        self.assertEqual(self.save().status_code, 200)
        rows = AuditLog.query.filter_by(entity_type='payment_settings').all()
        self.assertTrue(rows)
        details = ' '.join(str(decode_audit_changes(row)) for row in rows)
        self.assertNotIn(SECRET, details)
        self.assertNotIn(PUBLISHABLE, details)

    def test_unconfigured_install_reports_nothing_stored(self):
        with patch.dict(os.environ, {name: '' for name in DEPLOYMENT_ENV}):
            app_module._payment_env_baseline = None
            apply_payment_settings()
            payload = self.status()
        self.assertFalse(payload['enabled'])
        self.assertFalse(payload['configured'])
        self.assertFalse(payload['saved_in_settings'])
        self.assertEqual(payload['sources']['secret_key'], 'unset')
        self.assertTrue(payload['currency_supported'])  # MMK is mocked

    def test_enabling_requires_the_mmk_store_currency(self):
        with patch('app.get_currency_code', return_value='USD'):
            response = self.save()
        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertIn('MMK', response.get_json()['message'])
        self.assertIsNone(AppSetting.query.filter_by(key='mmpay_secret_key').first())

    def test_settings_panel_wires_payment_configuration(self):
        from pathlib import Path
        dashboard = Path(app.root_path, 'templates', 'dashboard.html').read_text(encoding='utf-8')
        for token in ('id="settings-group-payments"', 'data-group="payments"',
                      'id="save-mmpay-btn"', 'onclick="savePaymentSettings()"',
                      'id="clear-mmpay-btn"', 'function loadPaymentSettings()',
                      'function savePaymentSettings()', 'function clearPaymentSettings()',
                      'function togglePaymentSecret(', '/api/settings/myanmyanpay'):
            self.assertIn(token, dashboard)
        # The panel renders placeholders only; a stored credential is never echoed.
        panel = dashboard.split('id="settings-group-payments"', 1)[1].split(
            '/settings-group-payments', 1)[0]
        self.assertNotIn('value="pk_', panel)
        self.assertNotIn('value="sk_', panel)

    def test_disabled_configuration_does_not_report_ready(self):
        response = self.client.put(ROOT, json={**CONFIG, 'enabled': False})
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertFalse(payload['enabled'])
        self.assertFalse(payload['configured'])


if __name__ == '__main__':
    unittest.main()
