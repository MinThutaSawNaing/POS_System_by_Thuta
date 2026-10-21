"""Offline protocol/contract tests. All HTTP is mocked; no app import needed."""

import hashlib
import hmac
import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest.mock import Mock, patch

import requests

from myanmyanpay import (
    MyanMyanPayClient, PaymentConfigurationError, PaymentProviderError, is_enabled,
)


ENV = {
    'MMPAY_ENABLED': 'true',
    'MMPAY_API_BASE_URL': 'https://provider.example/api/',
    'MMPAY_APP_ID': 'example-app',
    'MMPAY_PUBLISHABLE_KEY': 'pk_test_example',
    'MMPAY_SECRET_KEY': 'sk_test_example',
}


def response(data, status=200):
    result = Mock(status_code=status)
    result.json.return_value = data
    return result


class MyanMyanPayClientTests(unittest.TestCase):
    def test_overlength_order_id_rejected_before_network(self):
        with patch.dict(os.environ, ENV), patch('myanmyanpay.requests.post') as post:
            gateway = MyanMyanPayClient.from_env()
            for operation in (gateway.get, gateway.cancel):
                with self.assertRaisesRegex(ValueError, '32 characters'):
                    operation('POS-' + 'a' * 32)
            with self.assertRaisesRegex(ValueError, '32 characters'):
                gateway.create('POS-' + 'a' * 32, 1000, 'https://pos.example/webhooks/myanmyanpay')
            post.assert_not_called()

    def setUp(self):
        self.env = patch.dict(os.environ, ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        # This guard prevents accidental real network even in new test cases.
        self.network = patch('requests.sessions.Session.request',
                             side_effect=AssertionError('Real network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.http = patch('myanmyanpay.requests.post')
        self.post = self.http.start()
        self.addCleanup(self.http.stop)
        self.client = MyanMyanPayClient.from_env()

    def test_disabled_by_default_needs_no_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            client = MyanMyanPayClient.from_env()
            self.assertFalse(is_enabled())
            self.assertFalse(client.enabled)
            for operation in (lambda: client.create('o', 1, 'https://shop.example/cb'),
                              lambda: client.get('o'), lambda: client.cancel('o')):
                with self.assertRaises(PaymentConfigurationError):
                    operation()
            self.assertFalse(client.verify_webhook(b'{}', '1', '0' * 64))
        self.post.assert_not_called()

    def test_disabled_webhook_mode_verifies_but_blocks_operations(self):
        with patch.dict(os.environ, MMPAY_ENABLED='false'):
            client = MyanMyanPayClient.from_env(for_webhook=True)
        self.assertFalse(client.enabled)
        raw, nonce = b'{"orderId":"outstanding"}', '123'
        signature = hmac.new(ENV['MMPAY_SECRET_KEY'].encode(),
                             nonce.encode() + b'.' + raw, hashlib.sha256).hexdigest()
        self.assertTrue(client.verify_webhook(raw, nonce, signature))
        self.assertFalse(client.verify_webhook(raw + b' ', nonce, signature))
        self.assertFalse(client.verify_webhook(raw, None, None))
        for operation in (lambda: client.create('o', Decimal('1.25'), 'https://shop.example/cb'),
                          lambda: client.get('o'), lambda: client.cancel('o')):
            with self.assertRaises(PaymentConfigurationError):
                operation()
        self.post.assert_not_called()

    def test_disabled_webhook_mode_requires_valid_configuration(self):
        for name, value in (('MMPAY_SECRET_KEY', ''),
                            ('MMPAY_API_BASE_URL', 'http://provider.example'),
                            ('MMPAY_SECRET_KEY', 'sk_live_example')):
            with self.subTest(name=name), patch.dict(os.environ, {
                    'MMPAY_ENABLED': 'false', name: value}):
                with self.assertRaises(PaymentConfigurationError):
                    MyanMyanPayClient.from_env(for_webhook=True)
        self.post.assert_not_called()

    def test_switches_and_invalid_switch(self):
        for value in ('1', 'true', 'TRUE', 'yes', 'on'):
            with patch.dict(os.environ, MMPAY_ENABLED=value):
                self.assertTrue(is_enabled())
        for value in ('', '0', 'false', 'no', 'off'):
            with patch.dict(os.environ, MMPAY_ENABLED=value):
                self.assertFalse(is_enabled())
        with patch.dict(os.environ, MMPAY_ENABLED='tru'):
            with self.assertRaises(PaymentConfigurationError):
                MyanMyanPayClient.from_env()

    def test_required_configuration_and_sanitized_errors(self):
        for name in ENV.keys() - {'MMPAY_ENABLED'}:
            for value in ('', ' secret-value ', 'secret-value\r\n'):
                with self.subTest(name=name), patch.dict(os.environ, {name: value}):
                    with self.assertRaises(PaymentConfigurationError) as caught:
                        MyanMyanPayClient.from_env()
                    self.assertNotIn('secret-value', str(caught.exception))

    def test_strict_https_base_url(self):
        for url in ('http://provider.example', '//provider.example', 'https://',
                    'https://user:pass@provider.example', 'https://provider.example?q=1',
                    'https://provider.example#fragment', 'https://provider.example?',
                    'https://provider.example\\evil', ' https://provider.example',
                    'https://provider.example\n', 'https://provider.example:bad',
                    'https://provider.example:0'):
            with self.subTest(url=url), patch.dict(os.environ, MMPAY_API_BASE_URL=url):
                with self.assertRaises(PaymentConfigurationError):
                    MyanMyanPayClient.from_env()

    def test_mixed_key_environments_rejected(self):
        for name, key in (('MMPAY_PUBLISHABLE_KEY', 'pk_live_example'),
                          ('MMPAY_SECRET_KEY', 'sk_live_example')):
            with patch.dict(os.environ, {name: key}):
                with self.assertRaises(PaymentConfigurationError):
                    MyanMyanPayClient.from_env()

    def assert_signed(self, call):
        raw = call.kwargs['data']
        headers = call.kwargs['headers']
        expected = hmac.new(ENV['MMPAY_SECRET_KEY'].encode(),
                            headers['X-Mmpay-Nonce'].encode() + b'.' + raw,
                            hashlib.sha256).hexdigest()
        self.assertEqual(headers['X-Mmpay-Signature'], expected)
        self.assertEqual(headers['Authorization'], 'Bearer ' + ENV['MMPAY_PUBLISHABLE_KEY'])
        self.assertEqual(headers['Content-Type'], 'application/json')
        self.assertEqual(call.kwargs['timeout'], (5, 20))
        self.assertIs(call.kwargs['allow_redirects'], False)
        self.assertEqual(raw, json.dumps(json.loads(raw), separators=(',', ':')).encode())

    def test_integral_amounts_are_sent_as_json_integers(self):
        # Live probe: the provider answers 403 for 6400.0 but 200 for 6400.
        for amount, expected in ((Decimal('6400.00'), 6400), (6400, 6400),
                                 (Decimal('6400.50'), 6400.5)):
            with self.subTest(amount=amount):
                self.post.reset_mock()
                self.post.side_effect = [response({'token': 'one-time'}),
                                         response({'orderId': 'order-1', 'qr': 'q',
                                                   'status': 'PENDING'}, 201)]
                self.client.create('order-1', amount, 'https://shop.example/callback')
                body = json.loads(self.post.call_args_list[1].kwargs['data'])
                self.assertEqual(body['amount'], expected)
                self.assertIs(type(body['amount']), type(expected))

    def test_create_sdk_handshake_binding_and_qr_contract(self):
        result = {'orderId': 'order-1', 'amount': 1500.5, 'qr': '000201-EMV', 'status': 'PENDING'}
        hs, created = response({'token': 'one-time'}), response(result, 201)
        self.post.side_effect = [hs, created]
        with patch('myanmyanpay.time.time_ns', side_effect=[1000000, 2000000]):
            self.assertEqual(self.client.create('order-1', Decimal('1500.50'),
                                                'https://shop.example/callback'), result)
        handshake, create = self.post.call_args_list
        self.assertEqual(handshake.args[0], 'https://provider.example/api/payments/sandbox-handshake')
        self.assertEqual(create.args[0], 'https://provider.example/api/payments/sandbox-create')
        self.assertEqual(json.loads(handshake.kwargs['data']), {'orderId': 'order-1', 'nonce': '1'})
        self.assertEqual(handshake.kwargs['headers']['X-Mmpay-Nonce'], '2')
        self.assertNotIn('X-Mmpay-Btoken', handshake.kwargs['headers'])
        self.assertEqual(create.kwargs['headers']['X-Mmpay-Btoken'], 'one-time')
        self.assertEqual(json.loads(create.kwargs['data']), {
            'orderId': 'order-1', 'nonce': '1', 'appId': 'example-app',
            'amount': 1500.5, 'callbackUrl': 'https://shop.example/callback'})
        self.assert_signed(handshake)
        self.assert_signed(create)
        hs.close.assert_called_once()
        created.close.assert_called_once()

    def test_live_get_and_cancel_each_handshake_and_no_retained_state(self):
        with patch.dict(os.environ, MMPAY_PUBLISHABLE_KEY='pk_live_example',
                        MMPAY_SECRET_KEY='sk_live_example'):
            client = MyanMyanPayClient.from_env()
        before = vars(client).copy()
        self.post.side_effect = [response({'token': 'first'}), response({'status': 'PENDING'}),
                                 response({'token': 'second'}), response({'status': 'CANCELLED'})]
        self.assertEqual(client.get('order')['status'], 'PENDING')
        self.assertEqual(client.cancel('order')['status'], 'CANCELLED')
        self.assertEqual(vars(client), before)
        self.assertEqual([c.args[0].rsplit('/', 1)[1] for c in self.post.call_args_list],
                         ['handshake', 'get', 'handshake', 'cancel'])
        for call, token in zip(self.post.call_args_list[1::2], ('first', 'second')):
            self.assertEqual(call.kwargs['headers']['X-Mmpay-Btoken'], token)
            body = json.loads(call.kwargs['data'])
            self.assertEqual(set(body), {'orderId', 'nonce'})
            self.assertEqual(body['nonce'], call.kwargs['headers']['X-Mmpay-Nonce'])

    def test_concurrent_operations_do_not_share_handshake_tokens(self):
        def fake_post(url, **kwargs):
            body = json.loads(kwargs['data'])
            if url.endswith('handshake'):
                return response({'token': 'token-' + body['orderId']})
            self.assertEqual(kwargs['headers']['X-Mmpay-Btoken'], 'token-' + body['orderId'])
            return response({'orderId': body['orderId']})
        self.post.side_effect = fake_post
        before = vars(self.client).copy()
        with ThreadPoolExecutor(max_workers=4) as pool:
            orders = list(pool.map(self.client.get, ['a', 'b', 'c', 'd']))
        self.assertEqual([r['orderId'] for r in orders], ['a', 'b', 'c', 'd'])
        self.assertEqual(vars(self.client), before)

    def test_transport_errors_sanitized_without_retry(self):
        for error in (requests.Timeout('sensitive request'), requests.ConnectionError('sensitive request'),
                      requests.exceptions.InvalidHeader('sensitive request')):
            self.post.reset_mock()
            self.post.side_effect = error
            with self.assertRaises(PaymentProviderError) as caught:
                self.client.get('order')
            self.assertNotIn('sensitive', str(caught.exception))
            self.assertIsNone(caught.exception.status_code)
            self.assertTrue(caught.exception.__suppress_context__)
            self.assertEqual(self.post.call_count, 1)

    def test_http_errors_and_redirects_never_followed_or_disclosed(self):
        for status in (301, 302, 307, 400, 401, 429, 500, 503):
            self.post.reset_mock()
            failed = response({'secret': 'sensitive'}, status)
            self.post.return_value = failed
            with self.assertRaises(PaymentProviderError) as caught:
                self.client.get('order')
            self.assertEqual(caught.exception.status_code, status)
            self.assertNotIn('sensitive', str(caught.exception))
            self.assertEqual(self.post.call_count, 1)
            failed.json.assert_not_called()
            failed.close.assert_called_once()

    def test_invalid_json_response_and_handshake_tokens_fail_closed(self):
        invalid_json = response(None)
        invalid_json.json.side_effect = ValueError('sensitive body')
        for item in (invalid_json, response([]), response(None), response({}),
                     response({'error': 'sensitive body'}), response({'token': ''}),
                     response({'token': 123}), response({'token': 'bad\r\ntoken'}),
                     response({'token': '\u2603'})):
            self.post.reset_mock()
            self.post.return_value = item
            with self.assertRaises(PaymentProviderError) as caught:
                self.client.get('order')
            self.assertNotIn('sensitive', str(caught.exception))
            self.assertEqual(self.post.call_count, 1)

    def test_operation_errors_and_missing_qr(self):
        for data in ({'error': 'private'}, [], {'qr': None}, {'qr': ''}, {'qr': ' '}):
            self.post.side_effect = [response({'token': 'token'}), response(data)]
            with self.assertRaises(PaymentProviderError):
                self.client.create('order', 1000, 'https://shop.example/callback')

    def test_invalid_arguments_never_send_requests(self):
        for amount in (True, '1000', None, 0, -1, float('nan'), float('inf'),
                       Decimal('NaN'), Decimal('Infinity'), Decimal('1e-9999')):
            with self.subTest(amount=str(amount)), self.assertRaises(ValueError):
                self.client.create('order', amount, 'https://shop.example/cb')
        for order in ('', ' ', None, 1):
            for method in (self.client.get, self.client.cancel):
                with self.assertRaises(ValueError):
                    method(order)
        for callback in ('http://shop.example/cb', None, 'https://u:p@shop.example/cb'):
            with self.assertRaises(ValueError):
                self.client.create('order', 1000, callback)
        self.post.assert_not_called()

    def test_webhook_exact_raw_bytes_and_constant_time_comparison(self):
        raw = b'{ "orderId": "o", "message": "\xe1\x80\x80" }\n'
        nonce = '123456'
        signature = hmac.new(ENV['MMPAY_SECRET_KEY'].encode(), nonce.encode() + b'.' + raw,
                             hashlib.sha256).hexdigest()
        with patch('myanmyanpay.hmac.compare_digest', wraps=hmac.compare_digest) as compare:
            self.assertTrue(self.client.verify_webhook(raw, nonce, signature))
            compare.assert_called_once()
        self.assertFalse(self.client.verify_webhook(raw.rstrip(), nonce, signature))
        self.assertFalse(self.client.verify_webhook(raw, nonce + '1', signature))
        self.assertFalse(self.client.verify_webhook(raw, nonce, '0' * 64))
        for args in ((raw.decode(), nonce, signature), (b'', nonce, signature),
                     (raw, None, signature), (raw, nonce, None),
                     (raw, nonce, '\u2603' * 64), (raw, nonce, 'a' * 63),
                     (raw, '\ud800', signature)):
            self.assertFalse(self.client.verify_webhook(*args))
        self.post.assert_not_called()


if __name__ == '__main__':
    unittest.main()