"""Shared atomic purchase receiving for the HTTP API and authorized AI tools."""

from datetime import datetime
import hashlib
import json
import re
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import joinedload


MAX_RECEIPT_ITEMS = 500


def receive_purchase_order(db, models, po_id, items=None, *, receive_all=False,
                           idempotency_key=None, actor_id=None):
    """Return (payload, HTTP status). Authorization belongs to the caller.

    Acquire the SQLite writer lock before reading quantities. Validation happens
    before any mutation and every failure rolls back the complete receipt.
    Keyed successes are stored in that same transaction; retries replay before
    checking PO status. Callers must authorize the actor on every attempt.
    """
    PurchaseOrder = models['PurchaseOrder']
    WarehouseInventory = models['WarehouseInventory']

    def integer(value, *, zero=False):
        if isinstance(value, bool) or not re.fullmatch(r'[0-9]+', str(value)):
            raise ValueError()
        number = int(value)
        if number < (0 if zero else 1) or number > 2147483647:
            raise ValueError()
        return number

    def fail(message, status=400):
        db.session.rollback()
        return {'success': False, 'message': message}, status

    try:
        po_id = integer(po_id)
        if idempotency_key is not None and (
                not isinstance(idempotency_key, str) or not idempotency_key.strip()
                or len(idempotency_key) > 128):
            return fail('Idempotency key must be a non-empty string of at most 128 characters')
        if not isinstance(receive_all, bool):
            return fail('receive_all must be a boolean')
        requested = {}
        if not receive_all:
            if not isinstance(items, list) or not items:
                return fail('No receiving items provided')
            if len(items) > MAX_RECEIPT_ITEMS:
                return fail('A receipt can contain at most 500 items; receive this order in batches')
            for row in items:
                if not isinstance(row, dict):
                    return fail('Invalid receiving items payload')
                item_id = integer(row.get('purchase_order_item_id'))
                quantity = integer(row.get('received_qty'), zero=True)
                requested[item_id] = requested.get(item_id, 0) + quantity
                if requested[item_id] > 2147483647:
                    raise ValueError()
    except (TypeError, ValueError):
        return fail('Receiving IDs and quantities must be whole non-negative numbers')

    try:
        db.session.rollback()
        db.session.execute(text('BEGIN IMMEDIATE'))
        ReceiptRequest = None
        fingerprint = None
        if idempotency_key is not None:
            ReceiptRequest = models['ReceiptRequest']
            fingerprint = hashlib.sha256(json.dumps({
                'actor_id': actor_id, 'po_id': po_id, 'receive_all': receive_all,
                'quantities': sorted(requested.items()),
            }, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
            previous = db.session.get(ReceiptRequest, idempotency_key)
            if previous is not None:
                if previous.fingerprint != fingerprint:
                    return fail('Idempotency key was already used for a different receiving request', 409)
                result = json.loads(previous.response_json)
                result['replayed'] = True
                db.session.rollback()
                return result, 200
        po = db.session.get(PurchaseOrder, po_id)
        if not po:
            return fail('Purchase order not found', 404)
        if po.status not in ('approved', 'partially_received'):
            return fail(f'Cannot receive items for {po.status} purchase order')
        # Older AI caller maps do not contain the line model. Resolve it from
        # mapper metadata, never by materializing the PO's items collection.
        PurchaseOrderItem = models.get('PurchaseOrderItem')
        if PurchaseOrderItem is None:
            PurchaseOrderItem = PurchaseOrder.items.property.mapper.class_
        lines = db.session.query(PurchaseOrderItem).filter(
            PurchaseOrderItem.purchase_order_id == po_id)
        received = func.coalesce(PurchaseOrderItem.received_qty, 0)

        def has_lines(query):
            return db.session.query(query.exists()).scalar()

        if receive_all:
            selected = lines.filter(received < PurchaseOrderItem.ordered_qty).options(
                joinedload(PurchaseOrderItem.product)).limit(MAX_RECEIPT_ITEMS + 1).all()
            if len(selected) > MAX_RECEIPT_ITEMS:
                return fail('More than 500 pending items; receive this order in batches of at most 500')
            requested = {item.id: max(item.ordered_qty - (item.received_qty or 0), 0)
                         for item in selected}
        else:
            selected = lines.filter(PurchaseOrderItem.id.in_(requested)).options(
                joinedload(PurchaseOrderItem.product)).all()
        item_map = {item.id: item for item in selected}
        if not item_map and not has_lines(lines):
            return fail('This purchase order has no line items')
        previously_received = has_lines(lines.filter(received > 0))
        for item_id, quantity in requested.items():
            item = item_map.get(item_id)
            if not item:
                return fail(f'Invalid PO item: {item_id}')
            if not item.product:
                return fail('Purchase order contains a missing product', 409)
            remaining = item.ordered_qty - (item.received_qty or 0)
            if quantity > remaining:
                return fail(f'Receive qty exceeds remaining for {item.product.name}. Remaining: {remaining}')
        if not any(requested.values()):
            return fail('At least one received quantity must be positive')

        received_lines = []
        for item_id, quantity in requested.items():
            if quantity == 0:
                continue
            item = item_map[item_id]
            item.received_qty = (item.received_qty or 0) + quantity
            db.session.add(WarehouseInventory(
                product_id=item.product_id, quantity=quantity,
                batch_number=po.po_number, received_date=datetime.utcnow(),
                unit_cost=item.unit_cost, branch_id=item.product.branch_id,
            ))
            if item.unit_cost and item.unit_cost > 0:
                item.product.cost = item.unit_cost
            received_lines.append({'purchase_order_item_id': item_id, 'received_qty': quantity})
        db.session.flush()
        po.status = ('partially_received' if has_lines(
            lines.filter(received < PurchaseOrderItem.ordered_qty)) else 'received')
        if not previously_received and po.supplier:
            po.supplier.total_orders = (po.supplier.total_orders or 0) + 1
        result = {'success': True, 'message': 'Receiving recorded', 'po_id': po.id,
                  'po_number': po.po_number, 'status': po.status, 'received_lines': received_lines,
                  'replayed': False}
        if ReceiptRequest is not None:
            db.session.add(ReceiptRequest(
                key=idempotency_key, fingerprint=fingerprint,
                response_json=json.dumps(result, sort_keys=True, separators=(',', ':'))))
        db.session.commit()
        return result, 200
    except (OperationalError, IntegrityError):
        return fail('Purchase inventory changed or is busy. Reload the order before retrying.', 409)
    except Exception:
        db.session.rollback()
        raise