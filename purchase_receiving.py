"""Shared atomic purchase receiving for the HTTP API and authorized AI tools."""

from datetime import datetime
import hashlib
import json
import re
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError


def register_receiving_requests(db):
    """Durable optional retry receipts, independent of mutable PO status."""
    class ReceivingRequest(db.Model):
        __tablename__ = 'receiving_request'
        key = db.Column(db.String(128), primary_key=True)
        fingerprint = db.Column(db.String(64), nullable=False)
        response_json = db.Column(db.Text, nullable=False)
    return ReceivingRequest


def receive_purchase_order(db, models, po_id, items=None, *, receive_all=False,
                           idempotency_key=None, user_id=None):
    """Return (payload, HTTP status). Authorization belongs to the caller.

    Acquire the SQLite writer lock before reading quantities. Validation happens
    before any mutation and every failure rolls back the complete receipt.
    """
    PurchaseOrder = models['PurchaseOrder']
    WarehouseInventory = models['WarehouseInventory']
    ReceivingRequest = models.get('ReceivingRequest')

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
        if idempotency_key is not None:
            if (not isinstance(idempotency_key, str) or not idempotency_key.strip()
                    or len(idempotency_key) > 128 or ReceivingRequest is None):
                return fail('Invalid receiving idempotency key')
            idempotency_key = idempotency_key.strip()
        requested = {}
        if not receive_all:
            if not isinstance(items, list) or not items:
                return fail('No receiving items provided')
            for row in items:
                if not isinstance(row, dict):
                    return fail('Invalid receiving items payload')
                item_id = integer(row.get('purchase_order_item_id'))
                quantity = integer(row.get('received_qty'), zero=True)
                requested[item_id] = requested.get(item_id, 0) + quantity
    except (TypeError, ValueError):
        return fail('Receiving IDs and quantities must be whole non-negative numbers')

    try:
        db.session.rollback()
        db.session.execute(text('BEGIN IMMEDIATE'))
        fingerprint = hashlib.sha256(json.dumps(
            [po_id, sorted(requested.items()), bool(receive_all), user_id],
            separators=(',', ':')).encode()).hexdigest()
        if idempotency_key:
            prior = db.session.get(ReceivingRequest, idempotency_key)
            if prior:
                if prior.fingerprint != fingerprint:
                    return fail('Idempotency key already used for another receiving request', 409)
                result = json.loads(prior.response_json)
                result['replayed'] = True
                db.session.rollback()
                return result, 200
        po = db.session.get(PurchaseOrder, po_id)
        if not po:
            return fail('Purchase order not found', 404)
        if po.status not in ('approved', 'partially_received'):
            return fail(f'Cannot receive items for {po.status} purchase order')
        item_map = {item.id: item for item in po.items}
        if not item_map:
            return fail('This purchase order has no line items')
        if receive_all:
            requested = {item.id: max(item.ordered_qty - (item.received_qty or 0), 0)
                         for item in po.items}
        previously_received = any((item.received_qty or 0) > 0 for item in po.items)
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
        po.status = ('received' if all(item.received_qty >= item.ordered_qty for item in po.items)
                     else 'partially_received')
        if po.supplier and not previously_received:
            po.supplier.total_orders = (po.supplier.total_orders or 0) + 1
        result = {'success': True, 'message': 'Receiving recorded', 'po_id': po.id,
                  'po_number': po.po_number, 'status': po.status, 'received_lines': received_lines}
        if idempotency_key:
            db.session.add(ReceivingRequest(key=idempotency_key, fingerprint=fingerprint,
                                           response_json=json.dumps(result)))
        db.session.commit()
        return result, 200
    except (OperationalError, IntegrityError):
        return fail('Purchase inventory changed or is busy. Reload the order before retrying.', 409)
    except Exception:
        db.session.rollback()
        raise