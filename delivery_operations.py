"""Delivery audit, attempts and independent COD ledger (no sale accounting writes)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from flask import jsonify, request, session, has_request_context
from sqlalchemy import event, inspect
from sqlalchemy.orm import object_session
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import deferred
from contextvars import ContextVar
from io import BytesIO
from urllib.parse import urlsplit
import ipaddress
import json
from flask import send_file
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.wsgi import get_input_stream


_actor = ContextVar('delivery_operation_actor', default=None)


class OperationConflict(ValueError):
    pass


def utc_naive(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime):
        raise ValueError('Invalid scheduled_at')
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def text(data, key, limit=400, required=False):
    value = data.get(key)
    if value is None:
        value = ''
    if not isinstance(value, str) or len(value.strip()) > limit:
        raise ValueError(f'{key} must be text of at most {limit} characters')
    value = value.strip()
    if required and not value:
        raise ValueError(f'{key} is required')
    return value


def money(value):
    if isinstance(value, bool) or value is None:
        raise ValueError('amount must be a positive monetary amount')
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result <= 0 or result > Decimal('999999999999.99') or result != result.quantize(Decimal('.01')):
            raise ValueError('amount must be positive with at most two decimals')
        return result.quantize(Decimal('.01'))
    except (InvalidOperation, TypeError):
        raise ValueError('Invalid amount')


class DeliveryOperations:
    def __init__(self, ns):
        self.ns = ns
        db = ns['db']

        class DeliveryEvent(db.Model):
            __tablename__ = 'delivery_event'
            id = db.Column(db.Integer, primary_key=True)
            delivery_id = db.Column(db.Integer, db.ForeignKey('delivery.id'), nullable=False, index=True)
            action = db.Column(db.String(40), nullable=False)
            note = db.Column(db.Text)
            actor_id = db.Column(db.Integer)
            actor_name = db.Column(db.String(120), nullable=False)
            created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
            outcome = db.Column(db.String(20))
            reason = db.Column(db.Text)
            proof_note = db.Column(db.Text)
            amount = db.Column(db.Numeric(16, 2))
            reference = db.Column(db.String(120))
            # NULL for ordinary history, unique for each ledger operation.
            ledger_key = db.Column(db.String(80), unique=True)
            delivery = db.relationship(ns['Delivery'], backref=db.backref('events', lazy='select', cascade='all, delete-orphan'))

        self.Event = DeliveryEvent

        class DeliveryProfile(db.Model):
            __tablename__ = 'delivery_profile'
            delivery_id = db.Column(db.Integer, db.ForeignKey('delivery.id'), primary_key=True)
            carrier_name = db.Column(db.String(120))
            tracking_url = db.Column(db.String(2000))
            carrier_status = db.Column(db.String(120))
            carrier_note = db.Column(db.Text)
            deleted_at = db.Column(db.DateTime)
            deleted_by = db.Column(db.Integer)
            delete_reason = db.Column(db.Text)
            delivery = db.relationship(ns['Delivery'], backref=db.backref('profile', uselist=False))

        class DeliveryProof(db.Model):
            __tablename__ = 'delivery_proof'
            id = db.Column(db.Integer, primary_key=True)
            delivery_id = db.Column(db.Integer, db.ForeignKey('delivery.id'), nullable=False, index=True)
            kind = db.Column(db.String(12), nullable=False)
            blob = deferred(db.Column(db.LargeBinary, nullable=False))
            mime = db.Column(db.String(30), nullable=False)
            size = db.Column(db.Integer, nullable=False)
            signer_name = db.Column(db.String(120))
            created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
            actor_id = db.Column(db.Integer, nullable=False)
            delivery = db.relationship(ns['Delivery'], backref=db.backref('proofs', lazy='select'))

        self.Profile, self.Proof = DeliveryProfile, DeliveryProof
        ns.update(DeliveryProfile=DeliveryProfile, DeliveryProof=DeliveryProof)

        @event.listens_for(db.session, 'before_flush')
        def audit_deliveries(session_db, flush_context, instances):
            for obj in session_db.deleted:
                if isinstance(obj, DeliveryProof) or (isinstance(obj, DeliveryEvent) and has_request_context()):
                    raise ValueError('Delivery history is immutable')
                if isinstance(obj, DeliveryProfile) and obj.deleted_at:
                    raise ValueError('Delivery deletion history is immutable')
            for obj in session_db.dirty:
                if isinstance(obj, (DeliveryProof, DeliveryEvent)) and session_db.is_modified(obj, include_collections=False):
                    raise ValueError('Delivery history is immutable')
                if isinstance(obj, DeliveryProfile) and any(value is not None for value in inspect(obj).attrs.deleted_at.history.deleted):
                    raise ValueError('Delivery deletion history is immutable')
                if isinstance(obj, DeliveryProfile) and obj.deleted_at and not inspect(obj).attrs.deleted_at.history.has_changes() and session_db.is_modified(obj, include_collections=False):
                    raise ValueError('Delivery deletion history is immutable')
            for obj in list(session_db.new) + list(session_db.dirty):
                if not isinstance(obj, ns['Delivery']):
                    continue
                obj.scheduled_at = utc_naive(obj.scheduled_at)
                state = inspect(obj)
                changed = [a.key for a in state.mapper.column_attrs if a.key != 'updated_at' and state.attrs[a.key].history.has_changes()]
                if obj in session_db.new:
                    self.record(obj, 'created', note='Delivery created')
                elif changed:
                    self.record(obj, 'updated', note='Updated: ' + ', '.join(changed))
                # Query persisted ledger state rather than a possibly stale
                # relationship cached by another workflow before collection.
                collected = (session_db.query(DeliveryEvent.id).filter_by(
                    delivery_id=obj.id, action='collection').first() if obj.id else None)
                if collected and any(k in changed for k in ('payment_status', 'delivery_fee', 'sale_id')) and obj not in session_db.new:
                    raise ValueError('Collected COD financial fields are immutable')
                if collected and 'stage' in changed and obj.stage != 'delivered':
                    raise ValueError('Collected deliveries must remain delivered')
            if has_request_context():
                for obj in session_db.deleted:
                    if isinstance(obj, DeliveryEvent):
                        raise ValueError('Delivery history cannot be deleted')
                for obj in session_db.dirty:
                    if isinstance(obj, DeliveryEvent) and session_db.is_modified(obj, include_collections=False):
                        raise ValueError('Delivery history is immutable')

    def record(self, delivery, action, **values):
        db = self.ns['db']
        operation_session = object_session(delivery)
        if operation_session is None:
            operation_session = db.session
        actor_id = _actor.get() or (session.get('user_id') if has_request_context() else None)
        with operation_session.no_autoflush:
            actor = operation_session.get(self.ns['User'], actor_id) if actor_id else None
        event = self.Event(delivery=delivery, action=action, actor_id=actor_id,
                           actor_name=actor.username if actor else 'System', **values)
        operation_session.add(event)
        return event

    def history_item(self, event):
        return {key: (getattr(event, key).isoformat() if key == 'created_at' else
                       format(event.amount, '.2f') if key == 'amount' and event.amount is not None else getattr(event, key))
                for key in ('id', 'action', 'note', 'actor_id', 'actor_name', 'created_at', 'outcome', 'reason', 'proof_note', 'amount', 'reference')}

    def details(self, delivery):
        events = sorted(delivery.events, key=lambda e: e.id or 0)
        attempts = [e for e in events if e.action == 'attempt']
        last = attempts[-1] if attempts else None
        failed = bool(last and last.outcome == 'failed' and not any(e.action == 'retry' and e.id > last.id for e in events))
        active = delivery.stage not in ('delivered', 'cancelled')
        due = utc_naive(delivery.scheduled_at) if delivery.scheduled_at else utc_naive(delivery.created_at) + timedelta(hours=48) if delivery.created_at else None
        overdue = bool(active and due and due < datetime.utcnow())
        collection = next((e for e in events if e.action == 'collection'), None)
        remittance = next((e for e in events if e.action == 'remittance'), None)
        expected = collection.amount if collection else Decimal(str(self.ns['delivery_amounts'](delivery)['collect_total']))
        cod = {'status': 'not_required' if expected == 0 else 'remitted' if remittance else 'collected' if collection else 'pending',
               'expected_amount': format(expected, '.2f'), 'collected_amount': format(collection.amount if collection else 0, '.2f'),
               'remitted_amount': format(remittance.amount if remittance else 0, '.2f')}
        return {'overdue': overdue, 'is_overdue': overdue, 'attempt_count': len(attempts),
                'last_attempt': self.history_item(last) if last else None,
                'last_attempt_outcome': last.outcome if last else None, 'has_failed_attempt': failed and active, 'cod': cod,
                **self.extended_details(delivery)}

    def active_query(self, query, include_deleted=False):
        if include_deleted:
            return query
        return query.filter(~self.ns['Delivery'].profile.has(self.Profile.deleted_at.isnot(None)))

    def tables(self):
        return [self.Event.__table__, self.Profile.__table__, self.Proof.__table__]

    def extended_details(self, delivery):
        profile = delivery.profile
        return {'carrier': {'name': profile.carrier_name if profile else '',
                            'tracking_code': delivery.tracking_code or '',
                            'tracking_url': profile.tracking_url if profile else '',
                            'status': profile.carrier_status if profile else '',
                            'note': profile.carrier_note if profile else ''},
                'proofs': [{'id': p.id, 'kind': p.kind,
                            'url': f'/api/deliveries/{delivery.id}/proofs/{p.id}',
                            'signer_name': p.signer_name, 'created_at': p.created_at.isoformat()}
                           for p in sorted(delivery.proofs, key=lambda p: p.id)],
                'is_deleted': bool(profile and profile.deleted_at)}

    def authorize(self, delivery, actor_id=None, branch_id=None):
        """Caller supplies server-trusted identity, never model/user JSON context."""
        if has_request_context():
            actor_id, branch_id = session.get('user_id'), session.get('branch_id')
        actor = self.ns['db'].session.get(self.ns['User'], actor_id) if actor_id else None
        if not actor or actor.role not in ('manager', 'boss'):
            raise PermissionError('Manager or boss required')
        if branch_id is None or delivery.branch_id != branch_id:
            raise PermissionError('Delivery outside active branch')
        branch = self.ns['db'].session.get(self.ns['Branch'], branch_id)
        if not branch or not branch.is_active:
            raise PermissionError('Active branch required')
        if delivery.profile and delivery.profile.deleted_at:
            raise ValueError('Delivery is deleted')
        return actor.id

    def set_carrier(self, delivery, data, *, actor_id=None, branch_id=None):
        actor_id = self.authorize(delivery, actor_id, branch_id)
        if not isinstance(data, dict):
            raise ValueError('A JSON object is required')
        mapping = {'carrier_name': ('carrier_name', 120), 'tracking_url': ('tracking_url', 2000),
                   'status': ('carrier_status', 120), 'note': ('carrier_note', 2000)}
        values = {target: text(data, key, limit) for key, (target, limit) in mapping.items() if key in data}
        url = values.get('tracking_url')
        if url:
            try:
                parsed = urlsplit(url)
                host = (parsed.hostname or '').lower().rstrip('.')
                if parsed.scheme.lower() != 'https' or not host or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443):
                    raise ValueError()
                if '.' not in host or host.endswith(('.localhost', '.local', '.internal', '.test', '.invalid')) or any(c.isspace() or ord(c) < 32 for c in url) or '\\' in url:
                    raise ValueError()
                try:
                    address = ipaddress.ip_address(host)
                except ValueError:
                    address = None
                if address is not None and not address.is_global:
                    raise ValueError()
            except ValueError:
                raise ValueError('tracking_url must be a public HTTPS URL without credentials')
        tracking = text(data, 'tracking_code', 120) if 'tracking_code' in data else None
        previous = {key: self.extended_details(delivery)['carrier'][key]
                    for key in ('name', 'tracking_code', 'status')}
        if delivery.profile is None:
            delivery.profile = self.Profile()
        for key, value in values.items():
            setattr(delivery.profile, key, value)
        if tracking is not None:
            delivery.tracking_code = tracking
        token = _actor.set(actor_id)
        try:
            current = {key: self.extended_details(delivery)['carrier'][key]
                       for key in ('name', 'tracking_code', 'status')}
            self.record(delivery, 'carrier_updated', note=json.dumps(
                {'mode': 'manual', 'previous': previous, 'current': current}, ensure_ascii=False))
            self.ns['db'].session.flush()
        finally:
            _actor.reset(token)

    def delete_delivery(self, delivery, data, *, actor_id=None, branch_id=None):
        actor_id = self.authorize(delivery, actor_id, branch_id)
        if not isinstance(data, dict) or data.get('confirm') is not True:
            raise ValueError('confirm must be true')
        reason = text(data, 'reason', 1000, required=True)
        if delivery.stage not in ('delivered', 'cancelled'):
            raise ValueError('Only delivered or cancelled deliveries may be deleted')
        ledger = self.Event.query.filter_by(delivery_id=delivery.id).filter(self.Event.action.in_(['collection', 'remittance'])).all()
        collected = any(item.action == 'collection' for item in ledger)
        remitted = any(item.action == 'remittance' for item in ledger)
        cod = self.details(delivery)['cod']
        if collected and not remitted:
            raise ValueError('Unsettled COD prevents deletion')
        if cod['status'] == 'collected' or (cod['status'] == 'pending' and delivery.stage != 'cancelled'):
            raise ValueError('Unsettled COD prevents deletion')
        if delivery.profile is None:
            delivery.profile = self.Profile()
        delivery.profile.deleted_at = datetime.utcnow()
        delivery.profile.deleted_by = actor_id
        delivery.profile.delete_reason = reason
        token = _actor.set(actor_id)
        try:
            self.record(delivery, 'deleted', reason=reason)
            self.ns['db'].session.flush()
        finally:
            _actor.reset(token)

    def add_proof(self, delivery, data, upload, *, actor_id=None, branch_id=None):
        actor_id = self.authorize(delivery, actor_id, branch_id)
        kind = data.get('kind')
        if kind not in ('photo', 'signature'):
            raise ValueError('kind must be photo or signature')
        signer = text(data, 'signer_name', 120, required=kind == 'signature')
        if self.Proof.query.filter_by(delivery_id=delivery.id).count() >= 10:
            raise ValueError('At most 10 proofs per delivery')
        if upload is None:
            raise ValueError('file is required')
        raw = upload.read(5 * 1024 * 1024 + 1)
        if len(raw) > 5 * 1024 * 1024:
            raise ValueError('Proof must be at most 5MB')
        try:
            with Image.open(BytesIO(raw)) as image:
                fmt = image.format
                if fmt not in ('JPEG', 'PNG') or (kind == 'signature' and fmt != 'PNG'):
                    raise ValueError('Use JPEG/PNG photos or PNG signatures')
                if image.width * image.height > 16000000 or image.width < 1 or image.height < 1:
                    raise ValueError('Proof dimensions exceed 16MP')
                image.verify()
            with Image.open(BytesIO(raw)) as image:
                image.load()
                oriented = ImageOps.exif_transpose(image)
                rgba = oriented.convert('RGBA')
                clean = Image.new('RGB', oriented.size, 'white')
                clean.paste(rgba, mask=rgba.getchannel('A'))
                if kind == 'signature' and all(low == high for low, high in clean.getextrema()):
                    raise ValueError('Signature must not be blank')
                output = BytesIO()
                clean.save(output, format=fmt)
            blob = output.getvalue()
            if len(blob) > 5 * 1024 * 1024:
                raise ValueError('Sanitized proof exceeds 5MB')
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
            raise ValueError('Invalid proof image') from error
        proof = self.Proof(delivery=delivery, kind=kind, blob=blob, size=len(blob),
                           mime='image/png' if fmt == 'PNG' else 'image/jpeg', signer_name=signer, actor_id=actor_id)
        self.ns['db'].session.add(proof)
        token = _actor.set(actor_id)
        try:
            self.record(delivery, 'proof_added', note=f'{kind} proof captured')
        finally:
            _actor.reset(token)
        return proof

    def execute_operation(self, delivery, action, data, *, actor_id=None, branch_id=None):
        actor_id = self.authorize(delivery, actor_id, branch_id)
        if not isinstance(data, dict):
            raise ValueError('A JSON object is required')
        if action == 'carrier':
            return self.set_carrier(delivery, data, actor_id=actor_id, branch_id=branch_id)
        if action == 'delete':
            return self.delete_delivery(delivery, data, actor_id=actor_id, branch_id=branch_id)
        if action == 'stage':
            return self.update_stage(delivery, data.get('stage'), actor_id=actor_id, branch_id=branch_id)
        if action not in ('attempts', 'retry', 'collection', 'remittance'):
            raise ValueError('Unknown delivery operation')
        token = _actor.set(actor_id)
        try:
            details = self.details(delivery)
            if action == 'attempts':
                outcome = data.get('outcome')
                if outcome not in ('failed', 'delivered'):
                    raise ValueError('outcome must be failed or delivered')
                if delivery.stage != 'delivering':
                    raise ValueError('Attempts require delivering stage')
                reason = text(data, 'reason', required=outcome == 'failed')
                proof = text(data, 'proof_note', 300, required=outcome == 'delivered')
                self.record(delivery, 'attempt', outcome=outcome, reason=reason, proof_note=proof)
                if outcome == 'delivered':
                    delivery.stage = 'delivered'
                    delivery.delivered_at = datetime.utcnow()
                    delivery.proof_note = proof
            elif action == 'retry':
                if delivery.stage != 'delivering' or not details['has_failed_attempt']:
                    raise ValueError('Only the latest failed attempt can be retried')
                scheduled = data.get('scheduled_at')
                delivery.scheduled_at = utc_naive(scheduled) if scheduled not in (None, '') else None
                delivery.stage = 'packaged'
                delivery.out_for_delivery_at = None
                delivery.packaged_at = datetime.utcnow()
                self.record(delivery, 'retry', note=text(data, 'note'))
            else:
                amount = money(data.get('amount'))
                reference = text(data, 'reference', 120, required=True)
                existing = self.Event.query.filter_by(ledger_key=f'{delivery.id}:{action}').first()
                if existing:
                    if existing.amount != amount or existing.reference != reference:
                        raise OperationConflict('Ledger entry already recorded')
                else:
                    cod = details['cod']
                    if delivery.stage != 'delivered' or cod['status'] == 'not_required':
                        raise ValueError('Only delivered COD orders may be settled')
                    if amount != Decimal(cod['expected_amount']):
                        raise ValueError('amount must equal the exact expected COD amount')
                    if action == 'remittance' and cod['status'] != 'collected':
                        raise ValueError('COD must be collected before remittance')
                    self.record(delivery, action, amount=amount, reference=reference, ledger_key=f'{delivery.id}:{action}')
            delivery.updated_at = datetime.utcnow()
            # Flush while authenticated actor scope is active, so automatic
            # column-change events retain the same actor outside HTTP requests.
            self.ns['db'].session.flush()
        finally:
            _actor.reset(token)

    def update_stage(self, delivery, stage, *, actor_id=None, branch_id=None):
        actor_id = self.authorize(delivery, actor_id, branch_id)
        if not isinstance(stage, str):
            raise ValueError('Invalid stage')
        stage = self.ns['normalize_delivery_stage'](stage)
        if not stage:
            raise ValueError('Invalid stage')
        if stage == delivery.stage:
            return
        if not self.ns['can_transition_delivery_stage'](delivery.stage, stage):
            raise ValueError(f'Cannot move {delivery.stage} to {stage}')
        delivery.stage = stage
        self.ns['apply_delivery_stage_timestamp'](delivery, stage)
        delivery.updated_at = datetime.utcnow()
        token = _actor.set(actor_id)
        try:
            self.record(delivery, 'stage_updated', note=f'Stage: {stage}')
            self.ns['db'].session.flush()
        finally:
            _actor.reset(token)

    def register(self):
        ns, Event = self.ns, self.Event
        app, db, Delivery = ns['app'], ns['db'], ns['Delivery']

        @app.route('/api/deliveries/<int:delivery_id>/carrier', methods=['POST'])
        @app.route('/api/deliveries/<int:delivery_id>/proofs', methods=['POST'])
        @app.route('/api/deliveries/<int:delivery_id>', methods=['DELETE'])
        @ns['manager_or_boss_required']
        def api_delivery_extension(delivery_id):
            try:
                if request.path.endswith('/proofs'):
                    # Per-request max_content_length is read-only before Flask
                    # 3.1. Bound the parser's stream without changing global
                    # configuration (which would race with other requests).
                    request.stream = get_input_stream(
                        request.environ, max_content_length=6 * 1024 * 1024)
                if db.engine.dialect.name == 'sqlite':
                    db.session.execute(ns['text']('BEGIN IMMEDIATE'))
                delivery = ns['operational_delivery_query']().filter(Delivery.id == delivery_id).with_for_update().first()
                if not delivery:
                    db.session.rollback()
                    return jsonify(success=False, message='Delivery not found'), 404
                deleted = request.method == 'DELETE'
                if deleted:
                    self.delete_delivery(delivery, request.get_json(silent=True))
                elif request.path.endswith('/proofs'):
                    self.add_proof(delivery, request.form, request.files.get('file'))
                else:
                    self.set_carrier(delivery, request.get_json(silent=True))
                delivery.updated_at = datetime.utcnow()
                db.session.commit()
                return jsonify(success=True, deleted=deleted, delivery=ns['serialize_delivery'](delivery))
            except RequestEntityTooLarge:
                db.session.rollback()
                return jsonify(success=False, message='Proof request must be at most 6MB'), 413
            except PermissionError as error:
                db.session.rollback()
                return jsonify(success=False, message=str(error)), 403
            except (ValueError, TypeError, OverflowError) as error:
                db.session.rollback()
                return jsonify(success=False, message=str(error)), 400

        @app.route('/api/deliveries/<int:delivery_id>/proofs/<int:proof_id>', methods=['GET'])
        def api_delivery_proof(delivery_id, proof_id):
            if 'user_id' not in session:
                return jsonify(error='Unauthorized'), 401
            delivery = ns['operational_delivery_query']().filter(Delivery.id == delivery_id).first()
            if not delivery:
                return jsonify(success=False, message='Delivery not found'), 404
            proof = self.Proof.query.filter_by(id=proof_id, delivery_id=delivery_id).first()
            if not proof:
                return jsonify(success=False, message='Proof not found'), 404
            response = send_file(BytesIO(proof.blob), mimetype=proof.mime,
                                 download_name=f'proof-{proof.id}.png' if proof.mime == 'image/png' else f'proof-{proof.id}.jpg',
                                 as_attachment=False, max_age=0)
            response.headers['Cache-Control'] = 'no-store'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            response.headers['Content-Security-Policy'] = "default-src 'none'; sandbox"
            return response

        @app.route('/api/deliveries/<int:delivery_id>/history')
        def api_delivery_history(delivery_id):
            if 'user_id' not in session:
                return jsonify(error='Unauthorized'), 401
            delivery = ns['operational_delivery_query']().filter(Delivery.id == delivery_id).first()
            if not delivery:
                return jsonify(success=False, message='Delivery not found'), 404
            return jsonify(items=[self.history_item(e) for e in sorted(delivery.events, key=lambda e: e.id, reverse=True)])

        @app.route('/api/deliveries/<int:delivery_id>/attempts', methods=['POST'])
        @app.route('/api/deliveries/<int:delivery_id>/retry', methods=['POST'])
        @app.route('/api/deliveries/<int:delivery_id>/collection', methods=['POST'])
        @app.route('/api/deliveries/<int:delivery_id>/remittance', methods=['POST'])
        @ns['manager_or_boss_required']
        def api_delivery_operation(delivery_id):
            # SQLite serialization precedes all ledger reads; unique ledger keys
            # remain the final protection for other DB engines/concurrent writers.
            try:
                if db.engine.dialect.name == 'sqlite':
                    db.session.execute(ns['text']('BEGIN IMMEDIATE'))
                delivery = ns['operational_delivery_query']().filter(Delivery.id == delivery_id).with_for_update().first()
                if not delivery:
                    db.session.rollback()
                    return jsonify(success=False, message='Delivery not found'), 404
                data = request.get_json(silent=True)
                if not isinstance(data, dict):
                    raise ValueError('A JSON object is required')
                action = request.path.rsplit('/', 1)[-1]
                self.execute_operation(delivery, action, data)
                delivery.updated_at = datetime.utcnow()
                db.session.commit()
                return jsonify(success=True, delivery=ns['serialize_delivery'](delivery))
            except OperationConflict as error:
                db.session.rollback()
                return jsonify(success=False, message=str(error)), 409
            except PermissionError as error:
                db.session.rollback()
                return jsonify(success=False, message=str(error)), 403
            except (ValueError, TypeError, OverflowError) as error:
                db.session.rollback()
                return jsonify(success=False, message=str(error)), 400
            except IntegrityError:
                db.session.rollback()
                return jsonify(success=False, message='Operation already recorded'), 409

        @app.route('/api/deliveries/bulk-assign', methods=['POST'])
        @ns['manager_or_boss_required']
        def api_delivery_bulk_assign():
            try:
                data = request.get_json(silent=True)
                if not isinstance(data, dict):
                    raise ValueError('A JSON object is required')
                ids = data.get('delivery_ids')
                if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or any(type(i) is not int or i <= 0 for i in ids) or len(set(ids)) != len(ids):
                    raise ValueError('delivery_ids must contain 1 to 100 unique positive integers')
                name = text(data, 'courier_name', 120, required=True)
                phone = text(data, 'courier_phone', 30)
                if db.engine.dialect.name == 'sqlite':
                    db.session.execute(ns['text']('BEGIN IMMEDIATE'))
                deliveries = ns['operational_delivery_query']().filter(Delivery.id.in_(ids)).with_for_update().all()
                if len(deliveries) != len(ids):
                    db.session.rollback()
                    return jsonify(success=False, message='Delivery not found'), 404
                if any(d.stage in ('delivered', 'cancelled') for d in deliveries):
                    raise ValueError('Only active deliveries can be assigned')
                for delivery in deliveries:
                    delivery.courier_name, delivery.courier_phone = name, phone
                    self.record(delivery, 'assigned', note=f'Courier: {name}; phone: {phone}')
                db.session.commit()
                return jsonify(success=True, updated=len(deliveries), items=[ns['serialize_delivery'](d) for d in deliveries])
            except (ValueError, TypeError) as error:
                db.session.rollback()
                return jsonify(success=False, message=str(error)), 400