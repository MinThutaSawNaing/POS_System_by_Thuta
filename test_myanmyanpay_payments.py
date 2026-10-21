"""Payment boundary tests; always run via run_tests.py (disposable database)."""
import hashlib
import hmac
import json
import os
import unittest
import uuid
from unittest.mock import patch

from app import (app, db, Branch, Product, Sale, SaleItem, User,
                 PaymentIntent, PaymentWebhookEvent)


ROOT = '/api/payments/myanmyanpay'
HEADERS = {'X-POS-Payment-Request': '1'}


class MyanMyanPayPaymentTests(unittest.TestCase):
    def setUp(self):
        self.context = app.app_context()
        self.context.push()
        app.config.update(TESTING=True)
        self.branch = Branch.query.filter_by(is_active=True).first()
        self.user = User.query.filter_by(username='admin').first()
        self.product = Product(name='MMPay boundary test', price=1000, stock=10,
                               tax_rate=5, branch_id=self.branch.id,
                               barcode='MMP-' + uuid.uuid4().hex)
        db.session.add(self.product)
        db.session.commit()
        self.client = app.test_client()
        with self.client.session_transaction() as s:
            s.update(user_id=self.user.id, role='manager', branch_id=self.branch.id)
        self.env = patch.dict(os.environ, {'MMPAY_ENABLED': 'true',
            'MMPAY_APP_ID': 'app-test', 'MMPAY_PUBLISHABLE_KEY': 'pk_test_abc',
            'MMPAY_SECRET_KEY': 'sk_test_abc', 'MMPAY_API_BASE_URL': 'https://api.myanmyanpay.com',
            'MMPAY_CALLBACK_URL': 'https://pos.example/webhooks/myanmyanpay'})
        self.env.start()
        self.currency = patch('app.get_currency_code', return_value='MMK')
        self.currency.start()
        self.create_mock = patch('myanmyanpay.MyanMyanPayClient.create', side_effect=self.provider_create)
        self.create = self.create_mock.start()
        self.txns = []

    def tearDown(self):
        PaymentWebhookEvent.query.delete()
        PaymentIntent.query.delete()
        ids = [r.sale_id for r in SaleItem.query.filter_by(product_id=self.product.id)]
        SaleItem.query.filter(SaleItem.sale_id.in_(ids or [0])).delete(synchronize_session=False)
        Sale.query.filter(Sale.id.in_(ids or [0])).delete(synchronize_session=False)
        db.session.delete(self.product)
        db.session.commit()
        self.create_mock.stop()
        self.currency.stop()
        self.env.stop()
        self.context.pop()

    def checkout(self, **updates):
        data = {'items': [{'product_id': self.product.id, 'price': 1000, 'quantity': 1}],
                'payment_method': 'mobile_payment', 'transaction_id': str(uuid.uuid4())}
        data.update(updates)
        return data

    @staticmethod
    def provider_create(order_id, amount, callback_url):
        return {'orderId': order_id, 'amount': float(amount), 'currency': 'MMK',
                'status': 'PENDING', 'vendorQrRefId': 'qr-' + order_id, 'qr': '000201TESTQR'}

    def begin(self, data=None):
        response = self.client.post(ROOT + '/intents', json=data or self.checkout(), headers=HEADERS)
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()['payment']

    def test_order_id_fits_provider_handshake_limit(self):
        payment = self.begin()
        self.assertLessEqual(len(payment['order_id']), 32)
        self.assertTrue(payment['qr_url'])

    def test_definitive_provider_rejection_releases_the_order(self):
        from myanmyanpay import PaymentProviderError
        with patch('myanmyanpay.MyanMyanPayClient.create',
                   side_effect=PaymentProviderError('rejected', status_code=403)):
            response = self.client.post(ROOT + '/intents', json=self.checkout(), headers=HEADERS)
        self.assertEqual(response.status_code, 422, response.get_json())
        payment = response.get_json()['payment']
        self.assertEqual(payment['status'], 'FAILED')
        self.assertIsNone(payment['qr_url'])
        # A released order never blocks the next checkout.
        retry = self.begin()
        self.assertEqual(retry['status'], 'PENDING')

    def test_ambiguous_provider_failure_keeps_the_order(self):
        from myanmyanpay import PaymentProviderError
        with patch('myanmyanpay.MyanMyanPayClient.create',
                   side_effect=PaymentProviderError('timeout', status_code=504)):
            response = self.client.post(ROOT + '/intents', json=self.checkout(), headers=HEADERS)
        self.assertEqual(response.status_code, 502, response.get_json())
        self.assertEqual(response.get_json()['payment']['status'], 'CREATING')

    def test_explicit_cancel_recovers_legacy_rejected_order(self):
        payment = self.begin()
        intent = db.session.get(PaymentIntent, payment['id'])
        intent.order_id = 'POS-' + uuid.uuid4().hex
        intent.status = 'CREATING'
        intent.qr_payload = None
        intent.vendor_qr_ref = None
        db.session.commit()
        with patch('myanmyanpay.MyanMyanPayClient.cancel') as cancel:
            response = self.client.post(ROOT + '/intents/' + intent.id + '/cancel', headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['payment']['status'], 'CANCELLED')
        cancel.assert_not_called()

    def event(self, payment, status='SUCCESS', **updates):
        payload = {'orderId': payment['order_id'], 'amount': float(payment['amount']),
                   'currency': 'MMK', 'status': status, 'vendorQrRefId': 'qr-' + payment['order_id'],
                   'transactionRefId': 'paid-' + payment['order_id'], 'condition': 'PRISTINE'}
        payload.update(updates)
        raw = json.dumps(payload, separators=(',', ':')).encode()
        nonce = '1234567890'
        signature = hmac.new(b'sk_test_abc', nonce.encode() + b'.' + raw, hashlib.sha256).hexdigest()
        return self.client.post('/webhooks/myanmyanpay', data=raw, content_type='application/json',
            headers={'X-Mmpay-Nonce': nonce, 'X-Mmpay-Signature': signature})

    def test_disabled_default_and_credentials_not_exposed(self):
        with patch.dict(os.environ, {'MMPAY_ENABLED': 'false'}):
            response = self.client.get(ROOT + '/config')
            self.assertFalse(response.get_json()['enabled'])
            self.assertEqual(self.client.post(ROOT + '/intents', json=self.checkout(), headers=HEADERS).status_code, 503)
        self.assertNotIn('sk_test', self.client.get(ROOT + '/config').get_data(as_text=True))

    def test_creating_payment_does_not_create_sale_or_decrement_stock(self):
        payment = self.begin()
        self.assertEqual(payment['amount'], '1050.00')
        self.assertEqual(payment['status'], 'PENDING')
        self.assertIsNone(Sale.query.filter_by(transaction_id=payment['transaction_id']).first())
        self.assertEqual(db.session.get(Product, self.product.id).stock, 10)
        qr = self.client.get(payment['qr_url'])
        self.assertEqual(qr.status_code, 200)
        self.assertIn(b'<svg', qr.data)
        # Compliance: the provider payload is rendered unchanged with a white
        # quiet zone around it (no logo or caption drawn inside the symbol).
        self.assertIn(b'width="384"', qr.data)
        self.assertIn(b'fill: rgb(100%,100%,100%)', qr.data)
        self.assertEqual(self.client.post(ROOT + '/intents/' + payment['id'] + '/complete', headers=HEADERS).status_code, 409)

    def test_order_exposes_the_fifteen_minute_validity_window(self):
        payment = self.begin()
        window = payment['expires_at'] - payment['server_now']
        self.assertLessEqual(window, 15 * 60 * 1000)
        self.assertGreater(window, 15 * 60 * 1000 - 5000)
        refreshed = self.client.get(ROOT + '/intents/' + payment['id']).get_json()['payment']
        self.assertEqual(refreshed['expires_at'], payment['expires_at'])

    def test_a_second_order_is_blocked_until_the_active_order_is_cancelled(self):
        first = self.begin()
        # Reloading, reopening the modal or resubmitting never mints another QR.
        blocked = self.client.post(ROOT + '/intents', json=self.checkout(), headers=HEADERS)
        self.assertEqual(blocked.status_code, 409, blocked.get_json())
        self.assertEqual(blocked.get_json()['payment']['id'], first['id'])
        self.assertEqual(PaymentIntent.query.count(), 1)

        result = self.provider_create(first['order_id'], 1050, '')
        result['status'] = 'CANCELLED'
        with patch('myanmyanpay.MyanMyanPayClient.cancel', return_value={'status': 'CANCELLED'}), \
                patch('myanmyanpay.MyanMyanPayClient.get', return_value=result):
            cancelled = self.client.post(ROOT + '/intents/' + first['id'] + '/cancel', headers=HEADERS)
            self.assertEqual(cancelled.status_code, 200, cancelled.get_json())

        second = self.begin()
        self.assertNotEqual(second['id'], first['id'])
        self.assertEqual(PaymentIntent.query.count(), 2)

    def test_signed_success_then_complete_is_idempotent_and_uses_saved_cart(self):
        payment = self.begin()
        self.assertEqual(self.event(payment).status_code, 200)
        duplicate = self.event(payment)
        self.assertTrue(duplicate.get_json()['duplicate'])
        endpoint = ROOT + '/intents/' + payment['id'] + '/complete'
        response = self.client.post(endpoint, json={'items': [], 'amount': 1}, headers=HEADERS)
        self.assertEqual(response.status_code, 201, response.get_json())
        replay = self.client.post(endpoint, headers=HEADERS)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(Sale.query.filter_by(transaction_id=payment['transaction_id']).count(), 1)
        db.session.expire_all()
        self.assertEqual(db.session.get(Product, self.product.id).stock, 9)
        self.assertEqual(PaymentWebhookEvent.query.count(), 1)

    def test_unsigned_tampered_and_wrong_amount_callbacks_never_pay(self):
        payment = self.begin()
        self.assertEqual(self.client.post('/webhooks/myanmyanpay', json={'status': 'SUCCESS'}).status_code, 401)
        for updates in ({'amount': 1}, {'currency': 'USD'}, {'vendorQrRefId': 'another-qr'},
                        {'transactionRefId': ''}, {'status': 'SETTLED'}):
            self.assertEqual(self.event(payment, **updates).status_code, 409)
        db.session.expire_all()
        self.assertEqual(db.session.get(PaymentIntent, payment['id']).status, 'PENDING')

    def test_out_of_order_events_cannot_undo_payment_or_refund(self):
        payment = self.begin()
        self.assertEqual(self.event(payment, 'EXPIRED').status_code, 200)
        self.assertEqual(self.event(payment).status_code, 200)
        self.assertEqual(self.event(payment, 'PENDING').status_code, 200)
        db.session.expire_all()
        self.assertEqual(db.session.get(PaymentIntent, payment['id']).status, 'SUCCESS')
        self.assertEqual(self.event(payment, 'REFUNDED').status_code, 200)
        self.assertEqual(self.event(payment, 'SUCCESS', condition='TOUCHED').status_code, 200)
        db.session.expire_all()
        self.assertEqual(db.session.get(PaymentIntent, payment['id']).status, 'REFUNDED')
        self.assertEqual(self.client.post(ROOT + '/intents/' + payment['id'] + '/complete', headers=HEADERS).status_code, 409)

    def test_idempotent_creation_and_changed_cart_rejected(self):
        data = self.checkout()
        payment = self.begin(data)
        replay = self.client.post(ROOT + '/intents', json=data, headers=HEADERS)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.get_json()['payment']['id'], payment['id'])
        data['items'][0]['quantity'] = 2
        self.assertEqual(self.client.post(ROOT + '/intents', json=data, headers=HEADERS).status_code, 409)
        self.assertEqual(self.create.call_count, 1)

    def test_split_payment_quotes_only_mobile_leg(self):
        payment = self.begin(self.checkout(payment_method='split_payment',
                             payment_breakdown={'cash': 500, 'mobile_payment': 550}))
        self.assertEqual(payment['amount'], '550.00')
        self.assertEqual(self.event(payment).status_code, 200)
        self.assertEqual(self.client.post(ROOT + '/intents/' + payment['id'] + '/complete', headers=HEADERS).status_code, 201)

    def test_manual_api_cannot_bypass_verification(self):
        response = self.client.post('/api/sales', json=self.checkout())
        self.assertEqual(response.status_code, 409)
        response = self.client.post('/api/sales', json=self.checkout(payment_method='split_payment',
                      payment_breakdown={'cash': 500, 'mobile_payment': 550}))
        self.assertEqual(response.status_code, 409)

    def test_saved_transaction_cannot_be_reused_as_cash_or_after_disabling(self):
        data = self.checkout()
        self.begin(data)
        cash = dict(data, payment_method='cash', cash_received=1050)
        self.assertEqual(self.client.post('/api/sales', json=cash).status_code, 409)
        with patch.dict(os.environ, {'MMPAY_ENABLED': 'false'}):
            self.assertEqual(self.client.post('/api/sales', json=data).status_code, 409)

    def test_trimmed_checkout_id_completes_and_terminal_orders_do_not_block(self):
        payment = self.begin(self.checkout(transaction_id='  ' + str(uuid.uuid4()) + '  '))
        self.assertEqual(payment['transaction_id'], payment['transaction_id'].strip())
        self.assertEqual(self.event(payment).status_code, 200)
        self.assertEqual(self.client.post(ROOT + '/intents/' + payment['id'] + '/complete', headers=HEADERS).status_code, 201)
        for status in ('FAILED', 'CANCELLED', 'EXPIRED', 'REFUNDED'):
            payment = self.begin()
            self.assertEqual(self.event(payment, status).status_code, 200)
        self.assertEqual(self.client.get(ROOT + '/intents').get_json()['payments'], [])

    def test_callbacks_for_outstanding_order_survive_disabled_switch(self):
        payment = self.begin()
        with patch.dict(os.environ, {'MMPAY_ENABLED': 'false'}):
            self.assertEqual(self.event(payment).status_code, 200)
            self.assertEqual(self.client.post(ROOT + '/intents/' + payment['id'] + '/complete', headers=HEADERS).status_code, 201)

    def test_disabled_switch_still_allows_existing_order_refresh(self):
        payment = self.begin()
        payload = self.provider_create(payment['order_id'], 1050, '')
        with patch.dict(os.environ, {'MMPAY_ENABLED': 'false'}), \
                patch('myanmyanpay.MyanMyanPayClient.get', return_value=payload):
            response = self.client.post(ROOT + '/intents/' + payment['id'] + '/refresh', headers=HEADERS)
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(self.client.post(ROOT + '/intents', json=self.checkout(), headers=HEADERS).status_code, 503)

    def test_misspelled_activation_never_enables_manual_payment(self):
        with patch.dict(os.environ, {'MMPAY_ENABLED': 'tru'}):
            configuration = self.client.get(ROOT + '/config').get_json()
            self.assertTrue(configuration['enabled'])
            self.assertFalse(configuration['configured'])
            self.assertEqual(self.client.post('/api/sales', json=self.checkout()).status_code, 409)

    def test_branch_isolation_and_cashier_checkout_access(self):
        payment = self.begin()
        foreign = Branch(name='Payment foreign branch', code='MMP-' + uuid.uuid4().hex[:8], is_active=True)
        db.session.add(foreign)
        db.session.commit()
        try:
            with self.client.session_transaction() as s:
                s['branch_id'] = foreign.id
            self.assertEqual(self.client.get(ROOT + '/intents/' + payment['id']).status_code, 404)
            self.assertEqual(self.client.get(ROOT + '/intents').get_json()['payments'], [])
            with self.client.session_transaction() as s:
                s.update(branch_id=self.branch.id, role='cashier')
            self.assertEqual(self.client.get(ROOT + '/config').status_code, 200)
            self.assertEqual(self.client.get(ROOT + '/intents/' + payment['id']).status_code, 200)
            self.assertEqual(self.event(payment).status_code, 200)
            self.assertEqual(self.client.post(ROOT + '/intents/' + payment['id'] + '/complete', headers=HEADERS).status_code, 201)
        finally:
            db.session.delete(foreign)
            db.session.commit()

    def test_duplicate_confirmed_provider_reference_cannot_pay_two_orders(self):
        # Only one open order per user/branch is allowed now, so the second
        # order is created by another manager in the same branch.
        first = self.begin()
        other = User(username='mm-duplicate-' + uuid.uuid4().hex[:8], password='test-only',
                     role='manager', branch_id=self.branch.id)
        db.session.add(other)
        db.session.commit()
        try:
            with self.client.session_transaction() as s:
                s['user_id'] = other.id
            second = self.begin()
        finally:
            with self.client.session_transaction() as s:
                s['user_id'] = self.user.id
        self.assertEqual(self.event(first, transactionRefId='same-bank-transaction').status_code, 200)
        conflict = self.event(second, transactionRefId='same-bank-transaction')
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.get_json()['code'], 'reference_conflict')
        db.session.expire_all()
        self.assertEqual(db.session.get(PaymentIntent, second['id']).status, 'PENDING')
        db.session.delete(other)
        db.session.commit()

    def test_sandbox_manual_callbacks_can_pay_distinct_orders_without_reference_collision(self):
        # The sandbox console reuses this marker, not a unique bank transaction.
        # Reproduce the reported payload with two otherwise independent orders.
        first = self.begin()
        self.assertEqual(self.event(first, vendor='MMPAY_MANUAL', method='QR',
                                    transactionRefId='MMPAY_MANUAL').status_code, 200)
        self.assertEqual(self.client.post(ROOT + '/intents/' + first['id'] + '/complete',
                                         headers=HEADERS).status_code, 201)
        self.product.price = 5350
        self.product.tax_rate = 0
        db.session.commit()
        with patch('myanmyanpay.MyanMyanPayClient.create', side_effect=lambda order_id, amount, url: {
                **self.provider_create(order_id, amount, url), 'vendorQrRefId': '1792591566093'}):
            second = self.begin(self.checkout(items=[{'product_id': self.product.id,
                                                      'price': 5350, 'quantity': 1}]))
        callback = dict(vendor='MMPAY_MANUAL', method='QR', transactionRefId='MMPAY_MANUAL',
                        vendorQrRefId='1792591566093', customMessage='',
                        callbackUrl='https://pos.example/webhooks/myanmyanpay')
        response = self.event(second, **callback)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(self.event(second, **callback).get_json()['duplicate'])
        db.session.expire_all()
        one = db.session.get(PaymentIntent, first['id'])
        two = db.session.get(PaymentIntent, second['id'])
        self.assertEqual(two.status, 'SUCCESS')
        self.assertNotEqual(one.transaction_ref, two.transaction_ref)
        self.assertIsNone(two.sale_transaction_id, 'callbacks do not automatically create sales')
        response = self.client.get(ROOT + '/intents/' + second['id'])
        self.assertEqual(response.get_json()['payment']['status'], 'SUCCESS')

    def test_manual_sandbox_marker_is_never_accepted_with_live_credentials(self):
        payment = self.begin()
        with patch.dict(os.environ, {'MMPAY_PUBLISHABLE_KEY': 'pk_live_abc',
                                     'MMPAY_SECRET_KEY': 'sk_live_abc'}):
            payload = {'orderId': payment['order_id'], 'amount': 1050, 'currency': 'MMK',
                       'status': 'SUCCESS', 'vendorQrRefId': 'qr-' + payment['order_id'],
                       'transactionRefId': 'MMPAY_MANUAL', 'vendor': 'MMPAY_MANUAL', 'method': 'QR'}
            raw = json.dumps(payload, separators=(',', ':')).encode()
            nonce = 'live-manual-rejection'
            signature = hmac.new(b'sk_live_abc', nonce.encode() + b'.' + raw, hashlib.sha256).hexdigest()
            response = self.client.post('/webhooks/myanmyanpay', data=raw, content_type='application/json',
                headers={'X-Mmpay-Nonce': nonce, 'X-Mmpay-Signature': signature})
        self.assertEqual(response.status_code, 409)
        db.session.expire_all()
        self.assertEqual(db.session.get(PaymentIntent, payment['id']).status, 'PENDING')

    def test_sandbox_manual_callback_keeps_identity_checks_and_reports_safe_conflicts(self):
        payment = self.begin()
        manual = dict(transactionRefId='MMPAY_MANUAL', vendor='MMPAY_MANUAL', method='QR')
        for mismatch in ({'amount': 1}, {'currency': 'USD'}, {'vendorQrRefId': 'wrong-qr'}):
            response = self.event(payment, **dict(manual, **mismatch))
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.get_json()['code'], 'payment_mismatch')
            self.assertTrue(response.get_json()['message'])
            self.assertNotIn('sk_test', response.get_data(as_text=True))
        self.assertEqual(self.event(payment, **manual).status_code, 200)

    def test_legacy_sandbox_manual_reference_can_be_redelivered_and_recovered(self):
        payment = self.begin()
        intent = db.session.get(PaymentIntent, payment['id'])
        intent.status = 'SUCCESS'
        intent.transaction_ref = 'MMPAY_MANUAL'
        db.session.commit()
        response = self.event(payment, transactionRefId='MMPAY_MANUAL', vendor='MMPAY_MANUAL')
        self.assertEqual(response.status_code, 200, response.get_json())
        db.session.expire_all()
        self.assertTrue(db.session.get(PaymentIntent, payment['id']).transaction_ref.startswith('sandbox-manual:'))

    def test_provider_refresh_recovers_manual_sandbox_success_after_rejected_callback(self):
        payment = self.begin()
        payload = dict(self.provider_create(payment['order_id'], 1050, ''),
                       status='SUCCESS', transactionRefId='MMPAY_MANUAL', vendor='MMPAY_MANUAL')
        with patch('myanmyanpay.MyanMyanPayClient.get', return_value=payload):
            response = self.client.post(ROOT + '/intents/' + payment['id'] + '/refresh', headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['payment']['status'], 'SUCCESS')
        db.session.expire_all()
        intent = db.session.get(PaymentIntent, payment['id'])
        self.assertTrue(intent.transaction_ref.startswith('sandbox-manual:'))
        self.assertIsNone(intent.sale_transaction_id)

    def test_cancel_uses_provider_and_does_not_cancel_paid_orders(self):
        payment = self.begin()
        result = self.provider_create(payment['order_id'], 1050, '')
        result['status'] = 'CANCELLED'
        with patch('myanmyanpay.MyanMyanPayClient.cancel', return_value={'status': 'CANCELLED'}) as cancel, \
                patch('myanmyanpay.MyanMyanPayClient.get', return_value=result):
            response = self.client.post(ROOT + '/intents/' + payment['id'] + '/cancel', headers=HEADERS)
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(response.get_json()['payment']['status'], 'CANCELLED')
            cancel.assert_called_once_with(payment['order_id'])
        self.assertEqual(self.event(payment).status_code, 200)  # late payment is preserved
        self.assertEqual(self.client.post(ROOT + '/intents/' + payment['id'] + '/cancel', headers=HEADERS).status_code, 409)

    def test_concurrent_completions_consume_payment_and_stock_once(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        payment = self.begin()
        self.assertEqual(self.event(payment).status_code, 200)
        user_id, branch_id, product_id = self.user.id, self.branch.id, self.product.id
        barrier = threading.Barrier(2)

        def finish():
            client = app.test_client()
            with client.session_transaction() as s:
                s.update(user_id=user_id, role='manager', branch_id=branch_id)
            barrier.wait(timeout=10)
            response = client.post(ROOT + '/intents/' + payment['id'] + '/complete', headers=HEADERS)
            return response.status_code

        db.session.remove()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: finish(), range(2)))
        self.assertIn(201, results)
        self.assertTrue(all(status in (200, 201, 409) for status in results), results)
        db.session.expire_all()
        self.product = db.session.get(Product, product_id)
        self.assertEqual(Sale.query.filter_by(transaction_id=payment['transaction_id']).count(), 1)
        self.assertEqual(self.product.stock, 9)

    def test_stock_loss_keeps_paid_order_for_reconciliation(self):
        payment = self.begin()
        self.event(payment)
        self.product.stock = 0
        db.session.commit()
        result = self.client.post(ROOT + '/intents/' + payment['id'] + '/complete', headers=HEADERS)
        self.assertEqual(result.status_code, 400)
        db.session.expire_all()
        intent = db.session.get(PaymentIntent, payment['id'])
        self.assertEqual(intent.status, 'SUCCESS')
        self.assertIsNone(intent.sale_transaction_id)
        self.assertTrue(self.client.get(ROOT + '/intents').get_json()['payments'])

    def test_duplicate_product_lines_invalid_numbers_and_missing_header_rejected(self):
        data = self.checkout()
        self.assertEqual(self.client.post(ROOT + '/intents', json=data).status_code, 403)
        data['items'] *= 2
        self.assertEqual(self.client.post(ROOT + '/intents', json=data, headers=HEADERS).status_code, 400)
        for qty in (0, -1, 1.5, True):
            data = self.checkout()
            data['items'][0]['quantity'] = qty
            self.assertEqual(self.client.post(ROOT + '/intents', json=data, headers=HEADERS).status_code, 400)

    def test_other_owner_or_branch_cannot_read_complete_or_render_qr(self):
        payment = self.begin()
        with self.client.session_transaction() as s:
            s['user_id'] = self.user.id + 10000
        for suffix, method in (('', 'get'), ('/qr', 'get'), ('/complete', 'post'), ('/cancel', 'post')):
            self.assertEqual(getattr(self.client, method)(ROOT + '/intents/' + payment['id'] + suffix, headers=HEADERS).status_code, 404)
        with self.client.session_transaction() as s:
            s.clear()
        self.assertEqual(self.client.get(ROOT + '/intents').status_code, 401)

    def test_timeout_keeps_same_durable_order_and_refresh_recovers(self):
        self.create.side_effect = TimeoutError('do not leak provider secrets')
        data = self.checkout()
        result = self.client.post(ROOT + '/intents', json=data, headers=HEADERS)
        self.assertEqual(result.status_code, 502)
        payment = result.get_json()['payment']
        self.assertEqual(self.client.post(ROOT + '/intents', json=data, headers=HEADERS).status_code, 200)
        self.assertEqual(self.create.call_count, 1)
        payload = self.provider_create(payment['order_id'], 1050, '')
        with patch('myanmyanpay.MyanMyanPayClient.get', return_value=payload):
            response = self.client.post(ROOT + '/intents/' + payment['id'] + '/refresh', headers=HEADERS)
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(response.get_json()['payment']['status'], 'PENDING')
            self.assertEqual(self.client.post(ROOT + '/intents/' + payment['id'] + '/refresh', headers=HEADERS).status_code, 429)


if __name__ == '__main__':
    unittest.main()