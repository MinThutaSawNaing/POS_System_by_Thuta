"""Atomic customer-debt collections shared by HTTP and authorized AI callers."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from accounting import AccountingError


MONEY_QUANT = Decimal('0.01')
MAX_MONEY = Decimal('999999999999.99')


def _integer(value, label):
    if isinstance(value, bool) or not re.fullmatch(r'[0-9]+', str(value)):
        raise ValueError(f'{label} must be a positive integer')
    value = int(value)
    if not 0 < value <= 2147483647:
        raise ValueError(f'{label} must be a positive integer')
    return value


def _amount(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('Invalid payment amount')
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError()
        result = result.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
        if result <= 0 or result > MAX_MONEY:
            raise ValueError()
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('Payment amount must be greater than 0 and have a valid monetary value') from None


def _status(debt, balance):
    if balance <= 0:
        return 'paid'
    if debt.due_date:
        due = debt.due_date
        if due.tzinfo:
            due = due.astimezone(timezone.utc).replace(tzinfo=None)
        if datetime.utcnow() > due:
            return 'overdue'
    return 'partial' if balance < Decimal(str(debt.amount)) else 'pending'


def record_debt_payment(db, models, debt_id, amount, *, notes=None, idempotency_key=None,
                        actor_id=None, branch_id=None):
    """Return (payload, status); the caller owns role authorization and branch scope."""
    try:
        debt_id = _integer(debt_id, 'Debt ID')
        branch_id = _integer(branch_id, 'Branch ID')
        actor_id = _integer(actor_id, 'Actor ID')
        payment_amount = _amount(amount)
        if idempotency_key is not None:
            if (not isinstance(idempotency_key, str) or not idempotency_key.strip()
                    or len(idempotency_key) > 128):
                raise ValueError('Idempotency key must be a non-empty string of at most 128 characters')
            idempotency_key = idempotency_key.strip()
        if notes is not None:
            if not isinstance(notes, str) or len(notes) > 500:
                raise ValueError('Payment notes must be a string of at most 500 characters')
            notes = notes.strip() or None
    except ValueError as exc:
        return {'success': False, 'message': str(exc)}, 400

    def fail(message, status=400):
        db.session.rollback()
        return {'success': False, 'message': message}, status

    try:
        db.session.rollback()
        accounting = getattr(db, 'accounting', None)
        if accounting is not None:
            accounting.write_lock()
        else:
            db.session.execute(text('BEGIN IMMEDIATE'))
        accounting_active = accounting is not None and accounting.config() is not None
        if db.session.get(models['User'], actor_id) is None:
            return fail('Authenticated user not found', 403)
        debt = db.session.query(models['Debt']).filter_by(
            id=debt_id, branch_id=branch_id).first()
        if debt is None:
            return fail('Debt record not found', 404)

        amount_text = format(payment_amount, '.2f')
        fingerprint = None
        receipt_key = None
        source_key = None
        location = accounting.location(debt.branch_id) if accounting_active else None
        if accounting_active and idempotency_key is not None:
            source_key = 'collection:' + idempotency_key
            receipt_key = 'debt-payment:' + idempotency_key
            fingerprint = hashlib.sha256(json.dumps({
                'debt_id': debt.id, 'branch_id': debt.branch_id,
                'amount': amount_text, 'notes': notes,
            }, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
            previous = accounting.Command.query.filter_by(key=receipt_key).first()
            if previous is not None:
                if previous.fingerprint != fingerprint:
                    return fail('Idempotency key was already used for a different payment', 409)
                result = dict(previous.response, replayed=True)
                db.session.rollback()
                return result, 200
            journal = accounting._find_source(accounting.Journal, source_key)
            if journal is not None:
                actual = sorted((line.location_id, line.account_code, line.amount_minor, line.reference)
                                for line in journal.lines)
                expected = sorted(((location.id, 'CASH', int(payment_amount * 100), None),
                                   (location.id, 'AR', -int(payment_amount * 100), f'debt:{debt.id}')))
                if journal.source_type != 'collection' or actual != expected:
                    return fail('Idempotency key was already used for a different collection', 409)
                total_paid = db.session.query(func.coalesce(func.sum(models['DebtPayment'].amount), 0)).filter_by(
                    debt_id=debt.id).scalar()
                result = {'success': True, 'replayed': True, 'remaining_balance': debt.balance,
                          'total_paid': float(total_paid or 0), 'debt_status': debt.status}
                db.session.rollback()
                return result, 200

        current_balance = Decimal(str(debt.balance)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
        if current_balance <= 0:
            return fail('This debt is already fully paid')
        if payment_amount > current_balance:
            return fail(f'Payment amount {amount_text} exceeds remaining balance {current_balance:.2f}')

        remaining_balance = (current_balance - payment_amount).quantize(MONEY_QUANT)
        payment = models['DebtPayment'](
            debt_id=debt.id, customer_id=debt.customer_id, amount=float(payment_amount),
            notes=notes, processed_by=actor_id, branch_id=debt.branch_id)
        if accounting_active:
            payment._accounting_actor_id = actor_id
            if idempotency_key is not None:
                payment._accounting_key = idempotency_key
                payment._accounting_date = accounting.today()
                payment._accounting_account = 'CASH'
        db.session.add(payment)
        debt.balance = float(remaining_balance)
        debt.status = _status(debt, remaining_balance)

        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M')
        payment_note = f'Payment of {amount_text} received'
        if notes:
            payment_note += f' - {notes}'
        existing_notes = debt.communication_notes or ''
        debt.communication_notes = (
            f'{existing_notes}\n[{timestamp}] {payment_note}' if existing_notes
            else f'[{timestamp}] {payment_note}')
        db.session.flush()
        if accounting_active:
            accounting.require_location(location.id, active=True)
            accounting.post_collection(payment)
            if idempotency_key is not None and accounting._find_source(accounting.Journal, source_key) is None:
                raise AccountingError('Debt collection did not post its journal', 500)

        total_paid = db.session.query(func.coalesce(func.sum(models['DebtPayment'].amount), 0)).filter_by(
            debt_id=debt.id).scalar()
        result = {
            'success': True, 'message': f'Payment of {amount_text} recorded successfully',
            'debt_id': debt.id, 'customer_id': debt.customer_id, 'payment_id': payment.id,
            'amount_paid': amount_text, 'remaining_balance': float(remaining_balance),
            'total_paid': float(total_paid or 0), 'debt_status': debt.status,
            'changed_fields': ['balance', 'status', 'communication_notes'],
        }
        if receipt_key is not None:
            accounting._add_immutable(accounting.Command(
                key=receipt_key, fingerprint=fingerprint, response=result))
        db.session.commit()
        return result, 200
    except AccountingError as exc:
        return fail(exc.message, exc.status)
    except (IntegrityError, OperationalError):
        return fail('Debt changed or is busy. Reload before retrying.', 409)
    except SQLAlchemyError:
        db.session.rollback()
        message = 'Failed to record payment'
        return {'success': False, 'message': message}, 500
    except Exception:
        db.session.rollback()
        raise
