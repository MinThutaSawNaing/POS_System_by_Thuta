// Run with node --test /absolute/path/to/public/email-security.test.js.
// The real browser script runs in an isolated VM; no server, storage or packages.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, 'email-security.js'), 'utf8');
const dashboard = fs.readFileSync(path.join(__dirname, '../templates/dashboard.html'), 'utf8');
const login = fs.readFileSync(path.join(__dirname, '../templates/login.html'), 'utf8');

function tagFor(html, id) {
  const tag = html.match(new RegExp(`<\\w+\\b[^>]*\\bid="${id}"[^>]*>`));
  assert.ok(tag, `Template contains #${id}`);
  return tag[0];
}

function element(id) {
  const tag = tagFor(dashboard, id);
  const select = dashboard.match(new RegExp(`<select\\b[^>]*\\bid="${id}"[^>]*>([\\s\\S]*?)<\\/select>`));
  const options = select ? [...select[1].matchAll(/<option\b([^>]*)>/g)] : [];
  const selected = options.find(option => /\bselected\b/.test(option[1])) || options[0];
  const listeners = new Map();
  return {
    id, value: (selected ? selected[1] : tag).match(/\bvalue="([^"]*)"/)?.[1] || '',
    placeholder: tag.match(/\bplaceholder="([^"]*)"/)?.[1] || '',
    textContent: '', className: '', style: {}, dataset: {},
    hidden: /\shidden(?:\s|>)/.test(tag),
    disabled: /\sdisabled(?:\s|\/?>)/.test(tag),
    required: /\srequired(?:\s|\/?>)/.test(tag),
    readOnly: /\sreadonly(?:\s|\/?>)/.test(tag),
    valid: true, validityChecks: 0, focused: false,
    // Fail loudly if untrusted provider/user messages ever become HTML.
    set innerHTML(_) { throw new Error(`Unsafe HTML assignment to #${id}`); },
    focus() { this.focused = true; },
    reportValidity() { this.validityChecks++; return this.valid; },
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    async emit(type) {
      const event = {preventDefault() { this.defaultPrevented = true; }};
      await Promise.all((listeners.get(type) || []).map(fn => fn.call(this, event)));
      return event;
    },
  };
}

function response(body, status = 200, headers = {}) {
  return {ok: status >= 200 && status < 300, status,
    json: async () => body, headers: {get: name => headers[name] ?? null}};
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return {promise, resolve, reject};
}

function harness() {
  const ids = ['emailSecurityModal', 'email-security-title', 'email-security-status',
    'email-security-address-group', 'email-security-address', 'email-security-password',
    'email-security-code', 'email-security-start-form', 'email-security-verify-form',
    'email-security-send', 'email-security-confirm', 'email-security-resend',
    'email-settings-form', 'email-settings-status', 'email-from',
    'email-smtp-host', 'email-smtp-port', 'email-smtp-username', 'email-smtp-password',
    'email-smtp-security', 'email-settings-password',
    'save-email-settings-btn', 'users-section'];
  const nodes = Object.fromEntries(ids.map(id => [id, element(id)]));
  nodes.emailSecurityModal.dataset.currentUser = '7';
  nodes['users-section'].hidden = true;
  let now = 1_000_000, timerId = 0;
  const timers = new Map(), pageListeners = new Map();
  const h = {nodes, requests: [], replies: [], confirmations: [], invalidations: [],
    modalShows: 0, userLoads: 0, allowDiscard: false,
    own: {success: true, configured: true, csrf_token: 'session-csrf',
      username: 'Cashier', email: null, two_factor_enabled: false}};
  const window = {
    fetch: async (url, options) => {
      h.requests.push({url, options});
      assert.ok(h.replies.length, `Unexpected request: ${url}`);
      const next = h.replies.shift();
      return typeof next === 'function' ? next(url, options) : next;
    },
    confirm: text => { h.confirmations.push(text); return h.allowDiscard; },
    addEventListener: (type, fn) => pageListeners.set(type, fn),
    // Credentials and OTPs must never be persisted by this feature.
    localStorage: {setItem() { throw new Error('Unexpected localStorage write'); }},
    sessionStorage: {setItem() { throw new Error('Unexpected sessionStorage write'); }},
  };
  const context = vm.createContext({window,
    document: {getElementById(id) { assert.ok(nodes[id], `Unknown DOM id: ${id}`); return nodes[id]; }},
    bootstrap: {Modal: {getOrCreateInstance(node) {
      assert.equal(node, nodes.emailSecurityModal);
      return {show() { h.modalShows++; }};
    }}},
    Date: {now: () => now},
    setInterval(fn) { const id = ++timerId; timers.set(id, fn); return id; },
    clearInterval: id => timers.delete(id),
    markBranchFormSaved() {},
    invalidateCacheGroups: (...args) => h.invalidations.push(args),
    loadUsers: () => { h.userLoads++; },
  });
  vm.runInContext(source, context, {filename: 'email-security.js'});
  h.ui = window.EmailSecurity;
  h.node = id => nodes[id];
  h.reply = (body, status, headers) => h.replies.push(response(body, status, headers));
  h.emit = (id, type = 'submit') => nodes[id].emit(type);
  h.close = () => h.emit('emailSecurityModal', 'hidden.bs.modal');
  h.pagehide = () => pageListeners.get('pagehide')();
  h.advance = milliseconds => {
    now += milliseconds;
    for (const fn of [...timers.values()]) fn();
  };
  h.timerCount = () => timers.size;
  h.open = async (own = {}) => { h.reply({...h.own, ...own}); await h.ui.open(); };
  h.start = async (overrides = {}) => {
    nodes['email-security-address'].value = ' person@example.com ';
    nodes['email-security-password'].value = 'current password';
    h.reply({success: true, challenge_id: 'challenge-1', masked_email: 'p***@example.com',
      resend_after: 60, ...overrides});
    await h.emit('email-security-start-form');
  };
  return h;
}

function assertJsonRequest(request, url, payload, token = 'session-csrf') {
  assert.equal(request.url, url);
  assert.equal(request.options.method, url === '/api/settings/email' ? 'PUT' : 'POST');
  assert.equal(request.options.headers['Content-Type'], 'application/json');
  assert.equal(request.options.headers['X-CSRF-Token'], token);
  assert.equal(request.options.credentials, 'same-origin');
  assert.equal(request.options.cache, 'no-store');
  assert.deepEqual(JSON.parse(request.options.body), payload);
}

test('own unlinked account opens without loading global settings and requires an email', async () => {
  const h = harness();
  assert.equal(h.requests.length, 0, 'initialization is lazy');
  await h.open();
  assert.deepEqual(h.requests.map(r => r.url), ['/api/auth/email']);
  assert.equal(h.requests[0].options.credentials, 'same-origin');
  assert.equal(h.requests[0].options.cache, 'no-store');
  assert.equal(h.modalShows, 1);
  assert.equal(h.node('email-security-title').textContent, 'Add email — Cashier');
  assert.equal(h.node('email-security-address-group').hidden, false);
  assert.equal(h.node('email-security-address').required, true);
  assert.equal(h.node('email-security-address').focused, true);
  assert.equal(h.node('email-security-start-form').hidden, false);
  assert.equal(h.node('email-security-verify-form').hidden, true);
  assert.match(h.node('email-security-status').textContent, /password-only until you verify/);
  h.node('email-security-start-form').valid = false;
  const event = await h.emit('email-security-start-form');
  assert.equal(event.defaultPrevented, true);
  assert.equal(h.node('email-security-start-form').validityChecks, 1);
  assert.equal(h.requests.length, 1, 'invalid form never sends a request');
});

test('target linked account uses own CSRF token, unlink labels and no hidden email requirement', async () => {
  const h = harness();
  h.reply(h.own);
  h.reply({success: true, username: 'Manager', email: 'manager@example.com', two_factor_enabled: true});
  await h.ui.open(42);
  assert.deepEqual(h.requests.map(r => r.url), ['/api/auth/email', '/api/users/42']);
  assert.equal(h.node('email-security-title').textContent, 'Unlink email — Manager');
  assert.equal(h.node('email-security-address-group').hidden, true);
  assert.equal(h.node('email-security-address').required, false);
  assert.equal(h.node('email-security-password').focused, true);
  assert.match(h.node('email-security-status').textContent, /Two-step login is on for manager@example.com/);
  h.node('email-security-password').value = 'owner password';
  h.reply({success: true, challenge_id: 'unlink-1', masked_email: 'm***@example.com', resend_after: 0});
  await h.emit('email-security-start-form');
  assertJsonRequest(h.requests[2], '/api/users/42/email/start',
    {email: '', password: 'owner password', action: 'unlink'});
  assert.equal(h.node('email-security-confirm').textContent, 'Verify and unlink');
  assert.match(h.node('email-security-status').textContent, /stays on until you confirm/);
  assert.equal(h.invalidations.length, 0);
});

test('own linked and target unlinked accounts choose their own status instead of the actor status', async () => {
  const own = harness();
  await own.open({email: 'cashier@example.com', two_factor_enabled: true});
  assert.deepEqual(own.requests.map(r => r.url), ['/api/auth/email']);
  assert.equal(own.node('email-security-title').textContent, 'Unlink email — Cashier');
  assert.equal(own.node('email-security-address').required, false);

  const target = harness();
  target.reply({...target.own, email: 'actor@example.com', two_factor_enabled: true});
  target.reply({success: true, username: '<img src=x onerror=steal()>', email: null});
  await target.ui.open('target/id');
  assert.equal(target.requests[1].url, '/api/users/target%2Fid');
  assert.equal(target.node('email-security-title').textContent, 'Add email — <img src=x onerror=steal()>');
  assert.equal(target.node('email-security-address').required, true);
  assert.equal(target.node('email-security-address-group').hidden, false);
  await target.start();
  assertJsonRequest(target.requests[2], '/api/users/target%2Fid/email/start',
    {email: 'person@example.com', password: 'current password', action: 'link'});
});

test('link remains pending until valid explicit verification and clears the password after send', async () => {
  const h = harness();
  await h.open();
  await h.emit('email-security-verify-form');
  assert.equal(h.requests.length, 1, 'cannot verify before a challenge exists');
  await h.start();
  assertJsonRequest(h.requests[1], '/api/users/7/email/start',
    {email: 'person@example.com', password: 'current password', action: 'link'});
  assert.equal(h.node('email-security-password').value, '');
  assert.equal(h.node('email-security-start-form').hidden, true);
  assert.equal(h.node('email-security-verify-form').hidden, false);
  assert.equal(h.node('email-security-confirm').textContent, 'Verify and enable');
  assert.match(h.node('email-security-status').textContent, /Enter the code to enable/);
  assert.equal(h.invalidations.length, 0, 'sending a code does not activate the account');
  h.node('email-security-verify-form').valid = false;
  await h.emit('email-security-verify-form');
  assert.equal(h.requests.length, 2);
  h.node('email-security-verify-form').valid = true;
  h.node('email-security-code').value = ' 123456 ';
  h.node('users-section').hidden = false;
  h.reply({success: true});
  await h.emit('email-security-verify-form');
  assertJsonRequest(h.requests[2], '/api/users/7/email/verify', {challenge_id: 'challenge-1', code: '123456'});
  assert.match(h.node('email-security-status').textContent, /Two-step login is now on/);
  assert.equal(h.invalidations.length, 1);
  assert.equal(h.userLoads, 1);
  assert.equal(h.node('email-security-code').value, '');
  assert.equal(h.node('email-security-verify-form').hidden, true);
  assert.equal(h.timerCount(), 0);
});

test('unconfigured delivery keeps both sensitive forms unavailable', async () => {
  const h = harness();
  await h.open({configured: false});
  assert.equal(h.node('email-security-start-form').hidden, true);
  assert.equal(h.node('email-security-verify-form').hidden, true);
  assert.match(h.node('email-security-status').textContent, /not configured/);
  assert.match(h.node('email-security-status').className, /alert-danger/);
});

test('closing the dialog or leaving the page clears secrets and cancels the resend timer', async () => {
  for (const leave of [h => h.close(), h => h.pagehide()]) {
    const h = harness();
    await h.open();
    await h.start();
    h.node('email-security-password').value = 'another secret';
    h.node('email-security-code').value = '123456';
    h.node('email-smtp-password').value = 're_draft_secret';
    await leave(h);
    for (const id of ['email-security-address', 'email-security-password', 'email-security-code']) {
      assert.equal(h.node(id).value, '', `${id} is cleared`);
    }
    assert.equal(h.timerCount(), 0);
    assert.equal(h.node('email-security-start-form').hidden, true);
    assert.equal(h.node('email-security-verify-form').hidden, true);
    await h.emit('email-security-verify-form');
    await h.emit('email-security-resend', 'click');
    assert.equal(h.requests.length, 2, 'closed challenges cannot be reused');
  }
  const h = harness();
  h.node('email-smtp-password').value = 're_draft_secret';
  h.node('email-settings-password').value = 'manager secret';
  h.pagehide();
  assert.equal(h.node('email-smtp-password').value, '');
  assert.equal(h.node('email-settings-password').value, '');
});

test('late open responses and errors cannot show forms or overwrite a newer dialog', async () => {
  for (const reject of [false, true]) {
    const h = harness(), pendingReply = deferred();
    h.replies.push(() => pendingReply.promise);
    const opening = h.ui.open();
    await h.close();
    await h.open({username: 'Newer account'});
    const status = h.node('email-security-status').textContent;
    if (reject) pendingReply.reject(new Error('stale network failure'));
    else pendingReply.resolve(response({...h.own, username: 'Old account', email: 'old@example.com'}));
    await opening;
    assert.equal(h.node('email-security-title').textContent, 'Add email — Newer account');
    assert.equal(h.node('email-security-status').textContent, status);
  }
});

test('late send responses after close cannot revive a challenge or overwrite the next account', async () => {
  const h = harness(), pendingReply = deferred();
  await h.open();
  h.node('email-security-address').value = 'old@example.com';
  h.node('email-security-password').value = 'old password';
  h.replies.push(() => pendingReply.promise);
  const sending = h.emit('email-security-start-form');
  assert.equal(h.node('email-security-send').disabled, true);
  assert.equal(h.node('email-security-send').textContent, 'Sending code…');
  await h.emit('email-security-start-form');
  assert.equal(h.requests.length, 2, 'double submit sends only one code');
  await h.close();
  assert.equal(h.node('email-security-send').textContent, 'Send verification code');
  assert.equal(h.node('email-security-send').disabled, false);
  await h.open({username: 'New account'});
  const status = h.node('email-security-status').textContent;
  pendingReply.resolve(response({success: true, challenge_id: 'stale', resend_after: 60}));
  await sending;
  assert.equal(h.node('email-security-status').textContent, status);
  assert.equal(h.node('email-security-send').textContent, 'Send verification code');
  assert.equal(h.node('email-security-send').disabled, false);
  assert.equal(h.node('email-security-verify-form').hidden, true);
  assert.equal(h.node('email-security-start-form').hidden, false);
  assert.equal(h.node('email-security-password').value, '');
  assert.equal(h.timerCount(), 0);
  await h.emit('email-security-verify-form');
  assert.equal(h.requests.length, 3);
});

test('late verification and resend responses after close do not report success or refresh users', async () => {
  for (const operation of ['verify', 'resend']) {
    for (const fails of [false, true]) {
      const h = harness(), pendingReply = deferred();
      await h.open();
      await h.start({resend_after: 0});
      h.node('email-security-code').value = '123456';
      h.replies.push(() => pendingReply.promise);
      const pending = operation === 'verify'
        ? h.emit('email-security-verify-form') : h.emit('email-security-resend', 'click');
      await h.close();
      const status = h.node('email-security-status').textContent;
      pendingReply.resolve(response(fails ? {success: false, message: 'stale error'}
        : {success: true, message: 'stale success', challenge_id: 'stale', resend_after: 60}, fails ? 400 : 200));
      await pending;
      assert.equal(h.node('email-security-status').textContent, status);
      assert.equal(h.invalidations.length, 0);
      assert.equal(h.userLoads, 0);
      assert.equal(h.node('email-security-verify-form').hidden, true);
      assert.equal(h.timerCount(), 0);
    }
  }
});

test('resend waits for the server cooldown, rotates the challenge, and sends CSRF JSON once', async () => {
  const h = harness();
  await h.open();
  await h.start({resend_after: 3});
  assert.equal(h.node('email-security-resend').textContent, 'Resend in 3s');
  await h.emit('email-security-resend', 'click');
  assert.equal(h.requests.length, 2);
  h.advance(2000);
  assert.equal(h.node('email-security-resend').textContent, 'Resend in 1s');
  assert.equal(h.node('email-security-resend').disabled, true);
  h.advance(1000);
  assert.equal(h.node('email-security-resend').disabled, false);
  assert.equal(h.node('email-security-resend').textContent, 'Resend code');
  const pendingReply = deferred();
  h.replies.push(() => pendingReply.promise);
  h.node('email-security-code').value = '111111';
  const sending = h.emit('email-security-resend', 'click');
  await h.emit('email-security-resend', 'click');
  assert.equal(h.requests.length, 3);
  assert.equal(h.node('email-security-confirm').disabled, true);
  assertJsonRequest(h.requests[2], '/api/users/7/email/resend', {challenge_id: 'challenge-1'});
  pendingReply.resolve(response({success: true, challenge_id: 'challenge-2', resend_after: 5}));
  await sending;
  assert.equal(h.node('email-security-code').value, '');
  assert.equal(h.node('email-security-resend').textContent, 'Resend in 5s');
  assert.equal(h.node('email-security-confirm').disabled, false);
  h.node('email-security-code').value = '222222';
  h.reply({success: true});
  await h.emit('email-security-verify-form');
  assertJsonRequest(h.requests[3], '/api/users/7/email/verify', {challenge_id: 'challenge-2', code: '222222'});
});

test('provider resend errors are plain text and honor Retry-After', async () => {
  const h = harness();
  await h.open();
  await h.start({resend_after: 0});
  const malicious = '<img src=x onerror="steal()"> provider unavailable';
  h.reply({success: false, message: malicious}, 429, {'Retry-After': '4'});
  await h.emit('email-security-resend', 'click');
  assert.equal(h.node('email-security-status').textContent, malicious);
  assert.match(h.node('email-security-status').className, /alert-danger/);
  assert.equal(h.node('email-security-resend').textContent, 'Resend in 4s');
  await h.emit('email-security-resend', 'click');
  assert.equal(h.requests.length, 3);
  h.advance(4000);
  assert.equal(h.node('email-security-resend').disabled, false);
});

test('verify failures clear the code and allow retry without activating email', async () => {
  const h = harness();
  await h.open();
  await h.start();
  h.node('email-security-code').value = '111111';
  const malicious = '<script>alert("provider")</script>';
  h.reply({success: false, error: malicious}, 400);
  await h.emit('email-security-verify-form');
  assert.equal(h.node('email-security-status').textContent, malicious);
  assert.equal(h.node('email-security-code').value, '');
  assert.equal(h.node('email-security-confirm').disabled, false);
  assert.equal(h.node('email-security-resend').disabled, true);
  assert.equal(h.node('email-security-verify-form').hidden, false);
  assert.equal(h.invalidations.length, 0);
});

test('provider send errors and non-JSON responses show readable text and restore the send button', async () => {
  for (const malformed of [false, true]) {
    const h = harness();
    await h.open();
    const malicious = '<svg onload=steal()>mail provider rejected sender</svg>';
    h.replies.push(malformed ? {ok: false, json: async () => { throw new Error('HTML gateway page'); }}
      : response({success: false, message: malicious}, 502));
    await h.emit('email-security-start-form');
    assert.equal(h.node('email-security-status').textContent, malformed
      ? 'Unable to reach the server. Check your connection and try again.' : malicious);
    assert.equal(h.node('email-security-send').disabled, false);
    assert.equal(h.node('email-security-send').textContent, 'Send verification code');
    assert.equal(h.node('email-security-verify-form').hidden, true);
    assert.equal(h.invalidations.length, 0);
  }
});

const savedSettings = {success: true, csrf_token: 'settings-csrf', configured: true,
  smtp_host: 'smtp.resend.com', smtp_port: 465, smtp_username: 'resend',
  smtp_password_configured: true, from_email: 'security@example.com'};

const settingsControls = ['email-smtp-host', 'email-smtp-port', 'email-smtp-username',
  'email-smtp-password', 'email-from', 'email-settings-password', 'save-email-settings-btn'];

function assertSettingsDisabled(h, disabled) {
  for (const id of settingsControls) {
    assert.equal(h.node(id).disabled, disabled, `${id} is ${disabled ? 'disabled' : 'enabled'}`);
  }
  assert.equal(h.node('email-smtp-host').readOnly, true);
  assert.equal(h.node('email-smtp-username').readOnly, true);
}

test('email settings load lazily once, deduplicate pending loads, and never populate the saved SMTP password', async () => {
  const h = harness(), pendingReply = deferred();
  assert.equal(h.requests.length, 0);
  assertSettingsDisabled(h, true);
  h.node('email-settings-password').value = 'stale manager password';
  assert.equal(h.node('save-email-settings-btn').disabled, true);
  h.replies.push(() => pendingReply.promise);
  const loading = h.ui.loadSettings();
  assertSettingsDisabled(h, true);
  await h.ui.loadSettings();
  await h.ui.loadSettings(true);
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].url, '/api/settings/email');
  assert.equal(h.requests[0].options.body, undefined);
  assert.equal(h.requests[0].options.cache, 'no-store');
  pendingReply.resolve(response({...savedSettings, smtp_password: 'must-not-be-displayed'}));
  await loading;
  assert.equal(h.node('email-smtp-host').value, 'smtp.resend.com');
  assert.equal(String(h.node('email-smtp-port').value), '465');
  assert.equal(h.node('email-smtp-username').value, 'resend');
  assert.equal(h.node('email-from').value, 'security@example.com');
  assert.equal(h.node('email-smtp-password').value, '');
  assert.equal(h.node('email-settings-password').value, '');
  assert.match(h.node('email-settings-status').textContent, /configured for every branch/);
  assert.equal(h.node('email-smtp-password').placeholder, 'Password configured — leave blank to keep');
  assertSettingsDisabled(h, false);
  await h.ui.loadSettings();
  assert.equal(h.requests.length, 1, 'revisiting settings does not reload');
});

test('settings navigation preserves drafts and an explicit reload requires discard confirmation', async () => {
  const h = harness();
  h.reply(savedSettings);
  await h.ui.loadSettings();
  h.node('email-from').value = 'draft@example.com';
  h.node('email-smtp-port').value = '2587';
  h.node('email-smtp-password').value = 're_draft';
  h.node('email-settings-password').value = 'draft password';
  await h.emit('email-settings-form', 'input');
  await h.ui.loadSettings();
  await h.ui.loadSettings(true);
  assert.equal(h.requests.length, 1);
  assert.equal(h.confirmations.length, 1);
  assert.match(h.confirmations[0], /Discard unsaved email settings/);
  assert.equal(h.node('email-from').value, 'draft@example.com');
  assert.equal(h.node('email-smtp-port').value, '2587');
  assert.equal(h.node('email-smtp-password').value, 're_draft');
  assert.equal(h.node('email-settings-password').value, 'draft password');
  h.allowDiscard = true;
  h.reply({...savedSettings, from_email: 'new-saved@example.com'});
  await h.ui.loadSettings(true);
  assert.equal(h.requests.length, 2);
  assert.equal(h.node('email-from').value, 'new-saved@example.com');
  assert.equal(String(h.node('email-smtp-port').value), '465');
  assert.equal(h.node('email-smtp-password').value, '');
  assert.equal(h.node('email-settings-password').value, '');
});

test('saving settings sends blank SMTP password to preserve credentials, validates first, and rotates CSRF', async () => {
  const h = harness();
  await h.emit('email-settings-form');
  assert.equal(h.requests.length, 0, 'cannot save before settings load');
  h.reply(savedSettings);
  await h.ui.loadSettings();
  h.node('email-from').value = ' changed@example.com ';
  h.node('email-smtp-port').value = '587';
  h.node('email-settings-form').valid = false;
  await h.emit('email-settings-form');
  assert.equal(h.requests.length, 1);
  h.node('email-settings-form').valid = true;
  h.node('email-settings-password').value = ' manager password with spaces ';
  const pendingReply = deferred();
  h.replies.push(() => pendingReply.promise);
  const saving = h.emit('email-settings-form');
  await h.emit('email-settings-form');
  await h.ui.loadSettings(true);
  assert.equal(h.requests.length, 2, 'saving deduplicates submits and settings loads');
  assertJsonRequest(h.requests[1], '/api/settings/email',
    {smtp_host: 'smtp.resend.com', smtp_port: 587, smtp_username: 'resend',
      smtp_password: '', from_email: 'changed@example.com', password: ' manager password with spaces '}, 'settings-csrf');
  assertSettingsDisabled(h, true);
  pendingReply.resolve(response({...savedSettings, csrf_token: 'new-settings-csrf'}));
  await saving;
  assertSettingsDisabled(h, false);
  assert.equal(h.node('email-settings-password').value, '', 'successful save clears the actor password');
  assert.equal(h.node('save-email-settings-btn').disabled, false);
  assert.match(h.node('email-settings-status').textContent, /saved for every branch/);
  h.node('email-smtp-password').value = ' re_new_secret ';
  h.node('email-settings-password').value = 'fresh manager password';
  await h.emit('email-settings-form', 'input');
  h.reply({...savedSettings, csrf_token: 'new-settings-csrf'});
  await h.emit('email-settings-form');
  assertJsonRequest(h.requests[2], '/api/settings/email',
    {smtp_host: 'smtp.resend.com', smtp_port: 587, smtp_username: 'resend',
      smtp_password: 're_new_secret', from_email: 'changed@example.com', password: 'fresh manager password'}, 'new-settings-csrf');
  assert.equal(h.node('email-smtp-password').value, '', 'successful save clears the typed SMTP password');
  assert.equal(h.node('email-settings-password').value, '');
  await h.ui.loadSettings();
  assert.equal(h.requests.length, 3);
});

test('settings failures display text only, retain unsaved drafts and allow retry', async () => {
  const h = harness();
  const malicious = '<img src=x onerror=steal()> invalid provider configuration';
  h.reply({success: false, message: malicious}, 503);
  await h.ui.loadSettings();
  assert.equal(h.node('email-settings-status').textContent, malicious);
  assertSettingsDisabled(h, true);
  h.reply({...savedSettings, configured: false, smtp_password_configured: false, from_email: ''});
  await h.ui.loadSettings();
  assert.equal(h.node('email-smtp-password').placeholder, 'YOUR_API_KEY');
  assert.match(h.node('email-settings-status').textContent, /Not configured/);
  h.node('email-from').value = 'draft@example.com';
  h.node('email-smtp-password').value = 're_new_secret';
  h.node('email-settings-password').value = 'manager password';
  await h.emit('email-settings-form', 'input');
  h.reply({success: false, message: malicious}, 400);
  await h.emit('email-settings-form');
  assert.equal(h.node('email-settings-status').textContent, malicious);
  assert.match(h.node('email-settings-status').className, /alert-danger/);
  assert.equal(h.node('email-from').value, 'draft@example.com');
  assert.equal(h.node('email-smtp-password').value, 're_new_secret');
  assertSettingsDisabled(h, false);
  await h.ui.loadSettings();
  assert.equal(h.requests.length, 3, 'failed save must not auto-reload and erase the draft');
});

test('saving an incomplete SMTP setup never claims that a missing password is configured', async () => {
  const h = harness();
  h.reply({...savedSettings, configured: false, smtp_password_configured: false});
  await h.ui.loadSettings();
  h.node('email-settings-password').value = 'manager password';
  h.reply({...savedSettings, configured: false, smtp_password_configured: false, message: 'Email settings saved.'});
  await h.emit('email-settings-form');
  assert.equal(h.node('email-smtp-password').value, '');
  assert.equal(h.node('email-settings-password').value, '');
  assert.equal(h.node('email-smtp-password').placeholder, 'YOUR_API_KEY');
  assert.match(h.node('email-settings-status').textContent, /Delivery is not configured/);
  assert.match(h.node('email-settings-status').className, /alert-danger/);
  assertSettingsDisabled(h, false);
});

test('SMTP security follows the loaded port and every allowed port change without a request', async () => {
  const h = harness();
  assert.equal(String(h.node('email-smtp-port').value), '465');
  h.reply({...savedSettings, smtp_port: 2587});
  await h.ui.loadSettings();
  assert.equal(String(h.node('email-smtp-port').value), '2587');
  assert.match(h.node('email-smtp-security').textContent, /STARTTLS/);
  for (const [port, security] of [['465', 'SSL/TLS'], ['25', 'STARTTLS'],
    ['2465', 'SSL/TLS'], ['587', 'STARTTLS'], ['2587', 'STARTTLS']]) {
    h.node('email-smtp-port').value = port;
    await h.emit('email-smtp-port', 'change');
    assert.ok(h.node('email-smtp-security').textContent.includes(security), `${port} explains ${security}`);
    assert.ok(!h.node('email-smtp-security').textContent.includes(security === 'STARTTLS' ? 'SSL/TLS' : 'STARTTLS'),
      `${port} does not describe the other connection mode`);
    assert.equal(h.requests.length, 1, 'security text changes locally, without saving or reloading');
  }
  await h.ui.loadSettings(true);
  assert.equal(h.confirmations.length, 1, 'changing only a port counts as an unsaved draft');
  assert.equal(h.requests.length, 1, 'declining reload preserves the changed port');
  assert.equal(h.node('email-smtp-port').value, '2587');
});

test('SMTP fields expose only the fixed provider, allowed ports and a blank bounded secret', () => {
  for (const [id, value] of [['email-smtp-host', 'smtp.resend.com'], ['email-smtp-username', 'resend']]) {
    const tag = tagFor(dashboard, id);
    assert.match(tag, /^<input\b/);
    assert.match(tag, /type="text"/);
    assert.ok(tag.includes(`value="${value}"`));
    assert.match(tag, /\sreadonly(?:\s|\/?>)/);
    assert.match(tag, /\srequired(?:\s|\/?>)/);
  }
  const port = dashboard.match(/<select\b[^>]*\bid="email-smtp-port"[^>]*>([\s\S]*?)<\/select>/);
  assert.ok(port, 'SMTP port is a constrained select');
  const options = [...port[1].matchAll(/<option\b([^>]*)>/g)];
  assert.deepEqual(options.map(option => option[1].match(/\bvalue="([^"]+)"/)[1]).sort(),
    ['25', '2587', '2465', '465', '587'].sort());
  assert.equal(element('email-smtp-port').value, '465');
  const password = tagFor(dashboard, 'email-smtp-password');
  assert.match(password, /type="password"/);
  assert.match(password, /maxlength="512"/);
  assert.doesNotMatch(password, /\s(?:value=|required(?:\s|\/?>))/);
  assert.equal(element('email-smtp-password').value, '', 'stored SMTP password is never rendered');
  assert.match(tagFor(dashboard, 'email-smtp-security'), /(?:role="status"|aria-live="polite")/);
  assert.doesNotMatch(dashboard, /\bid="email-api-key"/);
  assert.doesNotMatch(source, /email-api-key|api_key_configured/);
});

test('sidebar has no My login security entry while Users retain targeted email actions', () => {
  const sidebar = dashboard.match(/\bid="sidebar"[\s\S]*?\bid="sidebar-drag-handle"[^>]*>/);
  assert.ok(sidebar, 'dashboard sidebar exists');
  assert.doesNotMatch(sidebar[0], /My login security/i);
  assert.doesNotMatch(sidebar[0], /EmailSecurity\.open\(\s*\)/);
  assert.match(dashboard, /onclick="EmailSecurity\.open\(\$\{Number\(user\.id\)\}\)"/);
});

test('Users keeps own-account email access independent of the current branch roster', async () => {
  const ownButton = tagFor(dashboard, 'users-own-email-btn');
  const usersSection = dashboard.indexOf('id="users-section"');
  assert.ok(dashboard.indexOf('id="users-own-email-btn"') > usersSection);
  assert.ok(dashboard.indexOf('id="users-own-email-btn"') < dashboard.indexOf('id="logs-section"'));
  assert.match(ownButton, /onclick="EmailSecurity\.open\(\)"/);
  const h = harness();
  await h.open({username: 'Owner'});
  assert.deepEqual(h.requests.map(request => request.url), ['/api/auth/email']);
  assert.equal(h.node('email-security-title').textContent, 'Add email — Owner');
  assert.equal(h.node('email-security-start-form').hidden, false);
});

test('dashboard templates wire targeted Users actions, lazy settings and browser input validation', () => {
  assert.match(dashboard, /onclick="EmailSecurity\.open\(\$\{Number\(user\.id\)\}\)"/);
  assert.match(dashboard, /user\.email \? "Unlink email" : "Add email"/);
  assert.match(dashboard, /user\.two_factor_enabled \? "Two-step on" : "Password only"/);
  assert.match(dashboard, /groupId === "email"[^\n]*EmailSecurity\.loadSettings\(\)/);
  assert.match(dashboard, /<script[^>]+src="\/public\/email-security\.js[^\"]*"/);
  assert.match(tagFor(dashboard, 'emailSecurityModal'), /data-current-user="\{\{ session\.get\('user_id'\) \}\}"/);
  assert.match(tagFor(dashboard, 'email-security-address'), /type="email"/);
  assert.match(tagFor(dashboard, 'email-security-password'), /type="password"/);
  assert.match(tagFor(dashboard, 'email-security-password'), /autocomplete="current-password"/);
  for (const id of ['email-security-password', 'email-security-address', 'email-security-code', 'email-settings-password']) {
    assert.match(tagFor(dashboard, id), /\srequired(?:\s|\/?>)/);
  }
  assert.match(tagFor(dashboard, 'email-security-code'), /pattern="\[0-9\]\{6\}"/);
  assert.match(tagFor(dashboard, 'email-security-code'), /autocomplete="one-time-code"/);
  assert.match(tagFor(dashboard, 'email-smtp-password'), /type="password"/);
  assert.doesNotMatch(tagFor(dashboard, 'email-smtp-password'), /\svalue=/);
  assert.match(tagFor(dashboard, 'email-settings-password'), /type="password"/);
  assert.match(tagFor(dashboard, 'email-settings-password'), /autocomplete="current-password"/);
  assert.doesNotMatch(tagFor(dashboard, 'email-settings-password'), /\svalue=/);
  assert.match(tagFor(dashboard, 'email-security-status'), /aria-live="polite"/);
  assert.match(tagFor(dashboard, 'email-settings-status'), /aria-live="polite"/);
});

test('login challenge forms require a six-digit code and CSRF for verification and resend', () => {
  for (const [id, action] of [['verifyLoginForm', '/login/verify'], ['resendLoginForm', '/login/resend']]) {
    const form = login.match(new RegExp(`<form\\b[^>]*id="${id}"[^>]*>[\\s\\S]*?<\\/form>`));
    assert.ok(form, `${id} exists`);
    assert.match(form[0], /method="POST"/);
    assert.ok(form[0].includes(`action="${action}"`));
    assert.match(form[0], /name="csrf_token" value="\{\{ csrf_token \}\}"/);
  }
  assert.match(tagFor(login, 'login-code'), /pattern="\[0-9\]\{6\}"/);
  assert.match(tagFor(login, 'login-code'), /autocomplete="one-time-code"/);
  assert.match(tagFor(login, 'login-code'), /\srequired(?:\s|>)/);
  assert.match(tagFor(login, 'resend-login-code'), /data-wait="\{\{ resend_after\|default\(60\) \}\}"/);
  assert.match(login, /\{\{ error \}\}/);
  assert.doesNotMatch(login, /\{\{\s*(?:error|info|masked_email)[^}]*\|\s*safe/);
});

test('login template countdown enables resend only when due and prevents duplicate form submission', () => {
  const scripts = [...login.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)]
    .map(match => match[1]).filter(script => script.includes('resend-login-code'));
  assert.equal(scripts.length, 1, 'one login challenge controller');
  let ready, now = 1000, tick, cleared = false;
  const resend = {dataset: {wait: '3'}, disabled: false, textContent: ''};
  const verifyButton = {disabled: false};
  const forms = Object.fromEntries(['verifyLoginForm', 'resendLoginForm'].map(id => {
    const button = id === 'verifyLoginForm' ? verifyButton : resend;
    return [id, {submit: null,
      addEventListener(type, fn) { assert.equal(type, 'submit'); this.submit = fn; },
      querySelector(selector) { assert.equal(selector, 'button[type="submit"]'); return button; }}];
  }));
  vm.runInNewContext(scripts[0], {
    document: {
      addEventListener(type, fn) { assert.equal(type, 'DOMContentLoaded'); ready = fn; },
      getElementById(id) {
        assert.ok(id === 'resend-login-code' || forms[id], 'challenge skips CAPTCHA and password-only controls');
        return id === 'resend-login-code' ? resend : forms[id];
      },
    },
    Date: {now: () => now},
    setInterval(fn) { tick = fn; return 1; },
    clearInterval(id) { assert.equal(id, 1); cleared = true; },
  }, {filename: 'login.html challenge script'});
  ready();
  assert.equal(resend.disabled, true);
  assert.equal(resend.textContent, 'Resend in 3s');
  now += 2000;
  tick();
  assert.equal(resend.disabled, true);
  assert.equal(resend.textContent, 'Resend in 1s');
  now += 1000;
  tick();
  assert.equal(resend.disabled, false);
  assert.equal(resend.textContent, 'Resend code');
  assert.equal(cleared, true);
  forms.verifyLoginForm.submit.call(forms.verifyLoginForm);
  assert.equal(verifyButton.disabled, true);
  forms.resendLoginForm.submit.call(forms.resendLoginForm);
  assert.equal(resend.disabled, true);
});