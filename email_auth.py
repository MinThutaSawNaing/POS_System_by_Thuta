"""Optional email authentication: persistent, browser-bound, single-use challenges.

SQLite BEGIN IMMEDIATE serializes verification and send reservations across workers.
Only opaque IDs and CSRF/browser nonces enter the signed browser session; codes
are HMACs at rest. Provider responses and exceptions are deliberately never logged.
"""

import hashlib
import hmac
import os
import re
import secrets
import time
from contextlib import contextmanager
from email.utils import parseaddr, getaddresses
from types import SimpleNamespace

import requests
from flask import jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import text
from werkzeug.security import check_password_hash


TTL = 600
COOLDOWN = 60
MAX_ATTEMPTS = 5


def normalize_email(value):
    if not isinstance(value, str):
        raise ValueError('Enter a valid email address.')
    value = value.strip()
    if (len(value) > 254 or not re.fullmatch(
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", value)
            or '..' in value or len(value.split('@')[0]) > 64):
        raise ValueError('Enter a valid email address.')
    local, domain = value.rsplit('@', 1)
    return local + '@' + domain.lower()


def normalize_sender(value):
    if not isinstance(value, str) or len(value) > 254 or any(c in value for c in '\r\n\x00'):
        raise ValueError('Enter a valid sender email address.')
    value = value.strip()
    addresses = getaddresses([value])
    if len(addresses) != 1:
        raise ValueError('Enter a valid sender email address.')
    _, address = parseaddr(value)
    normalize_email(address)
    if value != address and not (value.endswith('>') and '<' in value):
        raise ValueError('Enter a valid sender email address.')
    return value


def masked_email(email):
    local, _, domain = email.partition('@')
    return local[:1] + '***@' + domain


class EmailAuthError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


def register_email_auth(app, db, pos):
    class EmailChallenge(db.Model):
        __tablename__ = 'email_challenge'
        id = db.Column(db.String(64), primary_key=True)
        user_id = db.Column(db.Integer, nullable=False, index=True)
        actor_id = db.Column(db.Integer, nullable=False)
        purpose = db.Column(db.String(12), nullable=False)
        email = db.Column(db.String(254), nullable=False)
        prior_email = db.Column(db.String(254))
        fingerprint = db.Column(db.String(64), nullable=False)
        user_branch_id = db.Column(db.Integer)
        branch_id = db.Column(db.Integer, nullable=False)
        browser_hash = db.Column(db.String(64), nullable=False)
        code_hash = db.Column(db.String(64), nullable=False)
        attempts = db.Column(db.Integer, nullable=False, default=0)
        expires_at = db.Column(db.Float, nullable=False)
        last_sent_at = db.Column(db.Float, nullable=False)
        consumed = db.Column(db.Boolean, nullable=False, default=False)
        delivered = db.Column(db.Boolean, nullable=False, default=False)
        remember = db.Column(db.Boolean, nullable=False, default=False)

    class EmailSendEvent(db.Model):
        __tablename__ = 'email_send_event'
        id = db.Column(db.Integer, primary_key=True)
        user_id = db.Column(db.Integer, nullable=False, index=True)
        ip_hash = db.Column(db.String(64), nullable=False, index=True)
        email_hash = db.Column(db.String(64), nullable=False, index=True)
        sent_at = db.Column(db.Float, nullable=False, index=True)

    def digest(*parts):
        key = str(app.secret_key).encode('utf-8')
        return hmac.new(key, ('email-auth\0' + '\0'.join(str(p) for p in parts)).encode(), hashlib.sha256).hexdigest()

    def csrf_token():
        if not session.get('email_csrf'):
            session['email_csrf'] = secrets.token_urlsafe(32)
        return session['email_csrf']

    def require_csrf(form=False):
        supplied = request.form.get('csrf_token') if form else request.headers.get('X-CSRF-Token')
        expected = session.get('email_csrf')
        if not isinstance(supplied, str) or not expected or not hmac.compare_digest(supplied, expected):
            raise EmailAuthError('Invalid security token. Reload and try again.', 403)

    def browser_hash():
        if not session.get('email_browser'):
            session['email_browser'] = secrets.token_urlsafe(32)
        return digest('browser', session['email_browser'])

    def config():
        key = pos['get_setting']('resend_api_key', '') or os.environ.get('RESEND_API_KEY', '')
        sender = pos['get_setting']('email_from', '') or os.environ.get('RESEND_FROM_EMAIL', '')
        try:
            sender = normalize_sender(sender)
        except ValueError:
            sender = ''
        return key.strip(), sender

    def configured():
        return secure_session_key() and all(config())

    def secure_session_key():
        return bool(app.secret_key and app.secret_key != 'your_super_secret_key_here')

    def send_code(email, code, purpose):
        key, sender = config()
        if not key or not sender:
            raise EmailAuthError('Email authentication is not configured. Contact a manager.', 503)
        try:
            response = requests.post(
                'https://api.resend.com/emails',
                headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
                json={'from': sender, 'to': [email], 'subject': 'Your POS verification code',
                      'text': f'Your POS {purpose} verification code is {code}. It expires in 10 minutes. If you did not request it, ignore this email.'},
                timeout=(3.05, 10), allow_redirects=False)
            success = 200 <= response.status_code < 300
        except Exception:
            # Even a transport exception may contain credentials/provider content.
            success = False
        if not success:
            raise EmailAuthError('Unable to send verification email. Please try again later.', 503)

    @contextmanager
    def locked():
        # Release the request's read transaction before acquiring a write lock.
        db.session.rollback()
        with db.engine.connect() as connection:
            connection.exec_driver_sql('BEGIN IMMEDIATE')
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        db.session.expire_all()

    def row(connection, sql, **params):
        result = connection.execute(text(sql), params).mappings().first()
        return dict(result) if result else None

    def user_row(connection, user_id):
        return row(connection, 'SELECT * FROM "user" WHERE id=:id', id=user_id)

    def fingerprint(user):
        return digest('user', user['password'], user['branch_id'], user['role'], user['email'], user['email_auth_version'])

    def user_fingerprint(user):
        return fingerprint(dict(password=user.password, branch_id=user.branch_id,
                                role=user.role, email=user.email, email_auth_version=user.email_auth_version))

    @app.before_request
    def check_email_auth_session():
        if request.endpoint == 'static' or request.path.startswith('/public/') or request.path in ('/healthz', '/display'):
            return None
        # Legacy sessions cannot bypass a newly enabled factor. Email-less
        # legacy accounts remain compatible until their next real login.
        expected = session.get('email_auth_fingerprint')
        if session.get('user_id'):
            user = db.session.get(pos['User'], session['user_id'])
            if (not user or ((user.email or user.email_auth_version > 0) and not expected) or
                    (expected and not hmac.compare_digest(user_fingerprint(user), expected))):
                session.clear()
                if request.path.startswith('/api/'):
                    return jsonify(success=False, message='Account security changed. Sign in again.'), 401
                return redirect(url_for('login'))

    def active_branch(connection, branch_id):
        return bool(row(connection, 'SELECT id FROM branch WHERE id=:id AND is_active=1', id=branch_id))

    def audit_change(connection, user_id, enabled):
        # Insert through the same connection: audit and link/unlink are one
        # transaction. Never persist the address, code, password, or hashes.
        connection.execute(pos['AuditLog'].__table__.insert().values(
            actor_user_id=session.get('user_id'), actor_username=session.get('username') or 'User',
            actor_role=session.get('role'), branch_id=session.get('branch_id'), category='Users',
            action='update', entity_type='UserEmail', entity_id=str(user_id),
            summary='Email two-factor authentication ' + ('enabled' if enabled else 'disabled'),
            changes_blob=pos['encode_audit_changes']({'two_factor_enabled': enabled}),
            request_method=request.method, request_path=request.path[:300]))

    def authorize(connection, user_id):
        actor = user_row(connection, session.get('user_id'))
        target = user_row(connection, user_id)
        branch_id = session.get('branch_id')
        if not actor:
            raise EmailAuthError('Unauthorized', 401)
        if actor['role'] not in ('cashier', 'manager', 'boss') or not active_branch(connection, branch_id):
            raise EmailAuthError('Access denied.', 403)
        if not target or (target['id'] != actor['id'] and (
                actor['role'] not in ('manager', 'boss') or target['branch_id'] != branch_id)):
            raise EmailAuthError('User not found.', 404)
        if actor['role'] == 'cashier' and actor['branch_id'] != branch_id:
            raise EmailAuthError('Access denied.', 403)
        return target

    def reserve_send(connection, user_id, email):
        now = time.time()
        ip = digest('ip', request.remote_addr or 'unknown')  # Never trust X-Forwarded-For.
        recipient = digest('recipient', email.lower())
        recent = row(connection, '''SELECT COUNT(*) AS total, MAX(sent_at) AS latest
            FROM email_send_event WHERE user_id=:uid AND sent_at>:since''', uid=user_id, since=now - 3600)
        if recent['latest'] and now - recent['latest'] < COOLDOWN:
            raise EmailAuthError('Please wait 60 seconds before requesting another email.', 429)
        for field, value, limit in (('user_id', user_id, 5), ('ip_hash', ip, 20), ('email_hash', recipient, 5)):
            count = row(connection, f'SELECT COUNT(*) AS n FROM email_send_event WHERE {field}=:value AND sent_at>:since', value=value, since=now - 3600)['n']
            if count >= limit:
                raise EmailAuthError('Too many email requests. Please try again later.', 429)
        connection.execute(text('INSERT INTO email_send_event (user_id,ip_hash,email_hash,sent_at) VALUES (:uid,:ip,:email,:now)'),
                           dict(uid=user_id, ip=ip, email=recipient, now=now))
        connection.execute(text('DELETE FROM email_send_event WHERE sent_at<:cutoff'), dict(cutoff=now - 86400))
        return now

    def metadata(challenge):
        return dict(success=True, challenge_id=challenge['id'], masked_email=masked_email(challenge['email']),
                    resend_after=max(0, int(COOLDOWN - (time.time() - challenge['last_sent_at']) + 0.999)),
                    expires_in=max(0, int(challenge['expires_at'] - time.time() + 0.999)),
                    message='Verification code sent. Check your email.')

    def deliver(challenge, code):
        try:
            send_code(challenge['email'], code, challenge['purpose'])
        except EmailAuthError:
            with locked() as connection:
                connection.execute(text('UPDATE email_challenge SET consumed=1 WHERE id=:id AND code_hash=:hash'),
                                   dict(id=challenge['id'], hash=challenge['code_hash']))
            raise
        with locked() as connection:
            connection.execute(text('UPDATE email_challenge SET delivered=1 WHERE id=:id AND code_hash=:hash AND consumed=0'),
                               dict(id=challenge['id'], hash=challenge['code_hash']))
        return metadata(challenge)

    def create_challenge(user_id, purpose, email=None, password=None, remember=False):
        binding = browser_hash()
        # Expensive password hashing must never hold SQLite's global writer lock.
        with db.engine.connect() as connection:
            snapshot = user_row(connection, user_id) if purpose == 'login' else authorize(connection, user_id)
        if not snapshot:
            raise EmailAuthError('Invalid credentials.', 401)
        snapshot_fingerprint = fingerprint(snapshot)
        if not isinstance(password, str) or not check_password_hash(snapshot['password'], password):
            raise EmailAuthError('The target account password is incorrect.', 403)
        with locked() as connection:
            user = user_row(connection, user_id) if purpose == 'login' else authorize(connection, user_id)
            if not user or not hmac.compare_digest(fingerprint(user), snapshot_fingerprint):
                raise EmailAuthError('Account changed. Start again.', 403)
            if purpose == 'login':
                branch_id = user['branch_id'] or pos['get_default_branch_id']()
                email = user['email']
            else:
                branch_id = session.get('branch_id')
                if purpose == 'link' and user['email']:
                    raise EmailAuthError('Unlink the current email before linking another address.')
                if purpose == 'unlink':
                    email = user['email']
            if not email:
                raise EmailAuthError('No email is linked to this account.')
            if not active_branch(connection, branch_id):
                raise EmailAuthError('Your assigned branch is inactive. Contact a manager.', 403)
            if not configured():
                raise EmailAuthError('Email authentication is not configured. Contact a manager.', 503)
            now = reserve_send(connection, user_id, email)
            code = f'{secrets.randbelow(1000000):06d}'
            challenge = dict(id=secrets.token_urlsafe(32), user_id=user_id,
                             actor_id=user_id if purpose == 'login' else session['user_id'],
                             purpose=purpose, email=email, prior_email=user['email'], fingerprint=fingerprint(user),
                             user_branch_id=user['branch_id'], branch_id=branch_id, browser_hash=binding,
                             attempts=0, expires_at=now + TTL, last_sent_at=now, consumed=False, delivered=False, remember=remember)
            challenge['code_hash'] = digest('code', challenge['id'], code)
            connection.execute(text('UPDATE email_challenge SET consumed=1 WHERE user_id=:uid AND browser_hash=:binding'), dict(uid=user_id, binding=binding))
            # Expired rows carry no rate-limit state (that is stored separately).
            connection.execute(text('DELETE FROM email_challenge WHERE expires_at<:cutoff'), dict(cutoff=now - 86400))
            columns = ','.join(challenge)
            connection.execute(text(f'INSERT INTO email_challenge ({columns}) VALUES ({",".join(":" + key for key in challenge)})'), challenge)
        return deliver(challenge, code)

    def checked_challenge(connection, challenge_id, purpose, user_id=None):
        challenge = row(connection, 'SELECT * FROM email_challenge WHERE id=:id', id=challenge_id) if isinstance(challenge_id, str) else None
        if (not challenge or challenge['purpose'] not in purpose or challenge['consumed'] or not challenge['delivered']
                or challenge['expires_at'] <= time.time() or challenge['attempts'] >= MAX_ATTEMPTS
                or not hmac.compare_digest(challenge['browser_hash'], browser_hash())
                or (user_id is not None and challenge['user_id'] != user_id)):
            raise EmailAuthError('Invalid or expired verification challenge. Start again.')
        if challenge['purpose'] == 'login':
            user = user_row(connection, challenge['user_id'])
        else:
            user = authorize(connection, challenge['user_id'])
            if challenge['actor_id'] != session.get('user_id') or challenge['branch_id'] != session.get('branch_id'):
                raise EmailAuthError('Invalid verification challenge.', 403)
        if (not user or fingerprint(user) != challenge['fingerprint']
                or not active_branch(connection, challenge['branch_id'])):
            raise EmailAuthError('Account or branch changed. Start again.', 403)
        return challenge, user

    def verify(challenge_id, code, purposes, user_id=None):
        valid = False
        with locked() as connection:
            challenge, user = checked_challenge(connection, challenge_id, purposes, user_id)
            # Every submitted value, including malformed codes, consumes an attempt.
            valid = isinstance(code, str) and bool(re.fullmatch(r'[0-9]{6}', code)) and hmac.compare_digest(
                challenge['code_hash'], digest('code', challenge['id'], code))
            connection.execute(text('UPDATE email_challenge SET attempts=attempts+1, consumed=:consumed WHERE id=:id'),
                               dict(id=challenge['id'], consumed=bool(valid)))
            if valid:
                if challenge['purpose'] in ('link', 'unlink'):
                    new_email = challenge['email'] if challenge['purpose'] == 'link' else None
                    connection.execute(text('UPDATE "user" SET email=:email, email_auth_version=email_auth_version+1 WHERE id=:id'),
                                       dict(id=user['id'], email=new_email))
                    connection.execute(text('UPDATE email_challenge SET consumed=1 WHERE user_id=:uid'), dict(uid=user['id']))
                    audit_change(connection, user['id'], challenge['purpose'] == 'link')
                    # Derive from the atomically verified state, not a subsequent
                    # read that could accidentally bless a concurrent reset.
                    challenge['session_fingerprint'] = fingerprint(dict(user, email=new_email,
                                                                         email_auth_version=user['email_auth_version'] + 1))
        if not valid:
            raise EmailAuthError('Invalid verification code. Check the code and try again.')
        return challenge

    def resend(challenge_id, purposes, user_id=None):
        with locked() as connection:
            challenge, _ = checked_challenge(connection, challenge_id, purposes, user_id)
            now = reserve_send(connection, challenge['user_id'], challenge['email'])
            code = f'{secrets.randbelow(1000000):06d}'
            challenge.update(code_hash=digest('code', challenge['id'], code), last_sent_at=now, expires_at=now + TTL)
            connection.execute(text('UPDATE email_challenge SET code_hash=:code_hash,last_sent_at=:last_sent_at,expires_at=:expires_at,delivered=0 WHERE id=:id'),
                               {key: challenge[key] for key in ('id', 'code_hash', 'last_sent_at', 'expires_at')})
        return deliver(challenge, code)

    @app.errorhandler(EmailAuthError)
    def email_error(error):
        return jsonify(success=False, message=error.message), error.status

    def payload():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise EmailAuthError('A JSON object is required.')
        return data

    @app.route('/api/auth/email', methods=['GET'])
    def api_auth_email():
        with db.engine.connect() as connection:
            user = authorize(connection, session.get('user_id'))
        return jsonify(id=user['id'], username=user['username'], email=user['email'],
                       two_factor_enabled=bool(user['email']), configured=configured(), csrf_token=csrf_token())

    @app.route('/api/settings/email', methods=['GET', 'PUT'])
    @pos['manager_or_boss_required']
    def api_settings_email():
        message = 'Email settings loaded.'
        if request.method == 'PUT':
            require_csrf()
            if not secure_session_key():
                raise EmailAuthError('Set a private application SECRET_KEY before configuring email authentication.', 503)
            data = payload()
            key = data.get('api_key', '')
            if not isinstance(key, str) or len(key) > 512 or any(c in key for c in '\r\n\x00'):
                raise EmailAuthError('Invalid API key.')
            try:
                sender = normalize_sender(data.get('from_email', ''))
            except ValueError as error:
                raise EmailAuthError(str(error)) from None
            with db.engine.connect() as connection:
                actor = authorize(connection, session.get('user_id'))
            if actor['role'] not in ('manager', 'boss'):
                raise EmailAuthError('Manager access required.', 403)
            actor_fingerprint = fingerprint(actor)
            password = data.get('password')
            if not isinstance(password, str) or not check_password_hash(actor['password'], password):
                raise EmailAuthError('Your current account password is incorrect.', 403)
            with locked() as connection:
                actor = authorize(connection, session.get('user_id'))
                if actor['role'] not in ('manager', 'boss') or not hmac.compare_digest(fingerprint(actor), actor_fingerprint):
                    raise EmailAuthError('Account changed. Sign in again.', 403)
                if key.strip():
                    pos['set_setting']('resend_api_key', key.strip(), connection=connection)
                pos['set_setting']('email_from', sender, connection=connection)
                connection.execute(pos['AuditLog'].__table__.insert().values(
                    actor_user_id=actor['id'], actor_username=actor['username'], actor_role=actor['role'],
                    branch_id=session.get('branch_id'), category='Settings', action='update',
                    entity_type='EmailSettings', entity_id='email', summary='Updated email authentication settings',
                    changes_blob=pos['encode_audit_changes']({'configuration': 'updated'}),
                    request_method=request.method, request_path=request.path[:300]))
            message = 'Email settings saved.'
        key, sender = config()
        return jsonify(success=True, configured=configured(), api_key_configured=bool(key),
                       from_email=sender, message=message, csrf_token=csrf_token())

    @app.route('/api/users/<int:user_id>/email/start', methods=['POST'])
    def api_user_email_start(user_id):
        require_csrf()
        data = payload()
        action = data.get('action')
        if action not in ('link', 'unlink'):
            raise EmailAuthError('Choose link or unlink.')
        email = None
        if action == 'link':
            try:
                email = normalize_email(data.get('email'))
            except ValueError as error:
                raise EmailAuthError(str(error)) from None
        return jsonify(create_challenge(user_id, action, email, data.get('password')))

    @app.route('/api/users/<int:user_id>/email/verify', methods=['POST'])
    def api_user_email_verify(user_id):
        require_csrf()
        data = payload()
        challenge = verify(data.get('challenge_id'), data.get('code'), ('link', 'unlink'), user_id)
        if user_id == session.get('user_id'):
            session['email_auth_fingerprint'] = challenge['session_fingerprint']
        return jsonify(success=True, message='Email authentication updated.')

    @app.route('/api/users/<int:user_id>/email/resend', methods=['POST'])
    def api_user_email_resend(user_id):
        require_csrf()
        return jsonify(resend(payload().get('challenge_id'), ('link', 'unlink'), user_id))

    def render_pending(error=None, info=None):
        try:
            with db.engine.connect() as connection:
                challenge, _ = checked_challenge(connection, session.get('email_login'), ('login',))
            return render_template('login.html', two_factor=True, csrf_token=csrf_token(),
                                   masked_email=masked_email(challenge['email']),
                                   resend_after=metadata(challenge)['resend_after'], error=error, info=info)
        except EmailAuthError as failure:
            session.pop('email_login', None)
            return render_template('login.html', error=error or failure.message)

    def begin_login(user, password, remember):
        session.clear()
        meta = create_challenge(user.id, 'login', password=password, remember=remember)
        session['email_login'] = meta['challenge_id']
        return render_pending(info=meta['message'])

    @app.route('/login/verify', methods=['POST'])
    def login_email_verify():
        try:
            require_csrf(form=True)
            challenge = verify(session.get('email_login'), request.form.get('code'), ('login',))
            return pos['finalize_login'](challenge['user_id'], bool(challenge['remember']),
                                         challenge['branch_id'], challenge['fingerprint'])
        except EmailAuthError as error:
            return render_pending(error=error.message), error.status

    @app.route('/login/resend', methods=['POST'])
    def login_email_resend():
        try:
            require_csrf(form=True)
            meta = resend(session.get('email_login'), ('login',))
            return render_pending(info=meta['message'])
        except EmailAuthError as error:
            return render_pending(error=error.message), error.status

    @app.route('/login/cancel')
    def login_email_cancel():
        cancel_pending()
        session.clear()
        return redirect(url_for('login'))

    def cancel_pending():
        challenge_id = session.get('email_login')
        if challenge_id:
            with locked() as connection:
                connection.execute(text('UPDATE email_challenge SET consumed=1 WHERE id=:id AND browser_hash=:binding'),
                                   dict(id=challenge_id, binding=browser_hash()))
        session.pop('email_login', None)

    def invalidate(user_id):
        # Participate in the caller's password-update transaction.
        db.session.execute(text('UPDATE email_challenge SET consumed=1 WHERE user_id=:uid'), dict(uid=user_id))

    return SimpleNamespace(Challenge=EmailChallenge, SendEvent=EmailSendEvent, begin_login=begin_login,
                           render_pending=render_pending, cancel_pending=cancel_pending,
                           invalidate=invalidate, configured=configured, user_fingerprint=user_fingerprint,
                           matches_fingerprint=lambda user, expected: hmac.compare_digest(
                               user_fingerprint(user), expected))