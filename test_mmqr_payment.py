"""Tests for MMQR settings storage and the POS payment contract."""

import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from app import AppSetting, app, db


VALID_PNG = (
    b'\x89PNG\r\n\x1a\n'
    b'\x00\x00\x00\x0dIHDR'
    b'\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00'
    b'\x1f\x15\xc4\x89'
    b'\x00\x00\x00\x0dIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff'
    b'\x89\x99=\x1d'
    b'\x00\x00\x00\x00IEND\xaeB`\x82'
)


class MMQRPaymentTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.context = app.app_context()
        self.context.push()
        self.original_mmqr_folder = app.config['MMQR_FOLDER']
        self.test_mmqr_folder = tempfile.mkdtemp(prefix='pos-mmqr-test-')
        app.config['MMQR_FOLDER'] = self.test_mmqr_folder
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = 1
            session['role'] = 'manager'

        self.previous = AppSetting.query.filter_by(key='mmqr_filename').first()
        self.previous_value = self.previous.value if self.previous else None

    def tearDown(self):
        setting = AppSetting.query.filter_by(key='mmqr_filename').first()
        if self.previous_value is None:
            if setting:
                db.session.delete(setting)
        elif setting:
            setting.value = self.previous_value
        else:
            db.session.add(AppSetting(key='mmqr_filename', value=self.previous_value))
        db.session.commit()
        app.config['MMQR_FOLDER'] = self.original_mmqr_folder
        shutil.rmtree(self.test_mmqr_folder, ignore_errors=True)
        self.context.pop()

    def test_manager_can_upload_and_read_mmqr(self):
        response = self.client.post(
            '/api/settings/mmqr',
            data={'mmqr': (io.BytesIO(VALID_PNG), 'payment.png')},
            content_type='multipart/form-data',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertIn('/uploads/mmqr/', payload['mmqr_url'])

        image = self.client.get(payload['mmqr_url'])
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.data, VALID_PNG)

        settings = self.client.get('/api/settings')
        self.assertEqual(settings.status_code, 200)
        self.assertEqual(settings.get_json()['mmqr_url'], payload['mmqr_url'])

    def test_non_manager_cannot_change_mmqr(self):
        with self.client.session_transaction() as session:
            session['role'] = 'cashier'
        response = self.client.post(
            '/api/settings/mmqr',
            data={'mmqr': (io.BytesIO(VALID_PNG), 'payment.png')},
            content_type='multipart/form-data',
        )
        self.assertEqual(response.status_code, 403)

    def test_invalid_signature_and_webp_are_rejected(self):
        for filename, content in (
            ('broken.png', b'\x89PNG\r\n\x1a\nnot-an-image'),
            ('payment.webp', b'RIFF1234WEBPdata'),
        ):
            response = self.client.post(
                '/api/settings/mmqr',
                data={'mmqr': (io.BytesIO(content), filename)},
                content_type='multipart/form-data',
            )
            self.assertEqual(response.status_code, 400)

    def test_mmqr_image_route_requires_login(self):
        with self.client.session_transaction() as session:
            session.clear()
        response = self.client.get('/uploads/mmqr/does-not-exist.png')
        self.assertEqual(response.status_code, 401)

    def test_manager_can_remove_mmqr(self):
        upload = self.client.post(
            '/api/settings/mmqr',
            data={'mmqr': (io.BytesIO(VALID_PNG), 'payment.png')},
            content_type='multipart/form-data',
        )
        self.assertEqual(upload.status_code, 200)
        filename = upload.get_json()['mmqr_filename']
        self.assertTrue(os.path.exists(os.path.join(app.config['MMQR_FOLDER'], filename)))

        removed = self.client.delete('/api/settings/mmqr')
        self.assertEqual(removed.status_code, 200)
        self.assertIsNone(removed.get_json()['mmqr_url'])
        self.assertFalse(os.path.exists(os.path.join(app.config['MMQR_FOLDER'], filename)))

    def test_dashboard_wires_mmqr_into_mobile_payment_flow(self):
        dashboard = Path(app.root_path, 'templates', 'dashboard.html').read_text(encoding='utf-8')
        self.assertIn('id="mmqr-file"', dashboard)
        self.assertIn('id="mmqrPaymentModal"', dashboard)
        self.assertIn('text-success', dashboard)
        self.assertIn('function showMMQRPaymentModal()', dashboard)
        self.assertIn('mobile_payment" || (paymentMethod === "split_payment"', dashboard)


if __name__ == '__main__':
    unittest.main()