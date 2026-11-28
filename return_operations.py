"""One transaction boundary for HTTP and AI merchandise returns/exchanges.

Call with a clean session. The service discards any preceding read transaction,
locks before inspecting balances, and owns the only commit (including accounting
and retry evidence). Register ReturnRequest before the application's create_all.
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re
from uuid import uuid4

import pytz
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError


CENT = Decimal('0.01')
MAX_MONEY = Decimal('999999999999.99')
MAX_ROWS = 500
SETTLEMENT_METHODS = {'cash', 'credit_card', 'debit_card', 'mobile_payment'}


def register_return_operations(db):
    """Return the retry model; registration is idempotent per SQLAlchemy db."""
    existing = getattr(db, 'return_request_model', None)
    if existing is not None:
        return existing

    class ReturnRequest(db.Model):
        __tablename__ = 'return_request'
        # No cascading FKs: deleting operational history must not permit replay.
        key = db.Column(db.String(128), primary_key=True)
        fingerprint = db.Column(db.String(64), nullable=False)
        result = db.Column(db.JSON, nullable=False)

    db.return_request_model = ReturnRequest
    return ReturnRequest


class _Failure(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


def _integer(value, label):
    if type(value) is not int or not 0 < value <= 2147483647:
        raise _Failure(f'{label} must be a positive integer')
    return value


def _money(value, label):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise _Failure(f'{label} must be a finite non-negative amount')
    if isinstance(value, str) and len(value) > 64:
        raise _Failure(f'{label} is too large')
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or not 0 <= amount <= MAX_MONEY:
            raise _Failure(f'{label} must be between 0 and {MAX_MONEY}')
        return amount.quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise _Failure(f'{label} must be a finite non-negative amount') from None


def _minor(value, label):
    return int(_money(value, label) * 100)


def _amount(minor):
    return float(Decimal(minor) / 100)


def _model(models, name, *, required=True):
    model = models.get(name)
    if model is None:
        # Old AI callers supply a model-only subset. Never omit debt/auth checks
        # just because a model was absent from that subset; use its own registry,
        # not an import of app (which could create a second application/database).
        for mapper in models['Sale'].registry.mappers:
            if mapper.class_.__name__ == name:
                model = mapper.class_
                break
    if model is None and required:
        raise RuntimeError('Required return model is unavailable')
    return model


def _validate(data):
    if not isinstance(data, dict):
        raise _Failure('Return/exchange must be a JSON object')
    try:
        json.dumps(data, allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        raise _Failure('Return/exchange must contain finite JSON values') from None
    transaction = data.get('original_transaction_id')
    if not isinstance(transaction, str) or not transaction.strip() or len(transaction) > 128:
        raise _Failure('Original transaction ID is required (maximum 128 characters)')
    key = data.get('idempotency_key')
    if key is not None and (not isinstance(key, str) or not key.strip()
                            or len(key) > 128 or any(ord(c) < 32 for c in key)):
        raise _Failure('Idempotency key must be a non-empty string of at most 128 characters')
    method = data.get('settlement_method', 'cash')
    if not isinstance(method, str) or method.strip().lower() not in SETTLEMENT_METHODS:
        raise _Failure('Invalid settlement method')
    notes = data.get('notes')
    if notes is not None and (not isinstance(notes, str) or len(notes.strip()) > 300):
        raise _Failure('Notes must be at most 300 characters')
    returns, exchanges = data.get('return_items'), data.get('exchange_items', [])
    if not isinstance(returns, list) or not returns or not isinstance(exchanges, list):
        raise _Failure('At least one return item is required; items must be lists')
    if len(returns) + len(exchanges) > MAX_ROWS:
        raise _Failure('A return/exchange can contain at most 500 rows')
    for rows, id_name in ((returns, 'sale_item_id'), (exchanges, 'product_id')):
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise _Failure('Each return/exchange item must be an object')
            item_id = _integer(row.get(id_name), id_name)
            _integer(row.get('quantity'), 'Quantity')
            if item_id in seen:
                raise _Failure(f'Duplicate {id_name} lines are not allowed')
            seen.add(item_id)
            if 'price' in row:
                _money(row['price'], 'Price')
    legacy = data.get('legacy_costs')
    if legacy is not None:
        if not isinstance(legacy, dict) or len(legacy) > MAX_ROWS:
            raise _Failure('legacy_costs must be an object of original sale item IDs and unit costs')
        selected = {str(row['sale_item_id']) for row in returns}
        for item_id, cost in legacy.items():
            if (not isinstance(item_id, str) or not re.fullmatch(r'[1-9][0-9]*', item_id)
                    or item_id not in selected or not isinstance(cost, str)):
                raise _Failure('legacy_costs must map returned original sale item IDs to unit cost strings')
            _money(cost, 'Legacy recovery unit cost')
    return transaction.strip(), returns, exchanges, method.strip().lower(), (notes or '').strip(), key


def _authorize(db, models, actor_id, branch_id, role):
    from flask import has_request_context, session

    if type(actor_id) is not int or actor_id <= 0:
        raise _Failure('Unauthorized', 401)
    if type(branch_id) is not int or branch_id <= 0:
        raise _Failure('An active branch is required', 403)
    if role not in ('cashier', 'manager', 'boss'):
        raise _Failure('Return/exchange access denied', 403)
    if has_request_context() and (
            session.get('user_id') != actor_id or session.get('branch_id') != branch_id
            or session.get('role') != role):
        raise _Failure('Return/exchange account or branch scope changed', 403)
    actor = db.session.get(_model(models, 'User'), actor_id)
    branch = db.session.get(_model(models, 'Branch'), branch_id)
    if actor is None or actor.role != role:
        raise _Failure('Return/exchange access denied', 403)
    if branch is None or not branch.is_active:
        raise _Failure('An active branch is required', 403)
    if role == 'cashier' and actor.branch_id != branch_id:
        raise _Failure('Return/exchange account or branch scope changed', 403)
    return actor, branch


def _allocate(total, weights):
    """Integer-cent largest remainder, stable in original sale-item ID order."""
    weight_sum = sum(weights)
    if not weight_sum:
        return [0] * len(weights)
    parts = [total * weight // weight_sum for weight in weights]
    order = sorted(range(len(weights)), key=lambda i: (-(total * weights[i] % weight_sum), i))
    for i in order[:total - sum(parts)]:
        parts[i] += 1
    return parts


def _cumulative(total, quantity, original_quantity):
    # Half-up in integer arithmetic; the last partial always gets the remainder.
    return (2 * total * quantity + original_quantity) // (2 * original_quantity)


def _refundable_lines(db, models, sale, items):
    snapshot = getattr(sale, 'receipt_snapshot', None)
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (TypeError, ValueError):
            snapshot = None
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    if 'delivery_fee' in snapshot:
        delivery_fee = _minor(snapshot['delivery_fee'], 'Original delivery fee')
    else:
        Delivery = _model(models, 'Delivery', required=False)
        delivery = db.session.query(Delivery).filter_by(sale_id=sale.id).first() if Delivery else None
        delivery_fee = (_minor(delivery.delivery_fee or 0, 'Original delivery fee')
                        if delivery and getattr(delivery, 'fee_included_in_sale', False) else 0)
    bases, taxes = [], []
    for item in items:
        _integer(item.quantity, 'Original quantity')
        bases.append(_minor(_money(item.price, 'Original price') * item.quantity, 'Original line total'))
        taxes.append(_minor(item.tax or 0, 'Original line tax'))
    # The saved sale tax is authoritative, including checkout's pre-coupon tax
    # semantics. Normally it equals the sum of the stored line taxes.
    sale_tax = _minor(sale.tax or 0, 'Original tax')
    if sum(taxes) != sale_tax:
        taxes = _allocate(sale_tax, taxes if sum(taxes) else bases)
    gross = [base + tax for base, tax in zip(bases, taxes)]
    paid = max(_minor(sale.total, 'Original total') - delivery_fee, 0)
    refundable = min(paid, sum(gross))
    if 'coupon_discount' in snapshot:
        refundable = min(refundable, max(sum(gross) - _minor(
            snapshot['coupon_discount'], 'Original coupon discount'), 0))
    # Equivalently allocate the discount by original gross weights. Keep original
    # tax unless the discounted credit itself is smaller (coupons can cover tax).
    credits = _allocate(refundable, gross)
    return {item.id: (credit - min(tax, credit), min(tax, credit))
            for item, credit, tax in zip(items, credits, taxes)}, refundable


def _promotion_prices(models, product):
    helper = models.get('active_promotion_prices')
    if callable(helper):
        return [_money(value, 'Promotion price') for value in helper(product)]
    # Model-only AI callers must enforce the same active Myanmar-time window.
    tz = pytz.timezone('Asia/Yangon')
    now = datetime.now(tz)
    prices = []
    for promotion in getattr(product, 'promotions', ()):
        start, end = promotion.start_date, promotion.end_date
        start = tz.localize(start) if start.tzinfo is None else start
        end = tz.localize(end) if end.tzinfo is None else end
        if not start <= now <= end:
            continue
        normal = _money(product.price, 'Product price')
        discount = _money(promotion.discount_value, 'Promotion discount')
        if promotion.discount_type == 'percent':
            price = normal - normal * discount / 100
        elif promotion.discount_type == 'fixed':
            price = normal - discount
        else:
            continue
        prices.append(_money(max(price, 0), 'Promotion price'))
    return prices


def _receipt_snapshot(models, sale, lines, actor, branch):
    builder = models.get('build_receipt_snapshot')
    if not callable(builder):
        return
    identity = models.get('get_receipt_identity')
    currency = models.get('get_currency_code')
    suffix = models.get('get_currency_suffix')
    snapshot = builder(
        transaction_id=sale.transaction_id, sale_date=sale.date, pos_name='Parrot POS',
        currency_code=currency() if callable(currency) else 'USD',
        currency_suffix=suffix() if callable(suffix) else '$',
        branch={field: getattr(branch, field, '') for field in ('name', 'code', 'address', 'phone', 'email')},
        cashier_name=actor.username, payment_method='exchange', cash_received=sale.cash_received,
        change_given=0, items=[{
            'product_id': line['product'].id, 'name': line['product'].name,
            'quantity': line['quantity'], 'unit_price': line['unit_price'],
            'tax_rate': line['tax_rate'], 'tax_amount': _amount(line['tax']),
        } for line in lines], subtotal=_amount(sum(line['base'] for line in lines)),
        tax=sale.tax, total=sale.total,
        receipt_identity=identity(branch) if callable(identity) else None)
    sale.receipt_snapshot = json.dumps(snapshot, ensure_ascii=False, default=str, allow_nan=False)


def process_return_exchange(db, models, data, *, actor_id, branch_id, role):
    """Return (JSON-safe result, status); accounting hooks must not commit."""
    def fail(message, status):
        db.session.rollback()
        return {'success': False, 'message': message, 'error': message}, status

    try:
        transaction, return_rows, exchange_rows, method, notes, key = _validate(data)
        # Rollback also expires cached ORM balances from request/AI preflight.
        db.session.rollback()
        db.session.execute(text('BEGIN IMMEDIATE'))
        actor, branch = _authorize(db, models, actor_id, branch_id, role)
        if data.get('legacy_costs') is not None and role not in ('manager', 'boss'):
            raise _Failure('Only a manager or boss can approve historical recovery costs', 403)
        Request = None
        fingerprint = None
        if key is not None:
            Request = getattr(db, 'return_request_model', None) or models.get('ReturnRequest')
            if Request is None:
                raise _Failure('Return retry storage is unavailable; retry after setup', 503)
            fingerprint = hashlib.sha256(json.dumps({
                'actor_id': actor_id, 'branch_id': branch_id, 'role': role,
                'request': {name: value for name, value in data.items() if name != 'idempotency_key'},
            }, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
            previous = db.session.get(Request, key)
            if previous is not None:
                if previous.fingerprint != fingerprint:
                    raise _Failure('Idempotency key was already used for a different return request', 409)
                result = dict(previous.result)
                db.session.rollback()
                return result, 201

        Sale, SaleItem, Product, Workflow, Item, Debt = (
            _model(models, name) for name in
            ('Sale', 'SaleItem', 'Product', 'ReturnExchange', 'ReturnExchangeItem', 'Debt'))
        sale = db.session.query(Sale).filter_by(transaction_id=transaction, branch_id=branch_id).first()
        if sale is None or (role == 'cashier' and sale.user_id != actor_id):
            raise _Failure('Original sale not found', 404)
        originals = db.session.query(SaleItem).filter_by(sale_id=sale.id).order_by(SaleItem.id).all()
        original_map = {item.id: item for item in originals}
        entitlements, goods_paid = _refundable_lines(db, models, sale, originals)
        prior = {}
        prior_rows = (db.session.query(Item).join(Workflow, Workflow.id == Item.return_exchange_id)
                      .filter(Workflow.original_sale_id == sale.id, Item.movement == 'return').all())
        for item in prior_rows:
            values = prior.setdefault(item.original_sale_item_id, [0, 0, 0])
            values[0] += _integer(item.quantity, 'Previously returned quantity')
            values[1] += _minor(item.line_total, 'Previous return total')
            values[2] += _minor(item.line_tax or 0, 'Previous return tax')
        prior_total = sum(_minor(value, 'Previous return credit') for (value,) in
                          db.session.query(Workflow.return_total).filter_by(original_sale_id=sale.id).all())
        remaining_credit = max(goods_paid - max(prior_total, sum(v[1] + v[2] for v in prior.values())), 0)
        return_lines = []
        for row in sorted(return_rows, key=lambda row: row['sale_item_id']):
            item_id, quantity = row['sale_item_id'], row['quantity']
            original = original_map.get(item_id)
            if original is None:
                raise _Failure(f'Return item {item_id} not found in original sale')
            returned, prior_base, prior_tax = prior.get(item_id, (0, 0, 0))
            available = max(original.quantity - returned, 0)
            if quantity > available:
                raise _Failure(f'Return qty exceeds available qty for item #{item_id}. Available: {available}')
            product = db.session.query(Product).filter_by(id=original.product_id, branch_id=branch_id).first()
            if product is None:
                raise _Failure('Product not found for return item', 404)
            base_cap, tax_cap = entitlements[item_id]
            base = max(_cumulative(base_cap, returned + quantity, original.quantity) - prior_base, 0)
            tax = max(_cumulative(tax_cap, returned + quantity, original.quantity) - prior_tax, 0)
            # Old workflows may already have over-refunded a discounted line.
            # Their credit is real: neither this line nor this sale can spend it again.
            credit = min(base + tax, max(base_cap + tax_cap - prior_base - prior_tax, 0), remaining_credit)
            tax = min(tax, credit)
            base = credit - tax
            remaining_credit -= credit
            original_base = _money(original.price, 'Original price') * original.quantity
            tax_rate = (_money(original.tax or 0, 'Original tax') / original_base * 100
                        if original_base else Decimal(0))
            return_lines.append(dict(original=original, product=product, quantity=quantity,
                unit_price=float((Decimal(base) / 100 / quantity).quantize(CENT, rounding=ROUND_HALF_UP)),
                tax_rate=float(tax_rate), base=base, tax=tax))

        exchange_lines = []
        for row in sorted(exchange_rows, key=lambda row: row['product_id']):
            product = db.session.query(Product).filter_by(id=row['product_id'], branch_id=branch_id).first()
            if product is None:
                raise _Failure(f"Exchange product {row['product_id']} not found", 404)
            quantity = row['quantity']
            if quantity > (product.stock or 0):
                raise _Failure(f'Insufficient stock for exchange product {product.name}. Available: {product.stock}')
            price = _money(row.get('price', product.price), 'Exchange price')
            normal = _money(product.price, 'Product price')
            if role == 'cashier' and price != normal and not any(
                    abs(price - allowed) <= CENT for allowed in _promotion_prices(models, product)):
                raise _Failure('Invalid price or expired promotion')
            tax_rate = Decimal(str(product.tax_rate or 0))
            base = _minor(price * quantity, 'Exchange line total')
            tax = _minor(Decimal(base) / 100 * tax_rate / 100, 'Exchange line tax')
            exchange_lines.append(dict(product=product, quantity=quantity, unit_price=float(price),
                                       tax_rate=float(tax_rate), base=base, tax=tax))
        return_total = sum(line['base'] + line['tax'] for line in return_lines)
        exchange_total = sum(line['base'] + line['tax'] for line in exchange_lines)
        _money(Decimal(exchange_total) / 100, 'Exchange total')
        debt_credits = []
        available_credit = return_total
        debts = db.session.query(Debt).filter(Debt.sale_id == sale.id, Debt.balance > 0).order_by(Debt.id).all()
        for debt in debts:
            if not available_credit:
                break
            balance = _minor(debt.balance, 'Debt balance')
            applied = min(balance, available_credit)
            if not applied:
                continue
            debt.balance = _amount(balance - applied)
            status_helper = models.get('calculate_debt_status')
            debt.status = (status_helper(debt) if callable(status_helper) else
                           'paid' if not debt.balance else
                           'partial' if _minor(debt.balance, 'Debt balance') < _minor(debt.amount, 'Debt amount') else 'pending')
            debt_credits.append({'debt_id': debt.id, 'amount_minor': applied})
            available_credit -= applied
        debt_credit = return_total - available_credit
        refund = max(available_credit - exchange_total, 0)
        collected = max(exchange_total - available_credit, 0)
        if debt_credit:
            label = f'Credit allocated to original sale debt: {Decimal(debt_credit) / 100:.2f}; not a cash refund.'
            notes = label + ('\n' + notes[:max(0, 299 - len(label))] if notes else '')
        adjustment = None
        if exchange_lines:
            adjustment = Sale(transaction_id=str(uuid4()), date=datetime.now(pytz.timezone('Asia/Yangon')),
                total=_amount(exchange_total), tax=_amount(sum(line['tax'] for line in exchange_lines)),
                cash_received=_amount(collected) if collected else None, refund_amount=0.0,
                payment_method='exchange', user_id=actor_id, branch_id=sale.branch_id,
                customer_id=getattr(sale, 'customer_id', None))
            db.session.add(adjustment)
            db.session.flush()
            for line in exchange_lines:
                db.session.add(SaleItem(sale_id=adjustment.id, product_id=line['product'].id,
                    quantity=line['quantity'], price=line['unit_price'], tax=_amount(line['tax'])))
                line['product'].stock -= line['quantity']
            _receipt_snapshot(models, adjustment, exchange_lines, actor, branch)
        workflow = Workflow(workflow_id=str(uuid4()), mode='exchange' if exchange_lines else 'return',
            original_sale_id=sale.id, adjustment_sale_id=adjustment.id if adjustment else None,
            return_total=_amount(return_total), exchange_total=_amount(exchange_total),
            net_total=_amount(exchange_total - return_total), refund_amount=_amount(refund),
            collected_amount=_amount(collected), settlement_method=method, notes=notes or None, user_id=actor_id)
        db.session.add(workflow)
        db.session.flush()
        for movement, lines in (('return', return_lines), ('exchange', exchange_lines)):
            for line in lines:
                if movement == 'return':
                    line['product'].stock = (line['product'].stock or 0) + line['quantity']
                db.session.add(Item(return_exchange_id=workflow.id,
                    original_sale_item_id=line['original'].id if movement == 'return' else None,
                    product_id=line['product'].id, movement=movement, quantity=line['quantity'],
                    unit_price=line['unit_price'], tax_rate=line['tax_rate'],
                    line_total=_amount(line['base']), line_tax=_amount(line['tax'])))
        workflow._accounting_debt_credits = debt_credits
        workflow._accounting_legacy_costs = data.get('legacy_costs')
        db.session.flush()
        accounting = getattr(db, 'accounting', None)
        if accounting is not None:
            # post_return owns exchange revenue/COGS too: NEVER post Sale separately.
            accounting.post_return(workflow, legacy_costs=data.get('legacy_costs'))
        result = dict(success=True, message='Return/exchange processed successfully',
            workflow_id=workflow.workflow_id, mode=workflow.mode, original_transaction_id=transaction,
            adjustment_transaction_id=adjustment.transaction_id if adjustment else None,
            return_total=workflow.return_total, exchange_total=workflow.exchange_total,
            net_total=workflow.net_total, refund_amount=workflow.refund_amount,
            collected_amount=workflow.collected_amount, settlement_method=method,
            debt_credit_amount=_amount(debt_credit), debt_credits=debt_credits)
        if Request is not None:
            db.session.add(Request(key=key, fingerprint=fingerprint, result=result))
        db.session.commit()
        return result, 201
    except _Failure as exc:
        return fail(exc.message, exc.status)
    except (OperationalError, IntegrityError):
        return fail('Return/exchange is busy or changed. Retry with the same idempotency key.', 409)
    except Exception as exc:
        # Import lazily: legacy installations and model-only tests need no ledger.
        try:
            from accounting import AccountingError
        except ImportError:
            AccountingError = ()
        if isinstance(exc, AccountingError):
            status = getattr(exc, 'status_code', getattr(exc, 'status', 400))
            return fail(str(exc), status if type(status) is int and 400 <= status < 600 else 400)
        return fail('Failed to process return/exchange', 500)
