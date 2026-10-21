"""Stateless MyanMyanPay HTTP adapter (no persistence or payment-status policy).

Protocol sources:
https://docs.myanmyanpay.com/api/
https://docs.myanmyanpay.com/sdk-python/
https://github.com/nawing/MMPay-Python-SDK/blob/master/mmpay/client.py

Unlike the upstream SDK, handshake tokens are local, errors are sanitized, and
webhook signatures are compared in constant time. Callers must persist intents,
match order/amount/currency, and handle webhook replay/idempotency themselves.
Never retry create automatically after an ambiguous transport failure.
"""

import hashlib
import hmac
import json
import math
import os
import re
import time
from decimal import Decimal
from urllib.parse import urlsplit

import requests


class PaymentConfigurationError(RuntimeError):
    """Disabled provider or invalid environment configuration (safe to display)."""


class PaymentProviderError(RuntimeError):
    """Sanitized transport/protocol failure; never contains response bodies.

    status_code is an HTTP integer, or None for transport/JSON/schema failures.
    A failure does not prove that the provider did not process the operation.
    """

    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def is_enabled():
    """Read MMPAY_ENABLED only; disabled by default, reject misspelled switches."""
    value = os.environ.get('MMPAY_ENABLED', '').strip().lower()
    if value in ('', '0', 'false', 'no', 'off'):
        return False
    if value in ('1', 'true', 'yes', 'on'):
        return True
    raise PaymentConfigurationError('MMPAY_ENABLED must be a boolean switch.')


def _https_url(value):
    # Reject parser normalization, credentials, query/fragment, and backslashes.
    if (not isinstance(value, str) or not value or
            any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value) or
            any(c in value for c in ('\\', '?', '#'))):
        return False
    try:
        parts = urlsplit(value)
        return (parts.scheme == 'https' and bool(parts.hostname) and
                parts.username is None and parts.password is None and
                parts.port != 0)
    except ValueError:
        return False


CONFIGURATION_NAMES = ('MMPAY_API_BASE_URL', 'MMPAY_APP_ID',
                       'MMPAY_PUBLISHABLE_KEY', 'MMPAY_SECRET_KEY')


def configuration_values():
    """Return the provider configuration currently present in the environment."""
    return {name: os.environ.get(name, '') for name in CONFIGURATION_NAMES}


class MyanMyanPayClient:
    """Environment-configured client; no session or per-request shared state.

    Configuration is captured at construction. Reload by calling from_env().
    Disabled construction needs no credentials unless for_webhook=True, which
    validates configuration for outstanding callbacks without enabling payments.
    Construction never performs network I/O.
    """

    TIMEOUT = (5, 20)  # connect/read seconds; requests does not impose a total deadline

    def __init__(self, *, for_webhook=False, for_recovery=False,
                 values=None, enabled=None):
        self.enabled = is_enabled() if enabled is None else bool(enabled)
        self.sandbox = False
        self._secret_key = None
        self._recovery = for_recovery
        if not self.enabled and not for_webhook and not for_recovery:
            return
        supplied = configuration_values() if values is None else values
        values = {}
        for name in CONFIGURATION_NAMES:
            value = supplied.get(name, '')
            if (not isinstance(value, str) or not value or
                    value != value.strip() or
                    any(ord(c) < 32 or ord(c) == 127 for c in value)):
                raise PaymentConfigurationError(f'{name} is missing or invalid.')
            values[name] = value
        if not _https_url(values['MMPAY_API_BASE_URL']):
            raise PaymentConfigurationError('MMPAY_API_BASE_URL must be a strict HTTPS base URL.')
        self._base_url = values['MMPAY_API_BASE_URL'].rstrip('/')
        self._app_id = values['MMPAY_APP_ID']
        self._publishable_key = values['MMPAY_PUBLISHABLE_KEY']
        self._secret_key = values['MMPAY_SECRET_KEY'].encode('utf-8')
        self.sandbox = '_test_' in self._publishable_key
        if self.sandbox != ('_test_' in values['MMPAY_SECRET_KEY']):
            raise PaymentConfigurationError('Publishable and secret keys must use the same environment.')
        try:
            self._publishable_key.encode('ascii')
        except UnicodeEncodeError:
            raise PaymentConfigurationError('MMPAY_PUBLISHABLE_KEY must be ASCII.') from None

    @classmethod
    def from_env(cls, *, for_webhook=False, for_recovery=False):
        """Load config; opt in to callback verification even while disabled.

        for_webhook=True requires the same valid configuration as an enabled
        client. It does not bypass the enabled gate for create/get/cancel.
        """
        return cls(for_webhook=for_webhook, for_recovery=for_recovery)

    @classmethod
    def from_values(cls, values, *, enabled=True):
        """Validate an explicit configuration mapping instead of the environment.

        The settings screen uses this to apply exactly the same rules as a live
        deployment before anything is stored. No network I/O is performed.
        """
        return cls(values=values, enabled=enabled)

    def _require_enabled(self, *, recovery=False):
        if not self.enabled and not (recovery and self._recovery):
            raise PaymentConfigurationError('MyanMyanPay is disabled.')

    def _signature(self, raw, nonce):
        return hmac.new(self._secret_key, nonce.encode('utf-8') + b'.' + raw,
                        hashlib.sha256).hexdigest()

    def verify_webhook(self, raw, nonce, signature):
        """Verify exact raw bytes, not parsed/re-serialized JSON. No replay check."""
        if (not self._secret_key or not isinstance(raw, bytes) or not raw or
                not isinstance(nonce, str) or not nonce or
                not isinstance(signature, str) or
                re.fullmatch(r'[0-9a-f]{64}', signature) is None):
            return False
        try:
            expected = self._signature(raw, nonce)
        except UnicodeEncodeError:
            return False
        return hmac.compare_digest(expected, signature)

    def _post(self, action, payload, nonce, btoken=None):
        raw = json.dumps(payload, separators=(',', ':'), allow_nan=False).encode('utf-8')
        headers = {
            'Authorization': 'Bearer ' + self._publishable_key,
            'Content-Type': 'application/json',
            'X-Mmpay-Nonce': nonce,
            'X-Mmpay-Signature': self._signature(raw, nonce),
        }
        if btoken is not None:
            headers['X-Mmpay-Btoken'] = btoken
        prefix = 'sandbox-' if self.sandbox else ''
        try:
            response = requests.post(
                f'{self._base_url}/payments/{prefix}{action}', data=raw,
                headers=headers, timeout=self.TIMEOUT, allow_redirects=False)
        except requests.RequestException:
            raise PaymentProviderError('MyanMyanPay transport failure.') from None
        try:
            if not 200 <= response.status_code < 300:
                raise PaymentProviderError('MyanMyanPay rejected the request.',
                                           status_code=response.status_code)
            try:
                data = response.json()
            except ValueError:
                raise PaymentProviderError('MyanMyanPay returned invalid JSON.') from None
            if not isinstance(data, dict) or not data or 'error' in data:
                raise PaymentProviderError('MyanMyanPay returned an invalid response.')
            return data
        finally:
            response.close()

    def _operation(self, action, order_id, **fields):
        self._require_enabled(recovery=action in ('get', 'cancel'))
        if not isinstance(order_id, str) or not order_id.strip() or len(order_id) > 32:
            raise ValueError('order_id must be a nonempty string of at most 32 characters.')
        nonce = str(time.time_ns() // 1_000_000)
        # As in the official SDK, the handshake body binds the operation nonce;
        # its own signature/header uses a separately generated nonce.
        handshake = self._post('handshake', {'orderId': order_id, 'nonce': nonce},
                               str(time.time_ns() // 1_000_000))
        btoken = handshake.get('token')
        if (not isinstance(btoken, str) or not btoken or
                any(ord(c) < 33 or ord(c) > 126 for c in btoken)):
            raise PaymentProviderError('MyanMyanPay returned an invalid handshake token.')
        payload = {'orderId': order_id, 'nonce': nonce, **fields}
        return self._post(action, payload, nonce, btoken)

    def create(self, order_id, amount, callback_url):
        """Return provider dict including nonempty qr (EMV payload, not an image).

        Positive int/float/Decimal amounts are accepted; Decimal is converted to
        a JSON number. Invalid call arguments raise ValueError before any I/O.
        """
        self._require_enabled()
        if isinstance(amount, bool) or not isinstance(amount, (int, float, Decimal)):
            raise ValueError('amount must be a positive finite number.')
        try:
            valid = math.isfinite(amount) and amount > 0
        except (ValueError, OverflowError):
            valid = False
        if not valid:
            raise ValueError('amount must be a positive finite number.')
        if not _https_url(callback_url):
            raise ValueError('callback_url must be a strict HTTPS URL.')
        if isinstance(amount, Decimal):
            amount = float(amount)
            if amount == 0:
                raise ValueError('amount is too small for a JSON number.')
        data = self._operation('create', order_id, appId=self._app_id,
                               amount=amount, callbackUrl=callback_url)
        if not isinstance(data.get('qr'), str) or not data['qr'].strip():
            raise PaymentProviderError('MyanMyanPay create response is missing the QR payload.')
        return data

    def get(self, order_id):
        return self._operation('get', order_id)

    def cancel(self, order_id):
        return self._operation('cancel', order_id)