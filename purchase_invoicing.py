"""Record a matched supplier invoice without posting inventory or payment."""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import joinedload


def invoice_payload(invoice, *, include_items=False):
    payload = {
        'supplier_reference': invoice.supplier_reference,
        'invoice_date': invoice.invoice_date.isoformat(),
        'total_amount': f'{Decimal(invoice.total_minor) / 100:.2f}',
        'currency': invoice.currency,
        'created_at': invoice.created_at.isoformat(),
    }
    if include_items:
        payload.update(invoice.snapshot)
        payload['actor_id'] = invoice.actor_id
    return payload


def create_purchase_invoice(db, models, po_id, data, *, actor_id, get_currency):
    def fail(message, status=400):
        db.session.rollback()
        return {'success': False, 'message': message}, status

    if not isinstance(data, dict) or set(data) != {'supplier_reference', 'invoice_date', 'total_amount'}:
        return fail('Supplier invoice reference, date and total are required')
    reference = data['supplier_reference']
    if not isinstance(reference, str) or not 1 <= len(reference.strip()) <= 120 or any(
            not character.isprintable() for character in reference):
        return fail('Enter a supplier invoice reference of 1–120 characters')
    reference = reference.strip()
    try:
        invoice_date = date.fromisoformat(data['invoice_date'])
    except (ValueError, TypeError):
        return fail('Enter an invoice date in YYYY-MM-DD format')
    amount = data['total_amount']
    if not isinstance(amount, str) or not re.fullmatch(r'[0-9]{1,12}(\.[0-9]{1,2})?', amount):
        return fail('Enter a non-negative invoice total with up to two decimal places')
    minor = int(Decimal(amount) * 100)

    Invoice, PO, Line = (models[name] for name in ('PurchaseInvoice', 'PurchaseOrder', 'PurchaseOrderItem'))
    try:
        db.session.rollback()
        db.session.execute(text('BEGIN IMMEDIATE'))
        actor = db.session.get(models['User'], actor_id) if actor_id else None
        if actor is None or actor.role not in ('manager', 'boss'):
            return fail('Manager or boss access required', 403)
        po = db.session.get(PO, po_id)
        if po is None:
            return fail('Purchase order not found', 404)
        previous = db.session.get(Invoice, po_id)
        if previous:
            if (previous.supplier_reference, previous.invoice_date, previous.total_minor) == (reference, invoice_date, minor):
                result = invoice_payload(previous)
                db.session.rollback()
                return {'success': True, 'invoice': result, 'replayed': True}, 200
            return fail('This purchase order already has a purchase invoice', 409)
        if po.status != 'received' or not po.supplier:
            return fail('Only fully received purchase orders with a supplier can be invoiced', 409)
        lines = Line.query.filter_by(purchase_order_id=po.id).options(joinedload(Line.product)).order_by(
            Line.id).limit(501).all()
        if len(lines) > 500:
            return fail('This legacy purchase order exceeds the 500-line invoice limit', 413)
        if not lines or any(not line.product or line.ordered_qty <= 0 or line.received_qty != line.ordered_qty
                            for line in lines):
            return fail('The purchase order lines do not show a complete receipt', 409)
        total, items = Decimal(0), []
        for line in lines:
            cost = Decimal(str(line.unit_cost))
            if not cost.is_finite() or cost < 0:
                return fail('The purchase order contains an invalid cost', 409)
            total += cost * line.received_qty
            items.append({'product_id': line.product_id, 'product_name': line.product.name,
                          'sku': line.product.sku, 'branch_id': line.product.branch_id,
                          'quantity': line.received_qty, 'unit_cost': str(cost)})
        matched = total.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        ordered = Decimal(str(po.total_amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if matched != ordered or matched * 100 != minor:
            return fail('The supplier invoice total must match the fully received purchase order', 409)
        currency = get_currency()
        invoice = Invoice(purchase_order_id=po.id, supplier_id=po.supplier_id, supplier_reference=reference,
                          invoice_date=invoice_date, total_minor=minor, currency=currency, actor_id=actor.id,
                          created_at=datetime.utcnow(), snapshot={'po_number': po.po_number,
                          'supplier_name': po.supplier.name, 'items': items})
        db.session.add(invoice)
        result = invoice_payload(invoice)
        db.session.commit()
        return {'success': True, 'invoice': result, 'replayed': False}, 201
    except (InvalidOperation, ValueError):
        return fail('The purchase order contains an invalid cost or total', 409)
    except IntegrityError:
        return fail('This supplier invoice reference is already recorded for that supplier', 409)
    except OperationalError:
        return fail('Purchase order is busy. Reload before retrying.', 409)
    except Exception:
        db.session.rollback()
        raise