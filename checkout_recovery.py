"""Pure checkout request validation and deterministic retry identities.

No application imports: transaction ownership and persistence belong to app.py.
The fingerprint covers the submitted intent, not mutable catalogue data.
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re


class CheckoutValidationError(ValueError):
    pass


def finite_amount(value, label):
    if isinstance(value, bool):
        raise CheckoutValidationError(f'{label} must be a finite non-negative number')
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise CheckoutValidationError(f'{label} must be a finite non-negative number') from None
    if not amount.is_finite() or not 0 <= amount <= Decimal('999999999999.99'):
        raise CheckoutValidationError(f'{label} must be a finite non-negative number')
    return amount


def positive_integer(value, label):
    if isinstance(value, bool) or not re.fullmatch(r'[0-9]+', str(value)):
        raise CheckoutValidationError(f'{label} must be a positive integer')
    if not 0 < int(value) <= 2147483647:
        raise CheckoutValidationError(f'{label} must be a positive integer')


def validate_checkout(data):
    if not isinstance(data, dict):
        raise CheckoutValidationError('Checkout must be a JSON object')
    rows = data.get('items')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 500:
        raise CheckoutValidationError('Items must contain between 1 and 500 lines')
    transaction_id = data.get('transaction_id')
    if transaction_id is not None and (not isinstance(transaction_id, str)
            or not transaction_id.strip() or transaction_id != transaction_id.strip()
            or len(transaction_id) > 128 or any(ord(c) < 32 for c in transaction_id)):
        raise CheckoutValidationError('Invalid transaction ID')
    for row in rows:
        if not isinstance(row, dict):
            raise CheckoutValidationError('Invalid checkout item')
        positive_integer(row.get('product_id'), 'Product ID')
        positive_integer(row.get('quantity'), 'Quantity')
        finite_amount(row.get('price'), 'Price')
        if 'tax_rate' in row:
            finite_amount(row['tax_rate'], 'Tax rate')
    if data.get('cash_received') is not None:
        finite_amount(data['cash_received'], 'Cash received')
    if data.get('customer_id') is not None:
        positive_integer(data['customer_id'], 'Customer ID')
    if 'payment_breakdown' in data:
        if not isinstance(data['payment_breakdown'], dict):
            raise CheckoutValidationError('Invalid payment breakdown')
        for value in data['payment_breakdown'].values():
            finite_amount(value, 'Payment amount')
    if 'delivery' in data:
        delivery = data['delivery']
        if not isinstance(delivery, dict):
            raise CheckoutValidationError('Invalid delivery')
        if 'enabled' in delivery and not isinstance(delivery['enabled'], bool):
            raise CheckoutValidationError('Delivery enabled flag must be boolean')
        for field in ('recipient_name', 'recipient_phone', 'delivery_address', 'township',
                      'instructions', 'courier_name', 'tracking_number', 'payment_status'):
            if delivery.get(field) is not None and not isinstance(delivery[field], str):
                raise CheckoutValidationError(f'Invalid delivery {field}')
    context = data.get('checkout_context')
    if context is not None:
        if not isinstance(context, dict) or not transaction_id:
            raise CheckoutValidationError('Checkout context requires a transaction ID')
        positive_integer(context.get('branch_id'), 'Checkout branch')
        positive_integer(context.get('user_id'), 'Checkout user')
        device = context.get('device_id')
        if not isinstance(device, str) or not 1 <= len(device) <= 128:
            raise CheckoutValidationError('Invalid checkout device')
        occurred = context.get('occurred_at')
        try:
            if not isinstance(occurred, str) or len(occurred) > 64:
                raise ValueError()
            timestamp = datetime.fromisoformat(occurred.replace('Z', '+00:00'))
            if timestamp.utcoffset() is None:
                raise ValueError()
        except (AttributeError, TypeError, ValueError):
            raise CheckoutValidationError('Checkout time must include a timezone') from None
        if not isinstance(context.get('offline'), bool):
            raise CheckoutValidationError('Checkout offline flag must be boolean')
        finite_amount(context.get('expected_total'), 'Expected total')
        if context['offline'] and (
                str(data.get('payment_method', 'cash')).strip().lower() != 'cash'
                or data.get('payment_breakdown') or data.get('coupon_code')
                or data.get('delivery', {}).get('enabled')):
            raise CheckoutValidationError('Offline checkout supports cash only, without coupons or delivery')
    # Reject NaN/Infinity anywhere, including optional nested metadata.
    try:
        json.dumps(data, allow_nan=False)
    except (ValueError, TypeError):
        raise CheckoutValidationError('Checkout must contain finite JSON values') from None


def checkout_fingerprint(data):
    def canonical(value):
        if isinstance(value, dict):
            return ['object', [[key, canonical(value[key])] for key in sorted(value)]]
        if isinstance(value, list):
            return ['array', [canonical(item) for item in value]]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return ['number', format(Decimal(str(value)).normalize(), 'f')]
        return [type(value).__name__, value]

    serialized = json.dumps(canonical(data), ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()