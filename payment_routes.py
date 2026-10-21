"""Persistent MyanMyanPay checkout boundary; no browser assertions mark money paid.

Provider callbacks update payment state only. Sale finalization reuses the normal
validated stock/sale transaction with an atomic, single-use payment claim.
"""

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from flask import Response, jsonify, request, session, url_for
from sqlalchemy.exc import IntegrityError


def payment_enabled():
    # Unknown switches fail CLOSED (configured error), never silently restore
    # the legacy manual-confirmation path after a deployment typo.
    return os.environ.get('MMPAY_ENABLED', '').strip().lower() not in ('', '0', 'false', 'no', 'off')


def register_payment_routes(app, db, pos):
    class PaymentIntent(db.Model):
        __tablename__ = 'payment_intent'
        id = db.Column(db.String(36), primary_key=True)
        order_id = db.Column(db.String(80), unique=True, nullable=False)
        transaction_id = db.Column(db.String(128), unique=True, nullable=False)
        branch_id = db.Column(db.Integer, nullable=False, index=True)
        user_id = db.Column(db.Integer, nullable=False, index=True)
        amount = db.Column(db.String(40), nullable=False)
        currency = db.Column(db.String(3), nullable=False, default='MMK')
        status = db.Column(db.String(20), nullable=False, default='CREATING')
        checkout_json = db.Column(db.Text, nullable=False)
        checkout_hash = db.Column(db.String(64), nullable=False)
        qr_payload = db.Column(db.Text)
        vendor_qr_ref = db.Column(db.String(160))
        transaction_ref = db.Column(db.String(160), unique=True)
        sale_transaction_id = db.Column(db.String(128), unique=True)
        created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
        updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
        last_checked_at = db.Column(db.DateTime)

    class PaymentWebhookEvent(db.Model):
        __tablename__ = 'payment_webhook_event'
        id = db.Column(db.Integer, primary_key=True)
        digest = db.Column(db.String(64), nullable=False, unique=True)
        order_id = db.Column(db.String(80), nullable=False, index=True)
        status = db.Column(db.String(20), nullable=False)
        received_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def client(for_webhook=False, for_recovery=False):
        from myanmyanpay import MyanMyanPayClient
        if for_webhook:
            return MyanMyanPayClient.from_env(for_webhook=True)
        if for_recovery:
            return MyanMyanPayClient.from_env(for_recovery=True)
        return MyanMyanPayClient.from_env()

    def callback_url():
        value = os.environ.get('MMPAY_CALLBACK_URL', '').strip()
        parsed = urlparse(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or parsed.path != '/webhooks/myanmyanpay'):
            raise ValueError('Set MMPAY_CALLBACK_URL to your public HTTPS /webhooks/myanmyanpay URL.')
        return value

    def access(write=False):
        if 'user_id' not in session:
            return jsonify(success=False, message='Unauthorized'), 401
        if session.get('role') not in ('manager', 'boss', 'cashier'):
            return jsonify(success=False, message='Account role is not authorized'), 403
        if not pos['get_current_branch_id']():
            return jsonify(success=False, message='Active branch required'), 403
        if write and request.headers.get('X-POS-Payment-Request') != '1':
            return jsonify(success=False, message='Payment request header required'), 403
        return None

    def enabled_error():
        if not payment_enabled():
            return jsonify(success=False, message='MyanMyanPay is not enabled'), 503
        try:
            callback_url()
            client()
            if pos['get_currency_code']() != 'MMK':
                raise ValueError('MyanMyanPay checkout requires branch currency MMK.')
        except Exception:
            return jsonify(success=False, message='MyanMyanPay is not configured. Ask your manager to check the server configuration.'), 503
        return None

    def owned(payment_id):
        return PaymentIntent.query.filter_by(
            id=payment_id, branch_id=pos['get_current_branch_id'](), user_id=session['user_id']).first()

    ORDER_WINDOW = timedelta(minutes=15)

    def _epoch_ms(value):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp() * 1000)

    def public(intent):
        created = intent.created_at or datetime.utcnow()
        return {
            'id': intent.id, 'order_id': intent.order_id, 'transaction_id': intent.transaction_id,
            'status': intent.status, 'amount': intent.amount, 'currency': intent.currency,
            'sale_transaction_id': intent.sale_transaction_id,
            'qr_url': url_for('mmpay_qr', payment_id=intent.id) if intent.qr_payload else None,
            'created_at': created.isoformat(),
            # Compliance timer: 15 minutes from order creation. Epoch milliseconds
            # plus the server clock let the browser correct for clock skew.
            'expires_at': _epoch_ms(created + ORDER_WINDOW),
            'server_now': _epoch_ms(datetime.utcnow()),
            'message': ('Payment received. Complete this saved checkout; do not pay again.'
                        if intent.status == 'SUCCESS' and not intent.sale_transaction_id else None),
        }

    def canonical(data):
        return json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)

    def prepare_checkout(raw):
        if not isinstance(raw, dict):
            raise ValueError('Checkout must be a JSON object.')
        allowed = {'items', 'payment_method', 'payment_breakdown', 'transaction_id', 'delivery', 'customer_id'}
        if set(raw) - allowed:
            raise ValueError('Unsupported mobile checkout fields.')
        data = json.loads(canonical(raw))
        txn = data.get('transaction_id')
        if not isinstance(txn, str) or not 1 <= len(txn.strip()) <= 128:
            raise ValueError('A stable transaction_id is required.')
        data['transaction_id'] = txn.strip()
        items = data.get('items')
        if not isinstance(items, list) or not 1 <= len(items) <= 200:
            raise ValueError('Checkout needs 1 to 200 product lines.')
        seen = set()
        for item in items:
            if not isinstance(item, dict) or set(item) - {'product_id', 'quantity', 'price', 'tax_rate'}:
                raise ValueError('Invalid checkout line.')
            pid, qty = item.get('product_id'), item.get('quantity')
            if type(pid) is not int or pid < 1 or pid in seen or type(qty) is not int or qty <= 0:
                raise ValueError('Each product must have one line with a positive integer quantity.')
            seen.add(pid)
            try:
                price = Decimal(str(item.get('price')))
                if not price.is_finite() or price < 0:
                    raise ValueError('Invalid price.')
            except InvalidOperation:
                raise ValueError('Invalid price.') from None
        if data.get('payment_method') not in ('mobile_payment', 'split_payment'):
            raise ValueError('Select mobile payment or a split containing mobile payment.')
        delivery = data.get('delivery')
        if delivery:
            if not isinstance(delivery, dict):
                raise ValueError('Invalid delivery.')
            if delivery.get('enabled') and any(not isinstance(delivery.get(k), str) or not delivery[k].strip()
                                               for k in ('recipient_name', 'recipient_phone', 'delivery_address')):
                raise ValueError('Recipient name, phone and address are required for delivery.')
        quote = pos['_create_sale_transaction'](data, quote_only=True)
        if not isinstance(quote, dict) or 'mobile_amount' not in quote:
            return None, quote
        amount = Decimal(quote['mobile_amount'])
        if not amount.is_finite() or amount <= 0:
            raise ValueError('Mobile payment amount must be positive.')
        return (data, format(amount.quantize(Decimal('0.01')), 'f')), None

    def apply_provider(intent, payload):
        """Use exact order/amount/currency and pinned references; preserve paid state.

        An authenticated late success must be recoverable even if an earlier
        cancellation/expiry arrived first. Old PENDING/FAILED events cannot undo
        SUCCESS, and a refund is never converted back into spendable payment.
        """
        if not isinstance(payload, dict) or payload.get('orderId') != intent.order_id:
            raise ValueError('Provider order does not match.')
        try:
            amount = Decimal(str(payload.get('amount')))
        except InvalidOperation:
            raise ValueError('Provider amount is invalid.') from None
        if not amount.is_finite() or amount != Decimal(intent.amount) or payload.get('currency') != intent.currency:
            raise ValueError('Provider amount or currency does not match.')
        status = payload.get('status')
        if status not in {'PENDING', 'SUCCESS', 'FAILED', 'REFUNDED', 'CANCELLED', 'EXPIRED'}:
            raise ValueError('Unsupported provider status.')
        vendor_ref = payload.get('vendorQrRefId')
        tx_ref = payload.get('transactionRefId')
        for ref in (vendor_ref, tx_ref):
            if ref is not None and (not isinstance(ref, str) or not 1 <= len(ref) <= 160):
                raise ValueError('Invalid provider reference.')
        if intent.vendor_qr_ref and vendor_ref != intent.vendor_qr_ref:
            raise ValueError('Provider QR reference does not match.')
        if intent.transaction_ref and tx_ref and tx_ref != intent.transaction_ref:
            raise ValueError('Provider transaction reference does not match.')
        if status == 'SUCCESS' and (not tx_ref or not vendor_ref):
            raise ValueError('Successful payment requires provider references.')
        if vendor_ref:
            intent.vendor_qr_ref = vendor_ref
        # A create response may contain a deprecated transactionRefId: only pin
        # the real transaction reference on confirmed SUCCESS/REFUNDED.
        if tx_ref and status in ('SUCCESS', 'REFUNDED'):
            intent.transaction_ref = tx_ref
        if intent.status == 'REFUNDED':
            pass
        elif status in ('SUCCESS', 'REFUNDED') or intent.status not in ('SUCCESS', 'FAILED', 'CANCELLED', 'EXPIRED'):
            intent.status = status
        qr = payload.get('qr')
        if qr is not None:
            if not isinstance(qr, str) or not 1 <= len(qr.encode('utf-8')) <= 2900:
                raise ValueError('Invalid provider QR payload.')
            intent.qr_payload = qr
        intent.updated_at = datetime.utcnow()

    def lock_intent(intent):
        PaymentIntent.query.filter_by(id=intent.id).update(
            {'updated_at': datetime.utcnow()}, synchronize_session=False)
        db.session.refresh(intent)

    @app.route('/api/payments/myanmyanpay/config')
    def mmpay_config():
        denied = access()
        if denied:
            return denied
        enabled = payment_enabled()
        problem = enabled_error() if enabled else None
        return jsonify(enabled=enabled, configured=enabled and problem is None, currency='MMK',
                       message='MyanMyanPay needs server configuration.' if problem else None)

    @app.route('/api/payments/myanmyanpay/intents', methods=['GET', 'POST'])
    def mmpay_intents():
        denied = access(request.method == 'POST')
        if denied:
            return denied
        if request.method == 'GET':
            rows = PaymentIntent.query.filter_by(
                branch_id=pos['get_current_branch_id'](), user_id=session['user_id'],
                sale_transaction_id=None).filter(PaymentIntent.status.in_(
                    ('CREATING', 'PENDING', 'SUCCESS'))).order_by(PaymentIntent.created_at.desc()).limit(50).all()
            return jsonify(success=True, payments=[public(row) for row in rows])
        problem = enabled_error()
        if problem:
            return problem
        try:
            prepared, error = prepare_checkout(request.get_json(silent=True))
            if error is not None:
                return error
            data, amount = prepared
            serialized = canonical(data)
            digest = hashlib.sha256(serialized.encode()).hexdigest()
            existing = PaymentIntent.query.filter_by(transaction_id=data['transaction_id']).first()
            if existing:
                if existing.branch_id != pos['get_current_branch_id']() or existing.user_id != session['user_id']:
                    return jsonify(success=False, message='Checkout reference unavailable.'), 409
                if existing.checkout_hash != digest:
                    return jsonify(success=False, message='Checkout changed. Resume or cancel the original payment first.'), 409
                return jsonify(success=True, payment=public(existing)), 200
            if pos['Sale'].query.filter_by(transaction_id=data['transaction_id']).first():
                return jsonify(success=False, message='Checkout reference already used by a sale.'), 409
            # Strict re-request policy: a user/branch may hold only one open order.
            # Refreshing, reopening the modal or reloading the page can never mint a
            # second QR; the caller must resume (or explicitly cancel) the active one.
            active_intent = PaymentIntent.query.filter_by(
                branch_id=pos['get_current_branch_id'](), user_id=session['user_id'],
                sale_transaction_id=None).filter(PaymentIntent.status.in_(
                    ('CREATING', 'PENDING', 'SUCCESS'))).order_by(
                    PaymentIntent.created_at.desc()).first()
            if active_intent:
                return jsonify(success=False, payment=public(active_intent),
                               message='An active payment already exists. Resume it, or cancel it explicitly before creating another.'), 409
            intent = PaymentIntent(id=str(uuid.uuid4()), order_id=uuid.uuid4().hex,
                                   transaction_id=data['transaction_id'], branch_id=pos['get_current_branch_id'](),
                                   user_id=session['user_id'], amount=amount, currency='MMK',
                                   checkout_json=serialized, checkout_hash=digest, status='CREATING')
            db.session.add(intent)
            db.session.commit()  # durable order BEFORE calling the provider
            try:
                payload = client().create(intent.order_id, Decimal(intent.amount), callback_url())
                # A callback can beat the create response. Reload durable state.
                lock_intent(intent)
                apply_provider(intent, payload)
                db.session.commit()
            except Exception:
                db.session.rollback()
                # Never invent a new order on an ambiguous network failure.
                return jsonify(success=False, payment=public(intent),
                               message='Payment creation is uncertain. Resume this order and check status; do not create another payment.'), 502
            return jsonify(success=True, payment=public(intent)), 201
        except (ValueError, TypeError, ArithmeticError, KeyError):
            db.session.rollback()
            return jsonify(success=False, message='Invalid checkout. Check items, amounts and payment method.'), 400
        except IntegrityError:
            db.session.rollback()
            return jsonify(success=False, message='Checkout already exists. Resume the existing payment.'), 409

    @app.route('/api/payments/myanmyanpay/intents/<payment_id>')
    def mmpay_intent(payment_id):
        denied = access()
        if denied:
            return denied
        intent = owned(payment_id)
        return (jsonify(success=True, payment=public(intent)) if intent else
                (jsonify(success=False, message='Payment not found'), 404))

    def sync_provider(payment_id, cancel=False):
        denied = access(True)
        if denied:
            return denied
        intent = owned(payment_id)
        if not intent:
            return jsonify(success=False, message='Payment not found'), 404
        if cancel and (intent.status in ('SUCCESS', 'REFUNDED') or intent.sale_transaction_id):
            return jsonify(success=False, message='Paid orders cannot be cancelled here. Reconcile/refund separately.'), 409
        # Older releases generated 36-character IDs. The provider rejects these
        # at handshake validation, before create can run. Only explicit cancel
        # may release such an unissued order; never abandon an uncertain valid ID.
        if (cancel and intent.status == 'CREATING' and len(intent.order_id) > 32
                and not intent.qr_payload and not intent.vendor_qr_ref
                and not intent.transaction_ref):
            lock_intent(intent)
            if (intent.status == 'CREATING' and not intent.qr_payload
                    and not intent.vendor_qr_ref and not intent.transaction_ref
                    and not intent.sale_transaction_id):
                intent.status = 'CANCELLED'
                db.session.commit()
                return jsonify(success=True, payment=public(intent))
            db.session.rollback()
            return jsonify(success=False, message='Payment changed. Refresh before cancelling.'), 409
        now = datetime.utcnow()
        if intent.last_checked_at and now - intent.last_checked_at < timedelta(seconds=5):
            return jsonify(success=False, message='Wait five seconds before checking again.'), 429
        # Atomic throttle also prevents simultaneous browser tabs spamming provider.
        changed = PaymentIntent.query.filter(PaymentIntent.id == intent.id,
            db.or_(PaymentIntent.last_checked_at.is_(None), PaymentIntent.last_checked_at <= now - timedelta(seconds=5))).update(
                {'last_checked_at': now}, synchronize_session=False)
        db.session.commit()
        if not changed:
            return jsonify(success=False, message='Payment check already running.'), 429
        try:
            gateway = client(for_recovery=True)
            result = gateway.cancel(intent.order_id) if cancel else gateway.get(intent.order_id)
            # Cancel docs omit currency: do NOT accept that response as a paid
            # assertion. Always retrieve canonical full transaction information.
            if cancel:
                result = gateway.get(intent.order_id)
            lock_intent(intent)
            apply_provider(intent, result)
            db.session.commit()
            return jsonify(success=True, payment=public(intent))
        except Exception:
            db.session.rollback()
            return jsonify(success=False, payment=public(intent), message='Provider status unavailable. Keep this order and retry later.'), 502

    @app.route('/api/payments/myanmyanpay/intents/<payment_id>/refresh', methods=['POST'])
    def mmpay_refresh(payment_id):
        return sync_provider(payment_id)

    @app.route('/api/payments/myanmyanpay/intents/<payment_id>/cancel', methods=['POST'])
    def mmpay_cancel(payment_id):
        return sync_provider(payment_id, cancel=True)

    @app.route('/api/payments/myanmyanpay/intents/<payment_id>/complete', methods=['POST'])
    def mmpay_complete(payment_id):
        denied = access(True)
        if denied:
            return denied
        intent = owned(payment_id)
        if not intent:
            return jsonify(success=False, message='Payment not found'), 404
        if intent.sale_transaction_id:
            sale = pos['Sale'].query.filter_by(transaction_id=intent.sale_transaction_id,
                                               branch_id=intent.branch_id).first()
            if sale:
                return pos['_sale_replay_response'](sale)
        if intent.status != 'SUCCESS':
            return jsonify(success=False, message='Payment has not been verified as successful.'), 409
        if intent.currency != pos['get_currency_code']():
            return jsonify(success=False, message='Branch currency changed. Payment needs reconciliation; do not pay again.'), 409
        result = pos['_create_sale_transaction'](json.loads(intent.checkout_json), verified_payment_id=intent.id)
        # Validation may fail before commit (stock/price/promotion changed).
        # The captured payment remains durable and visible for reconciliation.
        db.session.rollback()
        return result

    @app.route('/api/payments/myanmyanpay/intents/<payment_id>/qr')
    def mmpay_qr(payment_id):
        denied = access()
        if denied:
            return denied
        intent = owned(payment_id)
        if not intent or not intent.qr_payload or intent.status != 'PENDING':
            return jsonify(success=False, message='No active payment QR'), 404
        from reportlab.graphics.barcode.qr import QrCodeWidget
        from reportlab.graphics.shapes import Drawing, Rect
        from reportlab.graphics import renderSVG
        from reportlab.lib.colors import white
        widget = QrCodeWidget(intent.qr_payload)
        x1, y1, x2, y2 = widget.getBounds()
        # QR integrity: the provider payload is rendered by the provider library
        # and never altered, overlaid or re-encoded. A white quiet zone is added
        # around the symbol so the downloaded file also scans reliably.
        symbol = 320
        quiet = 32
        canvas = symbol + 2 * quiet
        drawing = Drawing(canvas, canvas, transform=[
            symbol / (x2 - x1), 0, 0, symbol / (y2 - y1), quiet, quiet])
        drawing.add(Rect(0, 0, canvas, canvas, fillColor=white,
                         strokeColor=white, strokeWidth=0))
        drawing.add(widget)
        response = Response(renderSVG.drawToString(drawing), mimetype='image/svg+xml')
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
        return response

    @app.route('/webhooks/myanmyanpay', methods=['POST'])
    def mmpay_webhook():
        # Public machine callback, deliberately outside cookie-scoped /api/.
        # Authenticate raw bytes BEFORE parsing JSON, and never trust session.
        if request.content_length and request.content_length > 65536:
            return jsonify(received=False), 413
        raw = request.get_data(cache=False)
        if len(raw) > 65536:
            return jsonify(received=False), 413
        try:
            gateway = client(for_webhook=True)
            if not gateway.verify_webhook(raw, request.headers.get('X-Mmpay-Nonce', ''),
                                          request.headers.get('X-Mmpay-Signature', '')):
                return jsonify(received=False), 401
        except Exception:
            return jsonify(received=False), 503
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError('Invalid payload')
            order_id = payload.get('orderId')
            intent = PaymentIntent.query.filter_by(order_id=order_id).first() if isinstance(order_id, str) else None
            if not intent:
                return jsonify(received=False), 404
            # Serialize callbacks for the SAME order using a DB write lock.
            # This prevents concurrent/out-of-order events overwriting SUCCESS.
            lock_intent(intent)
            digest = hashlib.sha256(raw).hexdigest()
            if PaymentWebhookEvent.query.filter_by(digest=digest).first():
                db.session.rollback()
                return jsonify(received=True, duplicate=True)
            apply_provider(intent, payload)
            db.session.add(PaymentWebhookEvent(digest=digest, order_id=intent.order_id, status=payload['status']))
            db.session.commit()
            return jsonify(received=True)
        except (ValueError, TypeError, ArithmeticError, IntegrityError):
            db.session.rollback()
            return jsonify(received=False), 409

    return PaymentIntent, PaymentWebhookEvent