"""Email 2FA HTTP/security regressions against a disposable Flask application.

Run with ``python run_tests.py test_email_auth.py -q --tb=short``.  No workspace
``app`` import occurs: even startup migrations and uploads use temporary paths.
Only the provider HTTP boundary and clock are replaced; OTPs come from mail.
"""

import importlib.util
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask
import pytest
import requests


SETTINGS = '/api/settings/email'
SECURITY = '/api/auth/email'
PASSWORD = 'target-password-123!'
MANAGER_PASSWORD = 'manager-password-456!'
API_KEY = 're_test_email_secret_not_for_responses'
FROM_EMAIL = 'POS Security <security@example.com>'
EMAIL = 'cashier@example.com'


@pytest.fixture(scope='module')
def email_app(tmp_path_factory):
    root = tmp_path_factory.mktemp('email-auth')
    (root / 'instance').mkdir()
    shutil.copytree(Path(__file__).parent / 'templates', root / 'templates')
    original_init = Flask.__init__

    def isolated_init(application, *args, **kwargs):
        kwargs.update(instance_path=str(root / 'instance'), root_path=str(root))
        original_init(application, *args, **kwargs)

    name = '_isolated_email_auth_test_app'
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name('app.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        with patch.object(Flask, '__init__', isolated_init):
            spec.loader.exec_module(module)
        module.app.config.update(TESTING=True, SECRET_KEY='isolated-email-auth-only')
        # Hash the two fixed fixture passwords only once; application login and
        # challenge routes still execute their real password verification.
        module._email_test_passwords = {
            value: module.generate_password_hash(value, method='pbkdf2:sha256:1000')
            for value in (PASSWORD, 'manager-password-456!')
        }
        with module.app.app_context():
            assert Path(module.db.engine.url.database).resolve().is_relative_to(root)
            # Disposable databases need neither crash recovery nor disk fsyncs;
            # schema resets otherwise dominate this security regression suite.
            def disposable_sqlite(connection, _record):
                connection.execute('PRAGMA synchronous=OFF')
                connection.execute('PRAGMA journal_mode=MEMORY')

            module.event.listen(module.db.engine, 'connect', disposable_sqlite)
            module.db.engine.dispose()
        yield module
    finally:
        if hasattr(module, 'db'):
            with module.app.app_context():
                module.db.session.remove()
                module.db.engine.dispose()
        sys.modules.pop(name, None)


@pytest.fixture
def world(email_app, monkeypatch):
    m = email_app
    import email_auth

    now = [time.time()]
    monkeypatch.setattr(email_auth, 'time', SimpleNamespace(time=lambda: now[0]))
    monkeypatch.delenv('RESEND_API_KEY', raising=False)
    monkeypatch.delenv('RESEND_FROM_EMAIL', raising=False)
    # Also block accidental non-email provider calls: no test can send real mail.
    deliveries = []
    delivery = SimpleNamespace(fail=False)

    def send(url, **kwargs):
        deliveries.append({'url': url, **kwargs})
        if delivery.fail:
            raise requests.ConnectionError('provider unavailable')
        return SimpleNamespace(status_code=200, ok=True,
                               json=lambda: {'id': 'mail-test-id'},
                               raise_for_status=lambda: None)

    monkeypatch.setattr(requests, 'post', send)
    with m.app.app_context():
        m.db.session.remove()
        m.db.drop_all()
        m.db.create_all()
        a = m.Branch(name='Alpha', code='EMAIL-A', is_active=True, is_default=True)
        b = m.Branch(name='Beta', code='EMAIL-B', is_active=True)
        m.db.session.add_all([a, b])
        m.db.session.flush()
        users = {}
        for name, role, branch, password in (
            ('manager', 'manager', a, 'manager-password-456!'),
            ('cashier', 'cashier', a, PASSWORD),
            ('peer', 'cashier', a, PASSWORD),
            ('remote', 'cashier', b, PASSWORD),
        ):
            user = m.User(username=name, role=role, branch_id=branch.id,
                          password=m._email_test_passwords[password])
            m.db.session.add(user)
            m.db.session.flush()
            users[name] = user.id
        m.db.session.commit()
        branches = (a.id, b.id)

    def client(name='manager', branch=None):
        c = m.app.test_client()
        with c.session_transaction() as session:
            session.update(user_id=users[name], username=name,
                           role='manager' if name == 'manager' else 'cashier',
                           branch_id=branch or (branches[1] if name == 'remote' else branches[0]))
        return c

    w = SimpleNamespace(m=m, app=m.app, users=users, branches=branches,
                        client=client, deliveries=deliveries, delivery=delivery,
                        advance=lambda seconds: now.__setitem__(0, now[0] + seconds))
    yield w
    with m.app.app_context():
        m.db.session.remove()


def payload(response, status=200):
    assert response.status_code == status, response.get_data(as_text=True)
    result = response.get_json()
    assert isinstance(result, dict)
    return result


def csrf(client, endpoint=SECURITY):
    token = payload(client.get(endpoint))['csrf_token']
    assert isinstance(token, str) and token
    return {'X-CSRF-Token': token}


def configure(w, client=None):
    c = client or w.client()
    response = c.put(SETTINGS, json={'api_key': API_KEY, 'from_email': FROM_EMAIL,
                                   'password': MANAGER_PASSWORD},
                     headers=csrf(c, SETTINGS))
    payload(response)
    return c


def start(w, client, target='cashier', action='link', **changes):
    data = {'email': EMAIL, 'password': PASSWORD, 'action': action, **changes}
    return client.post(f'/api/users/{w.users[target]}/email/start', json=data,
                       headers=csrf(client))


def verify(w, client, challenge_id, code, target='cashier'):
    return client.post(f'/api/users/{w.users[target]}/email/verify',
                       json={'challenge_id': challenge_id, 'code': code},
                       headers=csrf(client))


def resend(w, client, challenge_id, target='cashier'):
    return client.post(f'/api/users/{w.users[target]}/email/resend',
                       json={'challenge_id': challenge_id}, headers=csrf(client))


def mail_code(w):
    assert w.deliveries, 'A challenge must actually be delivered before it is usable'
    message = w.deliveries[-1]['json']
    body = ' '.join(str(message.get(key, '')) for key in ('text', 'html'))
    codes = set(re.findall(r'(?<!\d)\d{6}(?!\d)', body))
    assert len(codes) == 1, f'Expected one six-digit OTP in provider body: {body}'
    return codes.pop()


def link(w, client=None, target='cashier'):
    configure(w)
    c = client or w.client(target)
    challenge = payload(start(w, c, target=target))
    code = mail_code(w)
    payload(verify(w, c, challenge['challenge_id'], code, target=target))
    w.advance(61)
    return c


def assert_anonymous(client):
    with client.session_transaction() as session:
        assert 'user_id' not in session
    assert client.get('/api/products').status_code == 401


@pytest.mark.parametrize('remember', [False, True])
def test_unlinked_accounts_keep_password_only_login(world, remember):
    c = world.app.test_client()
    data = {'username': 'cashier', 'password': PASSWORD}
    if remember:
        data['remember'] = 'on'
    response = c.post('/login', data=data)
    assert response.status_code == 302
    with c.session_transaction() as session:
        assert session['user_id'] == world.users['cashier']
        assert session['branch_id'] == world.branches[0]
        assert session.permanent is remember
    assert world.deliveries == []


def test_link_requires_delivery_and_verification_before_enabling(world):
    configure(world)
    c = world.client('cashier')
    before = payload(c.get(SECURITY))
    assert before['two_factor_enabled'] is False
    challenge = payload(start(world, c))
    assert challenge['challenge_id']
    assert challenge['resend_after'] == 60
    assert challenge['expires_in'] == 600
    assert challenge['masked_email'] != EMAIL
    assert '@' in challenge['masked_email']
    assert payload(c.get(SECURITY))['two_factor_enabled'] is False
    code = mail_code(world)
    assert code not in json.dumps(challenge)
    payload(verify(world, c, challenge['challenge_id'], code))
    assert payload(c.get(SECURITY))['two_factor_enabled'] is True
    assert 400 <= verify(world, c, challenge['challenge_id'], code).status_code < 500


@pytest.mark.parametrize('password', [None, '', 'manager-password-456!', 'wrong'])
@pytest.mark.parametrize('action', ['link', 'unlink'])
def test_manager_must_supply_target_password_for_both_actions(world, password, action):
    configure(world)
    if action == 'unlink':
        link(world)
    c = world.client()
    count = len(world.deliveries)
    response = start(world, c, password=password, action=action)
    assert 400 <= response.status_code < 500
    assert len(world.deliveries) == count


def test_delivery_failure_does_not_enable_or_issue_usable_challenge(world):
    configure(world)
    c = world.client('cashier')
    world.delivery.fail = True
    response = start(world, c)
    assert 400 <= response.status_code < 600
    assert not (response.get_json() or {}).get('challenge_id')
    assert payload(c.get(SECURITY))['two_factor_enabled'] is False


def test_unlink_stays_enabled_until_otp_is_verified(world):
    c = link(world)
    challenge = payload(start(world, c, action='unlink'))
    assert payload(c.get(SECURITY))['two_factor_enabled'] is True
    payload(verify(world, c, challenge['challenge_id'], mail_code(world)))
    assert payload(c.get(SECURITY))['two_factor_enabled'] is False
    fresh = world.app.test_client()
    assert fresh.post('/login', data={'username': 'cashier', 'password': PASSWORD}).status_code == 302
    with fresh.session_transaction() as session:
        assert session['user_id'] == world.users['cashier']


def test_settings_are_global_encrypted_and_key_is_never_echoed(world, caplog):
    c = configure(world)
    status = payload(c.get(SETTINGS))
    assert status['configured'] is True
    assert status['api_key_configured'] is True
    assert API_KEY not in json.dumps(status)
    other = world.client(branch=world.branches[1])
    remote_status = payload(other.get(SETTINGS))
    assert remote_status['configured'] is True
    assert remote_status['from_email'] == FROM_EMAIL
    with world.m.app.app_context():
        for key, plaintext in [('resend_api_key', API_KEY), ('email_from', FROM_EMAIL)]:
            row = world.m.AppSetting.query.filter_by(key=key).one()
            assert row.value.startswith('gAAAA')
            assert plaintext not in row.value
            assert world.m.get_setting(key) == plaintext
    response = c.put(SETTINGS, json={'api_key': '', 'from_email': FROM_EMAIL,
                                   'password': MANAGER_PASSWORD},
                     headers=csrf(c, SETTINGS))
    assert payload(response)['api_key_configured'] is True
    cashier = world.client('cashier')
    challenge = payload(start(world, cashier))
    code = mail_code(world)
    sent = world.deliveries[-1]
    assert sent['url'] == 'https://api.resend.com/emails'
    assert sent['headers']['Authorization'] == 'Bearer ' + API_KEY
    assert sent['json']['from'] == FROM_EMAIL
    assert sent['json']['to'] == [EMAIL]
    assert sent['timeout'] == (3.05, 10)
    assert sent['allow_redirects'] is False
    with world.m.app.app_context():
        stored = world.m.db.session.get(world.m.EmailChallenge, challenge['challenge_id'])
        assert stored.code_hash != code
        assert len(stored.code_hash) == 64
    with cashier.session_transaction() as session:
        serialized = json.dumps(dict(session))
        assert API_KEY not in serialized
        assert code not in serialized
    assert API_KEY not in caplog.text
    assert code not in caplog.text


def test_unconfigured_install_does_not_offer_or_enable_email_auth(world):
    c = world.client('cashier')
    status = payload(c.get(SECURITY))
    assert status['configured'] is False
    assert status['two_factor_enabled'] is False
    assert start(world, c).status_code == 503
    assert payload(c.get(SECURITY))['two_factor_enabled'] is False
    assert not world.deliveries


@pytest.mark.parametrize('email', ['', 'not-an-email', 'a@b', 'a\r\nBcc:evil@example.com',
                                  None, [], 'a' * 65 + '@example.com'])
def test_invalid_recipient_is_rejected_without_delivery(world, email):
    configure(world)
    assert start(world, world.client('cashier'), email=email).status_code == 400
    assert not world.deliveries


def test_settings_require_authenticated_manager_and_session_bound_csrf(world):
    anonymous = world.app.test_client()
    assert anonymous.get(SETTINGS).status_code in (401, 403)
    assert anonymous.get(SECURITY).status_code == 401
    cashier = world.client('cashier')
    assert cashier.get(SETTINGS).status_code == 403
    assert cashier.put(SETTINGS, json={'api_key': API_KEY, 'from_email': FROM_EMAIL},
                       headers=csrf(cashier)).status_code == 403
    manager = world.client()
    token = csrf(manager, SETTINGS)
    data = {'api_key': API_KEY, 'from_email': FROM_EMAIL, 'password': MANAGER_PASSWORD}
    assert manager.put(SETTINGS, json=data).status_code == 403
    assert manager.put(SETTINGS, json=data, headers={'X-CSRF-Token': 'wrong'}).status_code == 403
    assert world.client().put(SETTINGS, json=data, headers=token).status_code == 403
    payload(manager.put(SETTINGS, json=data, headers=token))


@pytest.mark.parametrize('operation', ['start', 'verify', 'resend'])
def test_every_account_mutation_requires_csrf(world, operation):
    configure(world)
    c = world.client('cashier')
    challenge = payload(start(world, c))
    data = {'email': EMAIL, 'password': PASSWORD, 'action': 'link',
            'challenge_id': challenge['challenge_id'], 'code': mail_code(world)}
    url = f'/api/users/{world.users["cashier"]}/email/{operation}'
    count = len(world.deliveries)
    assert c.post(url, json=data).status_code == 403
    assert c.post(url, json=data, headers={'X-CSRF-Token': 'bad'}).status_code == 403
    assert len(world.deliveries) == count
    assert payload(c.get(SECURITY))['two_factor_enabled'] is False


@pytest.mark.parametrize('actor,target,branch,allowed', [
    ('cashier', 'cashier', 0, True),
    ('cashier', 'peer', 0, False),
    ('cashier', 'remote', 0, False),
    ('manager', 'cashier', 0, True),
    ('manager', 'remote', 0, False),
    ('manager', 'remote', 1, True),
    ('manager', 'cashier', 1, False),
    ('cashier', 'cashier', 1, False),
])
def test_account_management_respects_role_and_current_branch(world, actor, target, branch, allowed):
    configure(world)
    c = world.client(actor, branch=world.branches[branch])
    # Obtain token before selecting a deliberately invalid cashier branch.
    if actor == 'cashier' and branch == 1:
        c = world.client(actor)
        token = csrf(c)
        with c.session_transaction() as session:
            session['branch_id'] = world.branches[1]
    else:
        token = csrf(c)
    response = c.post(f'/api/users/{world.users[target]}/email/start',
                      json={'email': EMAIL, 'password': PASSWORD, 'action': 'link'}, headers=token)
    if allowed:
        challenge = payload(response)
        payload(verify(world, c, challenge['challenge_id'], mail_code(world), target=target))
    else:
        assert response.status_code in (403, 404)
        assert not world.deliveries


def test_challenge_cannot_be_used_by_another_browser_or_target(world):
    configure(world)
    c = world.client()
    challenge = payload(start(world, c))
    code = mail_code(world)
    stranger = world.client()
    assert verify(world, stranger, challenge['challenge_id'], code).status_code == 400
    assert verify(world, c, challenge['challenge_id'], code, target='peer').status_code == 400
    assert resend(world, stranger, challenge['challenge_id']).status_code == 400
    payload(verify(world, c, challenge['challenge_id'], code))


def test_wrong_and_malformed_codes_consume_persistent_attempt_budget(world):
    configure(world)
    c = world.client('cashier')
    challenge = payload(start(world, c))
    correct = mail_code(world)
    wrong = '000000' if correct != '000000' else '999999'
    for code in [wrong, None, '', '12345', 123456]:
        assert verify(world, c, challenge['challenge_id'], code).status_code == 400
        assert payload(c.get(SECURITY))['two_factor_enabled'] is False
    assert verify(world, c, challenge['challenge_id'], correct).status_code == 400
    world.advance(61)
    assert resend(world, c, challenge['challenge_id']).status_code == 400


def test_expired_code_and_resend_are_rejected(world):
    configure(world)
    c = world.client('cashier')
    challenge = payload(start(world, c))
    code = mail_code(world)
    world.advance(601)
    assert verify(world, c, challenge['challenge_id'], code).status_code == 400
    assert resend(world, c, challenge['challenge_id']).status_code == 400
    assert payload(c.get(SECURITY))['two_factor_enabled'] is False


def test_resend_cooldown_and_old_code_invalidation(world, monkeypatch):
    import email_auth

    # Deterministic distinct provider OTPs, not a test-only verification bypass.
    values = iter([123456, 654321])
    monkeypatch.setattr(email_auth.secrets, 'randbelow', lambda maximum: next(values))
    configure(world)
    c = world.client('cashier')
    challenge = payload(start(world, c))
    old_code = mail_code(world)
    assert resend(world, c, challenge['challenge_id']).status_code == 429
    world.advance(59)
    assert resend(world, c, challenge['challenge_id']).status_code == 429
    assert len(world.deliveries) == 1
    world.advance(2)
    replacement = payload(resend(world, c, challenge['challenge_id']))
    assert replacement['resend_after'] == 60
    assert replacement['expires_in'] == 600
    new_code = mail_code(world)
    assert new_code != old_code
    assert verify(world, c, challenge['challenge_id'], old_code).status_code == 400
    payload(verify(world, c, challenge['challenge_id'], new_code))


def test_failed_resend_invalidates_old_and_new_codes(world):
    configure(world)
    c = world.client('cashier')
    challenge = payload(start(world, c))
    old_code = mail_code(world)
    world.advance(61)
    world.delivery.fail = True
    assert resend(world, c, challenge['challenge_id']).status_code == 503
    new_code = mail_code(world)
    for code in (old_code, new_code):
        assert verify(world, c, challenge['challenge_id'], code).status_code == 400
    assert payload(c.get(SECURITY))['two_factor_enabled'] is False


class _LoginFormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.token = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('name') == 'csrf_token':
            self.token = attrs.get('value')


def login_pending(w, remember=False):
    c = w.app.test_client()
    response = c.post('/login', data={'username': 'cashier', 'password': PASSWORD,
                                     'remember': 'on' if remember else ''})
    assert response.status_code == 200
    parser = _LoginFormParser()
    parser.feed(response.get_data(as_text=True))
    assert parser.token, 'The second-factor page must provide the form CSRF token'
    assert_anonymous(c)
    return c, parser.token, mail_code(w)


@pytest.mark.parametrize('remember', [False, True])
def test_pending_login_is_not_authenticated_and_success_preserves_remember(world, remember):
    link(world)
    c, token, code = login_pending(world, remember)
    for endpoint in (SETTINGS, SECURITY, '/api/users', '/api/settings'):
        assert c.get(endpoint).status_code in (401, 403)
    assert c.get('/').status_code == 302
    response = c.post('/login/verify', data={'code': code, 'csrf_token': token})
    assert response.status_code == 302
    with c.session_transaction() as session:
        assert session['user_id'] == world.users['cashier']
        assert session['branch_id'] == world.branches[0]
        assert session['role'] == 'cashier'
        assert session.permanent is remember
    payload(c.get(SECURITY))


def test_login_delivery_failure_fails_closed(world):
    link(world)
    world.delivery.fail = True
    c = world.app.test_client()
    response = c.post('/login', data={'username': 'cashier', 'password': PASSWORD})
    assert response.status_code in (200, 503)
    assert_anonymous(c)
    assert c.post('/login/verify', data={'code': mail_code(world)}).status_code == 403
    assert_anonymous(c)


def test_login_verify_and_resend_require_form_csrf(world):
    link(world)
    c, token, code = login_pending(world)
    count = len(world.deliveries)
    for endpoint in ('/login/verify', '/login/resend'):
        for bad_token in (None, 'wrong'):
            data = {'code': code}
            if bad_token is not None:
                data['csrf_token'] = bad_token
            assert c.post(endpoint, data=data).status_code == 403
            assert_anonymous(c)
    assert len(world.deliveries) == count
    assert c.post('/login/verify', data={'code': code, 'csrf_token': token}).status_code == 302


def test_login_attempt_budget_survives_replaying_pending_cookie(world):
    link(world)
    c, token, code = login_pending(world)
    cookie_name = world.app.config['SESSION_COOKIE_NAME']
    snapshot = c.get_cookie(cookie_name).value
    wrong = '000000' if code != '000000' else '999999'
    for _ in range(5):
        c.set_cookie(cookie_name, snapshot)
        assert c.post('/login/verify', data={'code': wrong, 'csrf_token': token}).status_code == 400
    c.set_cookie(cookie_name, snapshot)
    assert c.post('/login/verify', data={'code': code, 'csrf_token': token}).status_code == 400
    assert_anonymous(c)


@pytest.mark.parametrize('finish', ['verify', 'cancel', 'logout'])
def test_pending_cookie_cannot_replay_completed_or_cancelled_login(world, finish):
    link(world)
    c, token, code = login_pending(world)
    cookie_name = world.app.config['SESSION_COOKIE_NAME']
    snapshot = c.get_cookie(cookie_name).value
    if finish == 'verify':
        assert c.post('/login/verify', data={'code': code, 'csrf_token': token}).status_code == 302
    else:
        assert c.get('/login/cancel' if finish == 'cancel' else '/logout').status_code == 302
    attacker = world.app.test_client()
    attacker.set_cookie(cookie_name, snapshot)
    assert attacker.post('/login/verify', data={'code': code, 'csrf_token': token}).status_code == 400
    assert_anonymous(attacker)


def test_login_resend_cooldown_replaces_code_and_expired_login_fails(world, monkeypatch):
    import email_auth

    link(world)
    values = iter([111111, 222222])
    monkeypatch.setattr(email_auth.secrets, 'randbelow', lambda maximum: next(values))
    c, token, old_code = login_pending(world)
    assert c.post('/login/resend', data={'csrf_token': token}).status_code == 429
    world.advance(61)
    assert c.post('/login/resend', data={'csrf_token': token}).status_code == 200
    new_code = mail_code(world)
    assert new_code != old_code
    assert c.post('/login/verify', data={'code': old_code, 'csrf_token': token}).status_code == 400
    world.advance(601)
    assert c.post('/login/verify', data={'code': new_code, 'csrf_token': token}).status_code == 400
    assert_anonymous(c)


@pytest.mark.parametrize('change', ['inactive_branch', 'branch', 'password', 'role', 'email'])
def test_pending_login_invalidated_by_account_or_branch_change(world, change):
    link(world)
    c, token, code = login_pending(world)
    # External account administration while a login is pending is fixture setup.
    with world.m.app.app_context():
        user = world.m.db.session.get(world.m.User, world.users['cashier'])
        if change == 'inactive_branch':
            world.m.db.session.get(world.m.Branch, world.branches[0]).is_active = False
        elif change == 'branch':
            user.branch_id = world.branches[1]
        elif change == 'password':
            user.password = world.m.generate_password_hash('changed-password')
        elif change == 'role':
            user.role = 'manager'
        else:
            user.email = 'replacement@example.com'
        world.m.db.session.commit()
    assert c.post('/login/verify', data={'code': code, 'csrf_token': token}).status_code == 403
    assert_anonymous(c)


def test_manager_branch_switch_invalidates_management_challenge(world):
    c = configure(world)
    challenge = payload(start(world, c))
    code = mail_code(world)
    with c.session_transaction() as session:
        session['branch_id'] = world.branches[1]
    assert verify(world, c, challenge['challenge_id'], code).status_code in (403, 404)
    assert resend(world, c, challenge['challenge_id']).status_code in (403, 404)
    assert payload(world.client('cashier').get(SECURITY))['two_factor_enabled'] is False


def test_one_wrong_code_does_not_prevent_later_correct_verification(world):
    configure(world)
    c = world.client('cashier')
    challenge = payload(start(world, c))
    code = mail_code(world)
    wrong = '000000' if code != '000000' else '999999'
    assert verify(world, c, challenge['challenge_id'], wrong).status_code == 400
    payload(verify(world, c, challenge['challenge_id'], code))
    assert payload(c.get(SECURITY))['two_factor_enabled'] is True


def test_hourly_send_limit_cannot_be_bypassed_by_restarting_challenges(world):
    configure(world)
    c = world.client('cashier')
    for _ in range(5):
        payload(start(world, c))
        world.advance(61)
    assert len(world.deliveries) == 5
    assert start(world, c).status_code == 429
    assert len(world.deliveries) == 5
    world.advance(3601)
    payload(start(world, c))
    assert len(world.deliveries) == 6


def test_delivery_failure_is_still_rate_limited(world):
    configure(world)
    c = world.client('cashier')
    world.delivery.fail = True
    assert start(world, c).status_code == 503
    assert start(world, c).status_code == 429
    assert len(world.deliveries) == 1


def test_failed_unlink_delivery_does_not_remove_existing_second_factor(world):
    c = link(world)
    world.delivery.fail = True
    assert start(world, c, action='unlink').status_code == 503
    assert payload(c.get(SECURITY))['two_factor_enabled'] is True


def test_wrong_login_password_never_sends_email(world):
    link(world)
    count = len(world.deliveries)
    c = world.app.test_client()
    assert c.post('/login', data={'username': 'cashier', 'password': 'wrong'}).status_code == 200
    assert_anonymous(c)
    assert len(world.deliveries) == count


def test_legacy_user_update_cannot_bypass_email_verification(world):
    c = configure(world)
    target = world.users['cashier']
    for data in ({'email': EMAIL}, {'two_factor_enabled': True},
                 {'email': EMAIL, 'two_factor_enabled': True}):
        assert c.put(f'/api/users/{target}', json=data).status_code == 400
    assert payload(world.client('cashier').get(SECURITY))['two_factor_enabled'] is False
    assert not world.deliveries


def test_starting_second_factor_login_clears_prior_authenticated_identity(world):
    link(world)
    c = world.client('manager')
    assert c.post('/login', data={'username': 'cashier', 'password': PASSWORD}).status_code == 200
    assert_anonymous(c)
    assert c.get(SETTINGS).status_code in (401, 403)


@pytest.mark.parametrize('status', [302, 400, 401, 429, 500])
def test_non_success_provider_responses_fail_closed(world, monkeypatch, status, caplog):
    configure(world)

    def rejected(*args, **kwargs):
        return SimpleNamespace(status_code=status, text=API_KEY)

    monkeypatch.setattr(requests, 'post', rejected)
    c = world.client('cashier')
    response = start(world, c)
    assert response.status_code == 503
    assert API_KEY not in response.get_data(as_text=True)
    assert API_KEY not in caplog.text
    assert payload(c.get(SECURITY))['two_factor_enabled'] is False


def test_provider_exception_does_not_disclose_credentials(world, monkeypatch, caplog):
    configure(world)

    def rejected(*args, **kwargs):
        raise requests.Timeout('Authorization: Bearer ' + API_KEY)

    monkeypatch.setattr(requests, 'post', rejected)
    response = start(world, world.client('cashier'))
    assert response.status_code == 503
    assert API_KEY not in response.get_data(as_text=True)
    assert API_KEY not in caplog.text


@pytest.mark.parametrize('sender', ['', 'invalid', 'one@example.com, two@example.com',
                                   'security@example.com\r\nBcc: other@example.com'])
def test_invalid_sender_cannot_overwrite_working_configuration(world, sender):
    c = configure(world)
    response = c.put(SETTINGS, json={'api_key': 'replacement-secret', 'from_email': sender,
                                   'password': MANAGER_PASSWORD},
                     headers=csrf(c, SETTINGS))
    assert response.status_code == 400
    assert payload(c.get(SETTINGS))['configured'] is True
    payload(start(world, world.client('cashier')))
    assert world.deliveries[-1]['headers']['Authorization'] == 'Bearer ' + API_KEY
    assert world.deliveries[-1]['json']['from'] == FROM_EMAIL


def test_existing_user_security_details_only_describe_that_account(world):
    c = link(world)
    linked = payload(c.get(SECURITY))
    assert linked['id'] == world.users['cashier']
    assert linked['username'] == 'cashier'
    assert linked['email'] == EMAIL
    assert linked['two_factor_enabled'] is True
    other = payload(world.client('peer').get(SECURITY))
    assert other['id'] == world.users['peer']
    assert other['email'] is None
    assert other['two_factor_enabled'] is False


def test_successful_login_resend_accepts_only_replacement_code(world, monkeypatch):
    import email_auth

    link(world)
    values = iter([333333, 444444])
    monkeypatch.setattr(email_auth.secrets, 'randbelow', lambda maximum: next(values))
    c, token, old_code = login_pending(world)
    world.advance(61)
    assert c.post('/login/resend', data={'csrf_token': token}).status_code == 200
    new_code = mail_code(world)
    assert new_code != old_code
    assert c.post('/login/verify', data={'code': old_code, 'csrf_token': token}).status_code == 400
    assert_anonymous(c)
    assert c.post('/login/verify', data={'code': new_code, 'csrf_token': token}).status_code == 302
    with c.session_transaction() as session:
        assert session['user_id'] == world.users['cashier']


def authenticated_login(w):
    client, token, code = login_pending(w)
    assert client.post('/login/verify', data={'code': code, 'csrf_token': token}).status_code == 302
    return client


def test_unlink_preserves_actor_session_but_revokes_other_completed_sessions(world):
    link(world)
    first = authenticated_login(world)
    world.advance(61)
    other = authenticated_login(world)
    world.advance(61)
    challenge = payload(start(world, first, action='unlink'))
    payload(verify(world, first, challenge['challenge_id'], mail_code(world)))
    assert payload(first.get(SECURITY))['two_factor_enabled'] is False
    assert other.get(SECURITY).status_code == 401
    assert_anonymous(other)


@pytest.mark.parametrize('change', ['password', 'role', 'branch', 'email'])
def test_completed_login_session_is_revoked_when_account_security_changes(world, change):
    link(world)
    c = authenticated_login(world)
    with world.m.app.app_context():
        user = world.m.db.session.get(world.m.User, world.users['cashier'])
        if change == 'password':
            user.password = world.m.generate_password_hash('changed-password', method='pbkdf2:sha256:1000')
        elif change == 'role':
            user.role = 'manager'
        elif change == 'branch':
            user.branch_id = world.branches[1]
        else:
            user.email = 'replacement@example.com'
        world.m.db.session.commit()
    assert c.get(SECURITY).status_code == 401
    assert_anonymous(c)


@pytest.mark.parametrize('password', [None, '', 'wrong', PASSWORD])
def test_email_settings_require_current_actor_password_without_partial_writes(world, password):
    c = configure(world)
    data = {'api_key': 'replacement-provider-secret', 'from_email': 'replacement@example.com'}
    if password is not None:
        data['password'] = password
    response = c.put(SETTINGS, json=data, headers=csrf(c, SETTINGS))
    assert response.status_code == 403
    assert 'replacement-provider-secret' not in response.get_data(as_text=True)
    payload(start(world, world.client('cashier')))
    sent = world.deliveries[-1]
    assert sent['headers']['Authorization'] == 'Bearer ' + API_KEY
    assert sent['json']['from'] == FROM_EMAIL
    with world.m.app.app_context():
        assert world.m.AuditLog.query.filter_by(entity_type='EmailSettings').count() == 1


def unlink(w, client):
    challenge = payload(start(w, client, action='unlink'))
    payload(verify(w, client, challenge['challenge_id'], mail_code(w)))
    assert payload(client.get(SECURITY))['two_factor_enabled'] is False
    w.advance(61)


def replay_cookie(w, snapshot):
    stale = w.app.test_client()
    stale.set_cookie(w.app.config['SESSION_COOKIE_NAME'], snapshot)
    assert stale.get(SECURITY).status_code == 401
    assert_anonymous(stale)


def test_signed_linked_cookie_does_not_revive_after_same_email_unlink_relink(world):
    actor = link(world)
    old = authenticated_login(world)
    snapshot = old.get_cookie(world.app.config['SESSION_COOKIE_NAME']).value
    world.advance(61)
    unlink(world, actor)
    link(world, actor)
    assert payload(actor.get(SECURITY))['email'] == EMAIL
    replay_cookie(world, snapshot)


def test_password_only_cookie_does_not_revive_after_link_unlink_cycle(world):
    old = world.app.test_client()
    assert old.post('/login', data={'username': 'cashier', 'password': PASSWORD}).status_code == 302
    snapshot = old.get_cookie(world.app.config['SESSION_COOKIE_NAME']).value
    actor = link(world)
    unlink(world, actor)
    replay_cookie(world, snapshot)


def test_raw_prefeature_cookie_does_not_revive_after_link_unlink_cycle(world):
    old = world.client('cashier')
    with old.session_transaction() as session:
        assert 'email_auth_fingerprint' not in session
    assert payload(old.get(SECURITY))['two_factor_enabled'] is False
    snapshot = old.get_cookie(world.app.config['SESSION_COOKIE_NAME']).value
    actor = link(world)
    unlink(world, actor)
    replay_cookie(world, snapshot)


def test_raw_legacy_session_cannot_bypass_newly_linked_factor(world):
    link(world)
    raw = world.client('cashier')
    assert raw.get(SECURITY).status_code == 401
    assert_anonymous(raw)


def test_settings_and_link_unlink_audits_contain_no_sensitive_values(world):
    actor = link(world)
    unlink(world, actor)
    with world.m.app.app_context():
        settings = world.m.AuditLog.query.filter_by(entity_type='EmailSettings').all()
        changes = world.m.AuditLog.query.filter_by(entity_type='UserEmail').order_by(world.m.AuditLog.id).all()
        assert settings
        assert len(changes) == 2
        details = [world.m.decode_audit_changes(row) for row in changes]
        assert details == [{'two_factor_enabled': True}, {'two_factor_enabled': False}]
        records = json.dumps([{'summary': row.summary, 'changes': world.m.decode_audit_changes(row)}
                              for row in [*settings, *changes]])
    secrets = [API_KEY, FROM_EMAIL, EMAIL, PASSWORD, MANAGER_PASSWORD]
    secrets.extend(re.findall(r'(?<!\d)\d{6}(?!\d)', mail['json']['text'])[0]
                   for mail in world.deliveries)
    for value in secrets:
        assert value not in records


def test_default_signing_key_prevents_email_settings_writes(world, monkeypatch):
    c = configure(world)
    csrf(c, SETTINGS)
    # Obtain a legitimate cookie under the unsafe key, rather than relying on
    # a cookie that changing SECRET_KEY would invalidate before route handling.
    monkeypatch.setattr(world.app, 'secret_key', 'your_super_secret_key_here')
    c = world.client()
    token = csrf(c, SETTINGS)
    response = c.put(SETTINGS, json={'api_key': API_KEY, 'from_email': FROM_EMAIL,
                                   'password': MANAGER_PASSWORD}, headers=token)
    assert response.status_code == 503
    assert payload(c.get(SETTINGS))['configured'] is False


def test_default_signing_key_prevents_enrollment_even_with_provider_env(world, monkeypatch):
    monkeypatch.setenv('RESEND_API_KEY', API_KEY)
    monkeypatch.setenv('RESEND_FROM_EMAIL', FROM_EMAIL)
    monkeypatch.setattr(world.app, 'secret_key', 'your_super_secret_key_here')
    c = world.client('cashier')
    assert payload(c.get(SECURITY))['configured'] is False
    assert start(world, c).status_code == 503
    assert not world.deliveries


def test_startup_migrates_legacy_user_schema_before_orm_queries(tmp_path):
    root = tmp_path / 'legacy-email-migration'
    (root / 'instance').mkdir(parents=True)
    shutil.copytree(Path(__file__).parent / 'templates', root / 'templates')
    from werkzeug.security import generate_password_hash

    hashed = generate_password_hash(PASSWORD, method='pbkdf2:sha256:1000')
    database = root / 'instance' / 'pos.db'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE user (id INTEGER PRIMARY KEY, username VARCHAR(80) UNIQUE NOT NULL, '
                           'password VARCHAR(120) NOT NULL, role VARCHAR(20), branch_id INTEGER)')
        connection.execute('INSERT INTO user VALUES (17, ?, ?, ?, NULL)', ('legacy', hashed, 'cashier'))

    original_init = Flask.__init__

    def isolated_init(application, *args, **kwargs):
        kwargs.update(instance_path=str(root / 'instance'), root_path=str(root))
        original_init(application, *args, **kwargs)

    # Real startup twice verifies both migration ordering and restart safety.
    for restart in range(2):
        name = f'_isolated_email_legacy_migration_{restart}'
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name('app.py'))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            with patch.object(Flask, '__init__', isolated_init):
                spec.loader.exec_module(module)
            module.app.config.update(TESTING=True, SECRET_KEY='legacy-migration-test-only')
            with module.app.app_context():
                user = module.db.session.get(module.User, 17)
                assert user.username == 'legacy'
                assert user.password == hashed
                assert user.email is None
                assert user.email_auth_version == 0
            client = module.app.test_client()
            assert client.post('/login', data={'username': 'legacy', 'password': PASSWORD}).status_code == 302
            assert payload(client.get(SECURITY))['two_factor_enabled'] is False
        finally:
            if hasattr(module, 'db'):
                with module.app.app_context():
                    module.db.session.remove()
                    module.db.engine.dispose()
            sys.modules.pop(name, None)


@pytest.mark.parametrize('provider_status', [200, 500])
def test_otp_cannot_verify_while_provider_delivery_is_still_pending(world, monkeypatch, provider_status):
    configure(world)
    c = world.client('cashier')
    challenge = payload(start(world, c))
    world.advance(61)
    attempted = []
    codes = []

    def provider_in_flight(_url, **kwargs):
        code = re.search(r'code is (\d{6})\.', kwargs['json']['text']).group(1)
        codes.append(code)
        # A second request arrives before the first provider request returns.
        # Reuse the real browser cookie and OTP, not a verification bypass.
        attempted.append(verify(world, c, challenge['challenge_id'], code).status_code)
        assert payload(c.get(SECURITY))['two_factor_enabled'] is False
        return SimpleNamespace(status_code=provider_status)

    monkeypatch.setattr(requests, 'post', provider_in_flight)
    response = resend(world, c, challenge['challenge_id'])
    assert attempted == [400]
    assert response.status_code == (200 if provider_status == 200 else 503)
    after_delivery = verify(world, c, challenge['challenge_id'], codes[0])
    if provider_status == 200:
        payload(after_delivery)
        assert payload(c.get(SECURITY))['two_factor_enabled'] is True
    else:
        assert after_delivery.status_code == 400
        assert payload(c.get(SECURITY))['two_factor_enabled'] is False