"""Tests for secret-at-rest encryption of the AI API key.

The AI API key must never be stored in plaintext in the database. It is
encrypted with Fernet using a key derived from SECRET_KEY, and only the
application can decrypt it for use at runtime.
"""

import os
import unittest
from unittest import mock

from app import (app, db, AppSetting, get_setting, set_setting,
                 encrypt_secret, decrypt_secret, migrate_legacy_secrets)


class SecretAtRestTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        with app.app_context():
            row = AppSetting.query.filter_by(key='ai_api_key').first()
            self._previous_raw = row.value if row else None

    def tearDown(self):
        with app.app_context():
            AppSetting.query.filter(AppSetting.key.like('test_secret_%')).delete()
            row = AppSetting.query.filter_by(key='ai_api_key').first()
            if self._previous_raw is None:
                if row:
                    db.session.delete(row)
            else:
                if row:
                    row.value = self._previous_raw
                else:
                    db.session.add(AppSetting(key='ai_api_key', value=self._previous_raw))
            db.session.commit()

    def test_encrypt_decrypt_roundtrip(self):
        token = encrypt_secret('sk-roundtrip-123456')
        self.assertTrue(token.startswith('gAAAA'))
        self.assertEqual(decrypt_secret(token), 'sk-roundtrip-123456')

    def test_encrypted_value_hides_plaintext(self):
        token = encrypt_secret('sk-hide-me-987654')
        self.assertNotIn('sk-hide-me-987654', token)

    def test_legacy_plaintext_returns_original(self):
        # Pre-encryption values keep working until the migration re-encrypts them.
        self.assertEqual(decrypt_secret('sk-legacy-plain-123'), 'sk-legacy-plain-123')

    def test_wrong_secret_key_returns_empty(self):
        with mock.patch.dict(os.environ, {'SECRET_KEY': 'encrypting-key'}):
            token = encrypt_secret('sk-wrong-key-12345')
        with mock.patch.dict(os.environ, {'SECRET_KEY': 'another-key'}):
            self.assertEqual(decrypt_secret(token), '')
        with mock.patch.dict(os.environ, {'SECRET_KEY': 'encrypting-key'}):
            self.assertEqual(decrypt_secret(token), 'sk-wrong-key-12345')

    def test_set_setting_stores_ciphertext(self):
        with app.app_context():
            set_setting('ai_api_key', 'sk-stored-123456')
            raw = db.session.execute(
                db.text("SELECT value FROM app_setting WHERE key='ai_api_key'")
            ).scalar()
            self.assertTrue(raw.startswith('gAAAA'))
            self.assertNotIn('sk-stored-123456', raw)
            self.assertEqual(get_setting('ai_api_key', ''), 'sk-stored-123456')

    def test_other_settings_stay_plaintext(self):
        with app.app_context():
            set_setting('test_secret_currency_aux', 'MMK')
            row = AppSetting.query.filter_by(key='test_secret_currency_aux').first()
            self.assertEqual(row.value, 'MMK')

    def test_migration_encrypts_legacy_plaintext(self):
        with app.app_context():
            row = AppSetting.query.filter_by(key='ai_api_key').first()
            if row:
                row.value = 'sk-legacy-key-123456'
            else:
                db.session.add(AppSetting(key='ai_api_key', value='sk-legacy-key-123456'))
            db.session.commit()

            migrate_legacy_secrets()

            row = AppSetting.query.filter_by(key='ai_api_key').first()
            self.assertTrue(row.value.startswith('gAAAA'))
            self.assertNotIn('sk-legacy-key-123456', row.value)
            self.assertEqual(get_setting('ai_api_key', ''), 'sk-legacy-key-123456')

    def test_ai_agent_receives_decrypted_key(self):
        with app.app_context():
            set_setting('ai_api_key', 'sk-integration-12345')
            with mock.patch.dict(os.environ, {'APIFREE_API_KEY': ''}):
                from ai_agent import AIAgent
                agent = AIAgent(db_get_setting=get_setting)
            self.assertEqual(agent.api_key, 'sk-integration-12345')


if __name__ == '__main__':
    unittest.main()
