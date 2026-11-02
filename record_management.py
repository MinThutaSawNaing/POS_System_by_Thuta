"""History maintenance shared by HTTP and AI callers.

Public functions return (JSON-safe payload, HTTP status), own their transaction,
and require a persisted manager/boss actor. Call with a clean session: like the
receiving service, these operations roll back any prior transaction before
acquiring the writer lock. No stock, payment, supplier statistics, or durable
retry ledgers are reversed. ``items`` on edit is a full replacement (max 500).
Models may be passed from the app/AI context; dependency checks also consult the
SQLAlchemy registry so omitting a model cannot bypass financial safety checks.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re

from sqlalchemy import or_, text
from sqlalchemy.exc import SQLAlchemyError
from flask import has_request_context, request


class _Failure(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


def _integer(value, label):
    if isinstance(value, bool) or not re.fullmatch(r'[0-9]+', str(value)):
        raise _Failure(f'{label} must be a positive integer')
    value = int(value)
    if not 0 < value <= 2147483647:
        raise _Failure(f'{label} must be a positive integer')
    return value


def _model(models, name):
    if name in models:
        return models[name]
    # PaymentIntent is registered by payment_routes, not an app-level global.
    anchor = models['User']
    for mapper in anchor.registry.mappers:
        if mapper.class_.__name__ == name:
            return mapper.class_
    raise RuntimeError(f'Required safety model is not registered: {name}')


def _operation(db, models, actor_id, work):
    try:
        db.session.rollback()
        if db.engine.dialect.name == 'sqlite':
            db.session.execute(text('BEGIN IMMEDIATE'))
        actor = db.session.get(_model(models, 'User'), actor_id) if actor_id else None
        if actor is None or actor.role not in ('manager', 'boss'):
            raise _Failure('Manager or boss access required', 403)
        result = work()
        # HTTP/AI POST requests are captured by the app's ORM audit listener.
        # Direct trusted-context callers have no request, so add equivalent
        # actor-attributed evidence in the same transaction here.
        if not has_request_context() or request.method in ('GET', 'HEAD', 'OPTIONS'):
            Audit = _model(models, 'AuditLog')
            is_sale = 'transaction_id' in result
            deleting = 'deleted' in result['message']
            db.session.add(Audit(actor_user_id=actor.id, actor_username=actor.username,
                actor_role=actor.role, branch_id=getattr(actor, 'branch_id', None),
                category='sales' if is_sale else 'purchases',
                action='delete' if deleting else 'update',
                entity_type='Sale' if is_sale else 'PurchaseOrder',
                entity_id=str(result.get('transaction_id', result.get('purchase_order_id'))),
                summary=result['message']))
        db.session.commit()
        return dict(success=True, **result), 200
    except _Failure as exc:
        db.session.rollback()
        payload = {'success': False, 'message': exc.message, 'error': exc.message,
                   'status_code': exc.status}
        if 'confirmation required' in exc.message:
            payload['requires_confirmation'] = True
        return payload, exc.status
    except SQLAlchemyError:
        db.session.rollback()
        message = 'Record changed, is busy, or has linked records. Reload before retrying.'
        return {'success': False, 'message': message, 'error': message, 'status_code': 409}, 409
    except Exception:
        db.session.rollback()
        raise


def _po(db, models, po_id):
    po = db.session.query(_model(models, 'PurchaseOrder')).filter_by(
        id=_integer(po_id, 'Purchase order ID')).with_for_update().first()
    if po is None:
        raise _Failure('Purchase order not found', 404)
    return po


def edit_purchase_order(db, models, po_id, data, *, actor_id):
    """Edit supplier_id/notes/expected_delivery_date/items; received POs: metadata only.

    Each replacement item requires product_id, ordered_qty (or quantity), and
    unit_cost. Optional id/purchase_order_item_id must belong to this PO.
    Received quantity and lifecycle status are never writable here.
    """
    def work():
        po = _po(db, models, po_id)
        if not isinstance(data, dict) or not data:
            raise _Failure('A non-empty JSON object is required')
        if set(data) - {'supplier_id', 'notes', 'expected_delivery_date', 'items'}:
            raise _Failure('Unsupported purchase order fields')
        changes = {}
        if 'supplier_id' in data:
            supplier_id = _integer(data['supplier_id'], 'Supplier ID')
            if db.session.get(_model(models, 'Supplier'), supplier_id) is None:
                raise _Failure('Supplier not found', 404)
            Line = _model(models, 'PurchaseOrderItem')
            if supplier_id != po.supplier_id and (po.status in ('received', 'partially_received') or
                    db.session.query(Line).filter(Line.purchase_order_id == po.id,
                                                  Line.received_qty > 0).first() is not None):
                raise _Failure('Received purchase order supplier cannot change; only notes and date may be edited', 409)
            changes['supplier_id'] = supplier_id
        if 'notes' in data:
            notes = data['notes']
            if notes is not None and (not isinstance(notes, str) or len(notes) > 300):
                raise _Failure('Notes must be a string of at most 300 characters')
            changes['notes'] = (notes or '').strip() or None
        if 'expected_delivery_date' in data:
            value = data['expected_delivery_date']
            parsed = None
            if value not in (None, ''):
                if not isinstance(value, str):
                    raise _Failure('Invalid expected delivery date; use ISO 8601')
                try:
                    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                    if parsed.tzinfo:
                        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
                except ValueError:
                    raise _Failure('Invalid expected delivery date; use ISO 8601')
            changes['expected_delivery_date'] = parsed
        Line = _model(models, 'PurchaseOrderItem')
        lines = db.session.query(Line).filter_by(purchase_order_id=po.id)
        replacements = None
        if 'items' in data:
            if (po.status in ('received', 'partially_received') or
                    lines.filter(Line.received_qty > 0).first() is not None):
                raise _Failure('Received purchase orders allow metadata edits only; items cannot change', 409)
            if po.status == 'cancelled':
                raise _Failure('Cancelled purchase order items cannot be changed', 409)
            rows = data['items']
            if not isinstance(rows, list) or not 1 <= len(rows) <= 500:
                raise _Failure('Items must contain between 1 and 500 lines')
            existing = {line.id: line for line in lines.all()}
            seen_ids, seen_products, replacements = set(), set(), []
            total = Decimal('0')
            for row in rows:
                if not isinstance(row, dict) or set(row) - {
                        'id', 'purchase_order_item_id', 'product_id', 'ordered_qty', 'quantity', 'unit_cost'}:
                    raise _Failure('Invalid purchase order item fields')
                pid = _integer(row.get('product_id'), 'Product ID')
                if pid in seen_products:
                    raise _Failure('Duplicate products are not allowed')
                seen_products.add(pid)
                if db.session.get(_model(models, 'Product'), pid) is None:
                    raise _Failure(f'Product not found: {pid}', 404)
                if 'quantity' in row and 'ordered_qty' in row and row['quantity'] != row['ordered_qty']:
                    raise _Failure('Conflicting quantity fields')
                qty = _integer(row.get('ordered_qty', row.get('quantity')), 'Ordered quantity')
                try:
                    cost = Decimal(str(row.get('unit_cost')))
                except (InvalidOperation, ValueError):
                    raise _Failure('Unit cost must be a finite non-negative number')
                if not cost.is_finite() or cost < 0 or cost > Decimal('999999999999.99'):
                    raise _Failure('Unit cost must be finite and between 0 and 999999999999.99')
                line_id = row.get('id', row.get('purchase_order_item_id'))
                if 'id' in row and 'purchase_order_item_id' in row and row['id'] != row['purchase_order_item_id']:
                    raise _Failure('Conflicting item IDs')
                if line_id is not None:
                    line_id = _integer(line_id, 'Item ID')
                    if line_id not in existing or line_id in seen_ids:
                        raise _Failure('Item ID must belong to this order and must not repeat')
                    seen_ids.add(line_id)
                replacements.append((existing.get(line_id), pid, qty, float(cost)))
                total += qty * cost
            if total > Decimal('999999999999.99'):
                raise _Failure('Purchase order total is too large')
            # Every field and line has been checked before the first mutation.
            for line in existing.values():
                if line.id not in seen_ids:
                    db.session.delete(line)
            for line, pid, qty, cost in replacements:
                if line is None:
                    line = Line(purchase_order_id=po.id, received_qty=0)
                    db.session.add(line)
                line.product_id, line.ordered_qty, line.unit_cost = pid, qty, cost
            po.total_amount = float(total)
        for field, value in changes.items():
            setattr(po, field, value)
        return {'message': 'Purchase order updated', 'purchase_order_id': po.id,
                'total_amount': po.total_amount, 'changed_fields': list(data)}
    return _operation(db, models, actor_id, work)


def delete_purchase_order(db, models, po_id, *, confirm=False, actor_id):
    """Delete history only. Warehouse batch references and receipt retry ledger survive."""
    def work():
        if confirm is not True:
            raise _Failure('Explicit confirmation required: permanently delete purchase order history? '
                           'Stock and payments are not reversed; reports change. Send confirm=true.')
        po = _po(db, models, po_id)
        for line in db.session.query(_model(models, 'PurchaseOrderItem')).filter_by(purchase_order_id=po.id):
            db.session.delete(line)
        # WarehouseInventory references the immutable batch number, not a PO FK.
        # Never delete movements or ReceiptRequest, even for fully received POs.
        db.session.delete(po)
        return {'message': 'Purchase order history deleted; stock and receipts unchanged',
                'purchase_order_id': po.id, 'changed_fields': ['deleted']}
    return _operation(db, models, actor_id, work)


def delete_sale(db, models, transaction_id, *, branch_id, confirm=False, actor_id):
    """Delete active-branch sale history, blocking linked financial/delivery workflows."""
    def work():
        if confirm is not True:
            raise _Failure('Explicit confirmation required: permanently delete sale history? '
                           'Stock and payments are not reversed; reports change. Send confirm=true.')
        if branch_id is None:
            raise _Failure('Active branch required', 403)
        if not isinstance(transaction_id, str) or not transaction_id.strip():
            raise _Failure('Transaction ID is required')
        sale = db.session.query(_model(models, 'Sale')).filter_by(
            transaction_id=transaction_id, branch_id=_integer(branch_id, 'Branch ID')).with_for_update().first()
        if sale is None:
            raise _Failure('Sale not found in active branch', 404)
        Return = _model(models, 'ReturnExchange')
        checks = [
            ('returns or exchanges', db.session.query(Return).filter(or_(
                Return.original_sale_id == sale.id, Return.adjustment_sale_id == sale.id))),
            ('debt or debt payments', db.session.query(_model(models, 'Debt')).filter_by(sale_id=sale.id)),
            ('delivery workflow', db.session.query(_model(models, 'Delivery')).filter_by(sale_id=sale.id)),
        ]
        Intent = _model(models, 'PaymentIntent')
        checks.append(('payment intent', db.session.query(Intent).filter(or_(
            Intent.transaction_id == transaction_id, Intent.sale_transaction_id == transaction_id))))
        Item = _model(models, 'SaleItem')
        ReturnItem = _model(models, 'ReturnExchangeItem')
        checks.append(('return items', db.session.query(ReturnItem).filter(
            ReturnItem.original_sale_item_id.in_(db.session.query(Item.id).filter_by(sale_id=sale.id)))))
        for label, query in checks:
            if query.first() is not None:
                raise _Failure(f'Cannot delete sale linked to {label}; linked workflows must be retained', 409)
        for item in db.session.query(Item).filter_by(sale_id=sale.id):
            db.session.delete(item)
        # Sale.transaction_id was the offline idempotency guard. Keep a durable
        # tombstone so an old queued sync cannot deduct stock a second time.
        Tombstone = _model(models, 'DeletedSaleRequest')
        db.session.add(Tombstone(transaction_id=sale.transaction_id, branch_id=sale.branch_id))
        db.session.delete(sale)
        return {'message': 'Sale history deleted; stock and payments unchanged',
                'transaction_id': transaction_id, 'changed_fields': ['deleted']}
    return _operation(db, models, actor_id, work)