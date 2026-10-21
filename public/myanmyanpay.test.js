// Run: node --test public/myanmyanpay.test.js
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {createCheckout} = require('./myanmyanpay.js');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Exercise the real browser adapter as well as the state machine. No browser
// dependency is needed for button visibility, labels and in-flight guards.
async function buttonHarness({enabled = true, configured = true, manual_allowed = false, sandbox = false, dashboard = false} = {}) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) {
      const classes = new Set();
      const attributes = new Map();
      elements.set(id, {
        textContent: '', value: '', checked: false, disabled: false, style: {}, children: [],
        classList: {
          add: name => classes.add(name), remove: name => classes.delete(name),
          contains: name => classes.has(name),
          toggle(name, force) { if (force) classes.add(name); else classes.delete(name); },
        },
        setAttribute: (name, value) => attributes.set(name, value),
        getAttribute: name => attributes.get(name),
        removeAttribute: name => attributes.delete(name),
        listeners: {}, focus() {},
        addEventListener(name, fn) { this.listeners[name] = fn; },
        replaceChildren() { this.children = []; },
        appendChild(child) { this.children.push(child); },
      });
    }
    return elements.get(id);
  };
  const h = {element, status: 'PENDING', calls: [], completions: [], opens: 0, notices: [], sequence: 0};
  const context = {
    document: {getElementById: element, createElement: () => element(Symbol()), addEventListener() {}},
    currentBranch: {id: 1}, CACHE_USER_ID: 1, cart: [], navigator: {onLine: true},
    bootstrap: {Modal: {getInstance: () => ({
      show() { h.opens++; element('mmqrPaymentModal').classList.add('show'); },
      hide() {
        const modal = element('mmqrPaymentModal');
        modal.classList.remove('show');
        modal.listeners['hidden.bs.modal']?.();
      },
    })}},
    crypto: {randomUUID: () => `button-test-${++h.sequence}`},
    setTimeout: fn => { h.tick = fn; return 1; }, clearTimeout() {},
    resetMMQRCountdown() {}, stopMMQRCountdown() {}, setMMQRDownloadState() {},
    confirmMMQRPayment() {},
    updateCartDisplay() {}, resetSplitPaymentFields() {}, togglePosDeliveryFields() {},
    updateCashRefundDisplay() {}, loadSales() {}, loadDashboardStats() {}, invalidateProductsCache() {},
    showSaleSuccessModal: (...args) => h.completions.push(args),
    showToast: (...args) => h.notices.push(args), formatCurrency: value => String(value),
    fetch: async (url, options) => {
      h.calls.push({url, options});
      if (h.intercept) { const response = await h.intercept(url, options); if (response) return response; }
      const payment = {id: 7, order_id: 'button-order', status: h.status, amount: 1000, qr_url: '/qr'};
      const data = url.endsWith('/config') ? {enabled, configured, manual_allowed, sandbox}
        : url.endsWith('/complete') ? {success: true, transaction_id: 'sandbox-sale'}
        : url.endsWith('/intents') && options.method === 'GET' ? {payments: []}
        : {payment};
      return {ok: true, json: async () => data};
    },
  };
  if (dashboard) {
    const source = fs.readFileSync(path.join(__dirname, '..', 'templates', 'dashboard.html'), 'utf8');
    const functions = ['addToCartToInternal', 'updateCartDisplay', 'showMMQRPaymentModal',
      'completeSale', 'splitUsesMobilePayment', 'resetSplitPaymentFields',
      'updateSplitPaymentDisplay', 'handlePaymentMethodChange', 'toggleCashPaymentFields',
      'getCurrentCartTotal', 'toCents', 'centsToNumber', 'mulMoney'];
    const helpers = functions.map(name => {
      const match = new RegExp(`function ${name}\\([^)]*\\)\\s*\\{`).exec(source);
      assert.ok(match, `dashboard function ${name} exists`);
      let depth = 0;
      for (let i = match.index + match[0].length - 1; i < source.length; i++) {
        if (source[i] === '{') depth++;
        if (source[i] === '}' && --depth === 0) return source.slice(match.index, i + 1);
      }
      throw new Error(`Unbalanced dashboard function: ${name}`);
    }).join('\n');
    Object.assign(context, {isSaleProcessing: false, APP_SETTINGS: {},
      t: text => text, hasCapability: () => true});
    vm.runInNewContext(helpers, context);
    element('payment-method').value = 'mobile_payment';
    element('split-method-one').value = 'cash';
    element('split-method-two').value = 'mobile_payment';
  }
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'myanmyanpay.js'), 'utf8'), context);
  await new Promise(setImmediate);
  h.checkout = context.MyanMyanPayCheckout;
  h.context = context;
  h.hidden = id => element(id).classList.contains('d-none');
  return h;
}

test('after a finished mobile sale, selecting products builds the next cart without reopening MMQR', async () => {
  const h = await buttonHarness({dashboard: true});
  const product = {id: 5, name: 'Next sale', price: 1000, stock: 10};
  h.context.cart = [{product_id: 5, price: 1000, quantity: 1, tax_rate: 0}];
  h.context.completeSale();
  await new Promise(setImmediate);
  assert.equal(h.checkout.state().active.status, 'PENDING');
  h.status = 'SUCCESS';
  await h.tick();
  assert.equal(h.completions.length, 1);
  assert.equal(h.context.cart.length, 0);
  assert.equal(h.checkout.state().active, null);
  const calls = h.calls.length, opens = h.opens;
  h.context.addToCartToInternal(product, 1);
  await new Promise(setImmediate);
  assert.equal(h.opens, opens, 'adding the next product must not reopen the payment window');
  assert.equal(h.calls.length, calls, 'editing the next cart must not create a frozen payment');
  assert.equal(h.element('payment-method').value, 'mobile_payment');
  h.context.addToCartToInternal({...product, id: 6}, 1);
  h.status = 'PENDING';
  h.context.completeSale();
  await new Promise(setImmediate);
  assert.equal(h.checkout.state().active.status, 'PENDING');
  assert.equal(h.element('pos-mmqr-image').style.display, 'block');
  const creates = h.calls.filter(c => c.url.endsWith('/intents') && c.options.method === 'POST');
  assert.equal(creates.length, 2, 'one payment for each explicitly checked-out sale');
  assert.equal(JSON.parse(creates[1].options.body).items.length, 2, 'the new QR includes the entire new cart');
  assert.notEqual(JSON.parse(creates[0].options.body).transaction_id, JSON.parse(creates[1].options.body).transaction_id);
});

for (const method of ['mobile_payment', 'split_payment']) {
  test(`editing the next ${method} cart does not request payment until explicit checkout`, async () => {
    const h = await buttonHarness({dashboard: true});
    h.element('payment-method').value = method;
    const opens = h.opens, calls = h.calls.length;
    h.context.addToCartToInternal({id: 5, name: 'New cart', price: 1000, stock: 10}, 1);
    h.context.cart[0].quantity = 2;
    h.context.updateCartDisplay();
    h.context.cart[0].quantity = 1;
    h.context.updateCartDisplay();
    assert.equal(h.opens, opens);
    assert.equal(h.calls.length, calls);
    h.element('split-amount-one').value = '200';
    h.element('split-amount-two').value = '800';
    h.context.handlePaymentMethodChange();
    await new Promise(setImmediate);
    assert.equal(h.checkout.state().active.status, 'PENDING', 'explicit payment selection still creates a QR');
    assert.equal(h.element('pos-mmqr-image').style.display, 'block');
    const pendingCalls = h.calls.length, pendingOpens = h.opens;
    h.context.cart[0].quantity = 2;
    h.context.updateCartDisplay();
    assert.equal(h.calls.length, pendingCalls, 'editing must not replace an existing payment');
    assert.equal(h.opens, pendingOpens);
    h.context.completeSale();
    assert.equal(h.checkout.state().active.id, 7, 'explicit checkout resumes the original pending order');
    assert.equal(h.calls.length, pendingCalls);
  });

  test(`completion does not reopen ${method} checkout through split-field reset`, async () => {
    const h = await buttonHarness({dashboard: true});
    h.element('payment-method').value = method;
    h.element('split-amount-one').value = '200';
    h.element('split-amount-two').value = '800';
    h.context.cart = [{product_id: 5, price: 1000, quantity: 1, tax_rate: 0}];
    h.context.completeSale();
    await new Promise(setImmediate);
    // Exercise a delayed hide with the dialog still shown during field reset,
    // before hidden.bs.modal, rather than only testing synchronous closure.
    h.context.bootstrap.Modal.getInstance = () => ({
      show() { h.opens++; }, hide() {},
    });
    const opens = h.opens;
    h.status = 'SUCCESS';
    await h.tick();
    assert.equal(h.completions.length, 1);
    assert.equal(h.opens, opens, 'resetting the finished sale must not open an empty checkout');
    assert.equal(h.checkout.state().active, null);
    assert.equal(h.checkout.state().quote, null);
    assert.equal(h.notices.length, 0, 'clearing a completed cart must not warn about missing items');
  });
}

test('payment readiness refresh after completion cannot reopen the completed checkout', async () => {
  const h = harness();
  let opens = 0;
  h.env.open = () => { opens++; };
  await h.checkout.loadConfig();
  await h.checkout.start({items: [1]});
  h.status = 'SUCCESS';
  await h.tick();
  assert.equal(h.completed.length, 1);
  const completedOpens = opens;
  await h.checkout.loadConfig();
  assert.equal(opens, completedOpens, 'completion must clear visibility even before hidden.bs.modal');
  assert.equal(h.checkout.state().active, null);
});

test('building a manual mobile cart does not warn about an unavailable QR before checkout', async () => {
  const h = await buttonHarness({dashboard: true, enabled: false, configured: false, manual_allowed: true});
  const opens = h.opens, calls = h.calls.length;
  h.context.addToCartToInternal({id: 5, name: 'Manual cart', price: 1000, stock: 10}, 1);
  assert.equal(h.opens, opens);
  assert.equal(h.calls.length, calls);
  assert.equal(h.notices.length, 1, 'only the product-added notice is shown');
  assert.doesNotMatch(h.notices[0][0], /MMQR|configured|unavailable/i);
  h.context.completeSale();
  assert.match(h.notices.at(-1)[0], /MMQR has not been configured/, 'actual checkout still reports missing configuration');
  assert.equal(h.checkout.state().active, null);
  assert.equal(h.completions.length, 0, 'missing QR must never record a sale');
});

test('payment recovery is labelled as discovery until an order can actually be resumed', async () => {
  const h = await buttonHarness();
  const button = h.element('mmp-open-existing');
  assert.equal(button.textContent, 'Find unfinished payments');
  assert.equal(button.disabled, false);
  const posts = h.calls.filter(c => c.options.method === 'POST').length;
  await button.onclick();
  assert.equal(h.opens, 1);
  assert.match(h.element('mmp-status').textContent, /No unfinished payments found/);
  assert.equal(h.element('mmp-existing').children.length, 0);
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length, posts);
});

test('empty discovery finishes without a generating message, QR controls or checkout action', async () => {
  const h = await buttonHarness();
  let finish;
  h.intercept = url => url.endsWith('/intents') ? new Promise(resolve => { finish = resolve; }) : undefined;
  const searching = h.element('mmp-open-existing').onclick();
  await new Promise(setImmediate);
  assert.doesNotMatch(h.element('pos-mmqr-missing').textContent, /Generating/);
  finish({ok: true, json: async () => ({payments: []})});
  await searching;
  assert.match(h.element('pos-mmqr-missing').textContent, /No unfinished payments/);
  assert.doesNotMatch(h.element('pos-mmqr-missing').textContent, /Generating/);
  assert.equal(h.hidden('confirm-mmqr-payment-btn'), true);
  assert.equal(h.hidden('mmqr-timer-box'), true);
  assert.equal(h.hidden('mmqr-downloads'), true);
  assert.equal(h.hidden('mmqr-amount-summary'), true);
  assert.equal(h.element('mmqr-payment-title').textContent, 'Find unfinished payments');
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
});

test('server-verified payment opens Done/receipt automatically exactly once', async () => {
  const h = await buttonHarness();
  await h.checkout.start({items: [1]});
  h.status = 'SUCCESS';
  await h.tick();
  assert.equal(h.completions.length, 1);
  assert.equal(h.checkout.state().active, null);
  assert.equal(h.checkout.state().busy, false);
  await h.tick();
  await h.checkout.action('complete');
  assert.equal(h.calls.filter(c => c.url.endsWith('/complete')).length, 1);
});

test('automatic sale-recording failure keeps paid order and retries only on explicit action', async () => {
  const h = harness(); await h.checkout.loadConfig();
  await h.checkout.start({items: [1]});
  h.status = 'SUCCESS';
  h.intercept = path => path.endsWith('/complete') ? {
    ok: false, json: async () => ({message: 'Stock changed; reconcile this sale'})
  } : undefined;
  await h.tick();
  assert.equal(h.calls.filter(c => c.path.endsWith('/complete')).length, 1);
  assert.equal(h.checkout.state().active.status, 'SUCCESS');
  assert.equal(h.checkout.state().busy, false);
  assert.match(h.rendered.text, /do not pay again/);
  await h.tick(); await h.tick();
  assert.equal(h.calls.filter(c => c.path.endsWith('/complete')).length, 1);
  h.intercept = null;
  await h.checkout.action('complete');
  assert.equal(h.completed.length, 1);
});

test('automatic completion holds the busy lock and never submits a second completion', async () => {
  const h = harness(); await h.checkout.loadConfig(); await h.checkout.start({items: [1]});
  let finish;
  h.intercept = path => path.endsWith('/complete') ? new Promise(resolve => { finish = resolve; }) : undefined;
  h.status = 'SUCCESS';
  const polling = h.tick();
  await new Promise(setImmediate);
  assert.equal(h.checkout.state().busy, true);
  await h.checkout.action('complete'); await h.checkout.action('refresh'); await h.tick();
  assert.equal(h.calls.filter(c => c.path.endsWith('/complete')).length, 1);
  finish({ok: true, json: async () => ({success: true, transaction_id: 'sale1'})});
  await polling;
  assert.equal(h.completed.length, 1);
  assert.equal(h.completed[0][1], true);
  assert.equal(h.checkout.state().busy, false);
});

test('late automatic completion response cannot finish a sale in a different branch', async () => {
  const h = harness(); await h.checkout.loadConfig(); await h.checkout.start({items: [1]});
  let finish;
  h.intercept = path => path.endsWith('/complete') ? new Promise(resolve => { finish = resolve; }) : undefined;
  h.status = 'SUCCESS'; const polling = h.tick(); await new Promise(setImmediate);
  h.scope = 'other-branch';
  finish({ok: true, json: async () => ({success: true, transaction_id: 'sale1'})});
  await polling;
  assert.equal(h.completed.length, 0);
});

test('cached or listed success is revalidated before automatic completion', async () => {
  const h = harness(); await h.checkout.loadConfig();
  h.intercept = path => path.endsWith('/intents/7') ? {ok: false, json: async () => ({message: 'Unavailable'})} : undefined;
  h.checkout.preview(); h.checkout.resume({id: 7, status: 'SUCCESS'});
  await new Promise(setImmediate);
  assert.equal(h.calls.filter(c => c.path.endsWith('/complete')).length, 0);
  assert.equal(h.completed.length, 0);
});

test('recovering a paid order records its sale while retaining an unrelated cart', async () => {
  const h = harness(); await h.checkout.loadConfig();
  h.status = 'SUCCESS'; h.checkout.preview(); h.checkout.resume({id: 7, status: 'PENDING'});
  await new Promise(setImmediate);
  assert.equal(h.completed.length, 1);
  assert.equal(h.completed[0][1], false);
  assert.equal(h.calls.filter(c => c.path.endsWith('/intents') && c.options.method === 'POST').length, 0);
});

test('reload revalidates a saved paid order and automatically opens its receipt without a new payment', async () => {
  const h = harness(); h.status = 'SUCCESS'; h.env.restore = () => ({id: 7, status: 'SUCCESS'});
  await h.checkout.loadConfig(); await new Promise(setImmediate);
  assert.equal(h.completed.length, 1);
  assert.equal(h.completed[0][1], false);
  assert.equal(h.checkout.state().busy, false);
  assert.equal(h.calls.filter(c => c.path.endsWith('/complete')).length, 1);
  assert.equal(h.calls.filter(c => c.path.endsWith('/intents') && c.options.method === 'POST').length, 0);
});

test('discovery hides the uploaded manual QR without enabling automatic manual sales', async () => {
  const h = await buttonHarness({enabled: false, configured: false, manual_allowed: true});
  h.element('pos-mmqr-image').style.display = 'block';
  await h.element('mmp-open-existing').onclick();
  assert.equal(h.element('pos-mmqr-image').style.display, 'none');
  assert.match(h.element('pos-mmqr-missing').textContent, /No unfinished payments/);
  assert.equal(h.hidden('confirm-mmqr-payment-btn'), true);
  assert.equal(h.completions.length, 0);
  h.checkout.preview('checkout');
  assert.equal(h.hidden('confirm-mmqr-payment-btn'), false);
  assert.equal(h.hidden('mmqr-qr-surface'), false);
  assert.equal(h.hidden('mmqr-timer-box'), false);
});

test('resume reopens the active order without searching or creating another payment', async () => {
  const h = await buttonHarness();
  await h.checkout.start({items: [1]});
  h.checkout.close();
  const button = h.element('mmp-open-existing');
  assert.equal(button.textContent, 'Resume payment — MMK 1,000.00');
  const calls = h.calls.length, opens = h.opens;
  await button.onclick();
  assert.equal(h.opens, opens + 1);
  assert.equal(h.calls.length, calls, 'reopen must not query or replace the selected order');
  assert.equal(h.checkout.state().active.id, 7);
  h.status = 'CANCELLED';
  await h.checkout.action('cancel');
  assert.equal(button.textContent, 'Find unfinished payments');
});

test('payment recovery is disabled during discovery and recovers after an error', async () => {
  const h = await buttonHarness();
  let finish;
  h.intercept = url => url.endsWith('/intents')
    ? new Promise(resolve => { finish = resolve; }) : undefined;
  const finding = h.element('mmp-open-existing').onclick();
  assert.equal(h.element('mmp-open-existing').disabled, true);
  finish({ok: false, json: async () => ({message: 'Connection interrupted; retry discovery'})});
  await finding;
  assert.equal(h.element('mmp-open-existing').disabled, false);
  assert.match(h.element('mmp-status').textContent, /Connection interrupted/);
});

test('discovery can select and resume a server-owned payment without creating or completing it', async () => {
  const h = await buttonHarness();
  h.intercept = (url, options) => url.endsWith('/intents') && options.method === 'GET'
    ? {ok: true, json: async () => ({payments: [{id: 7, amount: 1000, status: 'PENDING'}]})}
    : undefined;
  await h.element('mmp-open-existing').onclick();
  assert.match(h.element('mmp-status').textContent, /Select an existing payment/);
  const [choice] = h.element('mmp-existing').children;
  assert.match(choice.textContent, /Resume 7: MMK 1,000.00/);
  choice.onclick();
  await new Promise(setImmediate);
  assert.equal(h.checkout.state().active.id, 7);
  assert.equal(h.element('mmp-existing').children.length, 0);
  assert.equal(h.element('mmp-open-existing').textContent, 'Resume payment — MMK 1,000.00');
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
});

test('disabled configured provider keeps discovery usable without exposing new payment creation', async () => {
  const h = await buttonHarness({enabled: false, manual_allowed: false});
  assert.equal(h.hidden('mmp-open-existing'), false);
  await h.element('mmp-open-existing').onclick();
  assert.match(h.element('mmp-status').textContent, /No unfinished payments found/);
  assert.equal(h.element('confirm-mmqr-payment-btn').disabled, true);
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
});

test('pending order promotes status check and hides sale completion and discovery', async () => {
  const h = await buttonHarness();
  await h.checkout.start({items: [1]});
  assert.equal(h.hidden('confirm-mmqr-payment-btn'), true);
  assert.equal(h.element('confirm-mmqr-payment-btn').disabled, true);
  assert.equal(h.hidden('mmp-refresh'), false);
  assert.equal(h.element('mmp-refresh').textContent, 'Check now');
  assert.equal(h.hidden('mmp-auto-status'), false);
  assert.match(h.element('mmp-auto-status').textContent, /Checking payment status automatically/);
  assert.equal(h.hidden('mmp-cancel'), false);
  assert.equal(h.hidden('mmp-resume'), true);
  assert.equal(h.element('mmqr-close-btn').textContent, 'Close — resume later');
  assert.equal(h.hidden('mmqr-close-note'), false);
});

test('status refresh disables payment actions and automatically records verified payment', async () => {
  const h = await buttonHarness();
  await h.checkout.start({items: [1]});
  let finish;
  h.intercept = url => url.endsWith('/refresh') ? new Promise(resolve => { finish = resolve; }) : undefined;
  const refreshing = h.checkout.action('refresh');
  for (const id of ['mmp-refresh', 'mmp-cancel', 'mmp-resume', 'confirm-mmqr-payment-btn']) {
    assert.equal(h.element(id).disabled, true);
  }
  assert.equal(h.element('mmp-refresh').textContent, 'Please wait…');
  assert.equal(h.element('mmp-refresh').getAttribute('aria-busy'), 'true');
  finish({ok: true, json: async () => ({payment: {id: 7, status: 'SUCCESS', amount: 1000}})});
  await refreshing;
  assert.equal(h.hidden('mmp-refresh'), true);
  assert.equal(h.checkout.state().active, null);
  assert.equal(h.completions.length, 1);
  assert.equal(h.calls.filter(c => c.url.endsWith('/complete')).length, 1);
  assert.equal(h.element('confirm-mmqr-payment-btn').getAttribute('aria-busy'), 'false');
});

test('failed refresh and failed completion restore usable buttons without allowing another payment', async () => {
  const h = await buttonHarness();
  await h.checkout.start({items: [1]});
  h.intercept = url => url.endsWith('/refresh') || url.endsWith('/complete')
    ? {ok: false, json: async () => ({message: 'Try again safely'})} : undefined;
  await h.checkout.action('refresh');
  assert.equal(h.element('mmp-refresh').disabled, false);
  assert.equal(h.element('mmp-refresh').textContent, 'Check now');
  assert.equal(h.hidden('confirm-mmqr-payment-btn'), true);
  h.status = 'SUCCESS';
  h.checkout.resume({id: 7, status: 'SUCCESS'});
  await new Promise(setImmediate);
  await h.checkout.action('complete');
  assert.equal(h.element('confirm-mmqr-payment-btn').disabled, false);
  assert.equal(h.element('confirm-mmqr-payment-btn').textContent, 'Complete verified sale');
  assert.equal(h.hidden('mmp-cancel'), true);
  assert.match(h.element('mmp-status').textContent, /reconciliation/);
});

test('webhook success automatically records the sale without a refresh or completion click', async () => {
  const h = await buttonHarness();
  await h.checkout.start({items: [1]});
  h.status = 'SUCCESS'; // Next GET reads the state persisted by the verified callback.
  await h.tick();
  assert.equal(h.hidden('mmp-auto-status'), true);
  assert.equal(h.completions.length, 1);
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 2, 'one create and one idempotent sale completion');
});

test('rejected creation offers retry and discovery; cancellation restores checkout actions', async () => {
  const h = await buttonHarness();
  h.intercept = (url, options) => url.endsWith('/intents') && options.method === 'POST'
    ? {ok: false, json: async () => ({message: 'Checkout rejected'})} : undefined;
  await h.checkout.start({items: [1]});
  assert.equal(h.element('confirm-mmqr-payment-btn').textContent, 'Retry payment QR');
  assert.equal(h.element('confirm-mmqr-payment-btn').disabled, false);
  assert.equal(h.hidden('mmp-resume'), false);
  assert.equal(h.hidden('mmp-refresh'), true);
  assert.equal(h.hidden('mmp-cancel'), true);
  h.intercept = null;
  await h.checkout.start({items: [1]});
  h.status = 'CANCELLED';
  await h.checkout.action('cancel');
  assert.equal(h.hidden('confirm-mmqr-payment-btn'), false);
  assert.equal(h.hidden('mmp-resume'), false);
  assert.equal(h.hidden('mmp-cancel'), true);
  assert.equal(h.hidden('mmqr-close-note'), true);
  assert.equal(h.element('mmqr-close-btn').textContent, 'Close');
});

test('blocked setup fails closed and legacy checkout retains manual completion', async () => {
  const blocked = await buttonHarness({configured: false});
  blocked.checkout.preview();
  assert.equal(blocked.element('confirm-mmqr-payment-btn').disabled, true);
  assert.equal(blocked.element('confirm-mmqr-payment-btn').textContent, 'Payment unavailable');
  for (const id of ['mmp-refresh', 'mmp-cancel', 'mmp-resume']) assert.equal(blocked.hidden(id), true);
  const legacy = await buttonHarness({enabled: false, manual_allowed: true});
  assert.equal(legacy.element('confirm-mmqr-payment-btn').disabled, false);
  assert.equal(legacy.hidden('confirm-mmqr-payment-btn'), false);
  assert.equal(legacy.element('confirm-mmqr-payment-btn').textContent, 'Record manually confirmed sale');
  assert.equal(legacy.hidden('mmp-resume'), false);
  assert.equal(legacy.hidden('mmp-refresh'), true);
  assert.equal(legacy.hidden('mmp-cancel'), true);
});

test('disabled provider without explicit manual permission cannot expose manual completion', async () => {
  const h = await buttonHarness({enabled: false, configured: false});
  h.checkout.preview();
  assert.equal(h.checkout.mode(), 'blocked');
  assert.equal(h.element('confirm-mmqr-payment-btn').disabled, true);
  await h.checkout.start({items: [1]});
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
});

test('sandbox checkout distinguishes simulated success from real money', async () => {
  const h = await buttonHarness({sandbox: true});
  await h.checkout.start({items: [1]});
  assert.match(h.element('mmqr-payment-help').textContent, /SANDBOX.*no real money/);
  h.status = 'SUCCESS';
  await h.tick();
  assert.equal(h.checkout.state().active, null);
  assert.match(h.element('mmqr-payment-help').textContent, /stock|reports/);
  await h.checkout.action('complete');
  assert.equal(h.completions.length, 1);
  assert.match(h.completions[0][3], /SANDBOX.*simulated.*no real money/);
});

test('older config without explicit manual permission also fails closed', async () => {
  const h = await buttonHarness();
  h.intercept = async url => url.endsWith('/config') ? {
    ok: true, json: async () => ({enabled: false, configured: false})
  } : undefined;
  await h.checkout.loadConfig();
  h.checkout.preview();
  assert.equal(h.checkout.mode(), 'blocked');
  assert.equal(h.element('confirm-mmqr-payment-btn').disabled, true);
});
function harness() {
  const h = {scope: 'user:branch', snapshot: 'cart1', online: true, calls: [], texts: [], completed: [], payments: [], status: 'PENDING', configured: true, enabled: true};
  h.env = {
    scope: () => h.scope, snapshot: () => h.snapshot, online: () => h.online, uuid: () => 'stable-txn',
    open() {}, render: x => { h.rendered = x; h.texts.push(x.text); }, busy() {}, list: p => { h.listed = p; },
    setTimeout: fn => { h.tick = fn; return 1; }, clearTimeout() {}, completed: (...args) => h.completed.push(args),
    fetch: async (path, options) => {
      h.calls.push({path, options});
      if (h.intercept) { const result = await h.intercept(path, options); if (result) return result; }
      const now = Date.now();
      const payment = {id: 7, order_id: 'order1', status: h.status, amount: 1000, currency: 'MMK',
        created_at: new Date(now).toISOString(), expires_at: now + 900000, server_now: now};
      let data = path.endsWith('/config') ? {enabled: h.enabled, configured: h.configured, manual_allowed: h.enabled === false} :
        path.endsWith('/intents') && options.method === 'GET' ? {success: true, payments: h.payments} :
        path.endsWith('/complete') ? {success: true, transaction_id: 'sale1'} : {success: true, payment};
      return {ok: true, json: async () => data};
    }
  };
  h.checkout = createCheckout(h.env); return h;
}

test('automatic checks recover a missed callback and record its verified sale without another QR', async () => {
  const h = harness(); await h.checkout.loadConfig();
  await h.checkout.start({items: [1]});
  const realNow = Date.now;
  let now = realNow();
  Date.now = () => now;
  try {
    now += 16000;
    h.intercept = async path => path.endsWith('/refresh') ? {
      ok: true, json: async () => ({payment: {id: 7, status: 'SUCCESS', amount: 1000}})
    } : undefined;
    await h.tick();
    assert.equal(h.checkout.state().active, null);
    assert.equal(h.calls.filter(c => c.path.endsWith('/refresh')).length, 1);
    assert.equal(h.calls.filter(c => c.path.endsWith('/intents') && c.options.method === 'POST').length, 1);
    assert.equal(h.calls.filter(c => c.path.endsWith('/complete')).length, 1);
    assert.equal(h.completed.length, 1);
  } finally { Date.now = realNow; }
});

test('an older status GET cannot overwrite a verified payment returned by a manual refresh', async () => {
  const h = harness(); await h.checkout.loadConfig();
  await h.checkout.start({items: [1]});
  let finish;
  h.intercept = async path => path.endsWith('/intents/7')
    ? new Promise(resolve => { finish = resolve; }) : undefined;
  const polling = h.tick();
  h.status = 'SUCCESS';
  await h.checkout.action('refresh');
  finish({ok: true, json: async () => ({payment: {id: 7, status: 'PENDING'}})});
  await polling;
  assert.equal(h.checkout.state().active, null);
  assert.equal(h.completed.length, 1);
});

test('automatic provider checks are throttled and stop after close or a branch change', async () => {
  const h = harness(); await h.checkout.loadConfig();
  await h.checkout.start({items: [1]});
  const realNow = Date.now;
  let now = realNow();
  Date.now = () => now;
  try {
    now += 16000;
    await h.tick();
    assert.equal(h.calls.filter(c => c.path.endsWith('/refresh')).length, 1);
    for (let i = 0; i < 4; i++) { now += 3000; await h.tick(); }
    assert.equal(h.calls.filter(c => c.path.endsWith('/refresh')).length, 1);
    h.checkout.close();
    now += 16000;
    const count = h.calls.length;
    await h.tick();
    assert.equal(h.calls.length, count);
    h.scope = 'different-branch';
    await h.tick();
    assert.equal(h.calls.length, count);
  } finally { Date.now = realNow; }
});

test('in-flight GET cannot change closed checkout, and reopening continues automatic polling', async () => {
  const h = harness(); await h.checkout.loadConfig();
  await h.checkout.start({items: [1]});
  let finish;
  h.intercept = async path => path.endsWith('/intents/7')
    ? new Promise(resolve => { finish = resolve; }) : undefined;
  const polling = h.tick();
  h.checkout.close();
  finish({ok: true, json: async () => ({payment: {id: 7, status: 'SUCCESS'}})});
  await polling;
  assert.equal(h.checkout.state().active.status, 'PENDING');
  h.intercept = null; h.status = 'SUCCESS';
  h.checkout.preview();
  await h.tick();
  assert.equal(h.checkout.state().active, null);
  assert.equal(h.completed.length, 1);
});

test('automatic refresh failure backs off while a later webhook still enables completion', async () => {
  const h = harness(); await h.checkout.loadConfig();
  await h.checkout.start({items: [1]});
  const realNow = Date.now;
  let now = realNow();
  Date.now = () => now;
  try {
    h.intercept = async path => path.endsWith('/refresh') ? {
      ok: false, json: async () => ({message: 'Provider status unavailable'})
    } : undefined;
    now += 16000;
    await h.tick();
    assert.equal(h.checkout.state().busy, false);
    assert.equal(h.checkout.state().active.status, 'PENDING');
    now += 3000;
    h.status = 'SUCCESS';
    await h.tick();
    assert.equal(h.checkout.state().active, null);
    assert.doesNotMatch(h.rendered.text, /Provider status unavailable/);
    assert.equal(h.calls.filter(c => c.path.endsWith('/refresh')).length, 1);
    assert.equal(h.completed.length, 1);
  } finally { Date.now = realNow; }
});
test('uncertain create error remains visible after finally and status polling', async () => {
  const h = harness(); await h.checkout.loadConfig();
  h.status = 'CREATING';
  h.intercept = async (path, options) => options.method === 'POST' ? {
    ok: false, json: async () => ({success: false, message: 'Provider did not issue a QR.',
      payment: {id: 7, order_id: 'order1', status: 'CREATING'}})
  } : undefined;
  await h.checkout.start({items: [1]});
  assert.match(h.texts.at(-1), /Provider did not issue a QR/);
  await h.tick();
  assert.match(h.texts.at(-1), /Provider did not issue a QR/);
});

test('a rejected create without an order leaves a retryable non-busy error', async () => {
  const h = harness(); await h.checkout.loadConfig();
  h.intercept = async (path, options) => options.method === 'POST' ? {
    ok: false, json: async () => ({success: false, message: 'Checkout validation failed.'})
  } : undefined;
  await h.checkout.start({items: [1]});
  assert.equal(h.rendered.busy, false);
  assert.match(h.rendered.text, /Checkout validation failed/);
  assert.equal(h.checkout.state().active, null);
});

test('fail closed before runtime config and when misconfigured; explicit disabled is legacy', async () => {
  const h = harness(); assert.equal(h.checkout.mode(), 'loading');
  await h.checkout.start({items: [1]}); assert.equal(h.calls.length, 1); // config only
  h.configured = false; await h.checkout.loadConfig();
  assert.equal(h.checkout.mode(), 'blocked'); await h.checkout.start({});
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
  h.enabled = false; await h.checkout.loadConfig(); assert.equal(h.checkout.mode(), 'legacy');
});
test('create freezes full payload and transaction ID; polling only GET; complete uses no client payload', async () => {
  const h = harness(); await h.checkout.loadConfig();
  const sale = {items: [{product_id: 1}], payment_breakdown: {cash: 20, mobile_payment: 1000}, delivery: {enabled: true}};
  await h.checkout.start(sale); sale.items[0].product_id = 999;
  const create = h.calls.find(c => c.options.method === 'POST');
  assert.equal(create.options.headers['X-POS-Payment-Request'], '1');
  const frozen = JSON.parse(create.options.body); assert.equal(frozen.items[0].product_id, 1);
  assert.equal(frozen.transaction_id, 'stable-txn'); assert.deepEqual(frozen.delivery, {enabled: true});
  await h.tick(); assert.equal(h.calls.at(-1).options.method, 'GET');
  await h.checkout.action('complete'); assert.equal(h.completed.length, 0);
  h.snapshot = 'new-cart'; h.status = 'SUCCESS'; await h.tick(); await h.checkout.action('complete');
  assert.equal(h.calls.at(-1).options.body, undefined);
  assert.deepEqual(h.completed[0], [{success: true, transaction_id: 'sale1'}, false]);
});
test('pending paid intent resumes after reload, cannot create another payment', async () => {
  const h = harness(); await h.checkout.loadConfig(); h.payments = [{id: 8, status: 'SUCCESS'}];
  await h.checkout.start({items: [1]}); assert.deepEqual(h.listed, h.payments);
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
  h.status = 'SUCCESS'; h.checkout.resume(h.payments[0]); await new Promise(setImmediate);
  await h.checkout.action('complete'); assert.equal(h.completed[0][1], false);
});
test('duplicate creates blocked; late result ignored after branch changes', async () => {
  const h = harness(); await h.checkout.loadConfig(); let resolve;
  h.intercept = async (path, options) => {
    if (options.method === 'POST') return new Promise(r => { resolve = r; });
  };
  const first = h.checkout.start({items: [1]}); await new Promise(setImmediate);
  await h.checkout.start({items: [1]});
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 1);
  h.scope = 'another-branch'; resolve({ok: true, json: async () => ({success: true, payment: {id: 7}})});
  await first; assert.equal(h.checkout.state().active, null);
});
test('offline never posts; failed complete retains paid intent and reconciliation warning', async () => {
  const h = harness(); await h.checkout.loadConfig(); h.online = false;
  await h.checkout.start({items: [1]}); assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
  h.intercept = async path => { if (path.endsWith('/complete')) throw Error('inventory changed'); };
  h.online = true; h.status = 'SUCCESS'; await h.checkout.start({items: [1]});
  await h.checkout.action('complete'); assert.equal(h.checkout.state().active.id, 7);
  assert.match(h.texts.at(-1), /reconciliation, do not pay again/); assert.equal(h.completed.length, 0);
});
test('create timeout replay retains transaction and rejects changed cart; cancel failure retains intent', async () => {
  const h = harness(); await h.checkout.loadConfig();
  h.intercept = async (path, options) => { if (options.method === 'POST') throw Error('timeout'); };
  await h.checkout.start({items: [1]}); await h.checkout.start({items: [1]});
  const posts = h.calls.filter(c => c.options.method === 'POST'); assert.equal(posts.length, 2);
  assert.equal(posts[0].options.body, posts[1].options.body);
  h.snapshot = 'new-cart'; await h.checkout.start({items: [2]});
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 2);
  h.checkout.resume({id: 7, status: 'PENDING'}); await new Promise(setImmediate);
  await h.checkout.action('cancel'); assert.equal(h.checkout.state().active.id, 7);
});
for (const terminal of ['FAILED', 'EXPIRED', 'CANCELLED', 'REFUNDED']) {
  test(`persisted ${terminal} releases original quote and permits a new checkout`, async () => {
    const h = harness(); let sequence = 0;
    h.env.uuid = () => `transaction-${++sequence}`;
    await h.checkout.loadConfig(); await h.checkout.start({items: [1]});
    h.status = terminal; await h.tick();
    assert.equal(h.checkout.state().active, null);
    assert.equal(h.checkout.state().quote, null);
    if (terminal === 'REFUNDED') {
      h.checkout.preview(); assert.match(h.texts.at(-1), /REFUNDED.*reconciliation/);
    }
    h.status = 'PENDING'; h.snapshot = 'cart2'; await h.checkout.start({items: [2]});
    const creates = h.calls.filter(c => c.path.endsWith('/intents') && c.options.method === 'POST');
    assert.equal(creates.length, 2);
    assert.notEqual(JSON.parse(creates[0].options.body).transaction_id, JSON.parse(creates[1].options.body).transaction_id);
    assert.equal(h.completed.length, 0);
  });
}
test('uncertain 502 create retains returned intent for refresh without another create', async () => {
  const h = harness(); await h.checkout.loadConfig();
  h.intercept = async (path, options) => {
    if (path.endsWith('/intents') && options.method === 'POST') return {
      ok: false, json: async () => ({success: false, message: 'Creation uncertain', payment: {id: 55, status: 'CREATING'}})
    };
  };
  await h.checkout.start({items: [1]});
  assert.equal(h.checkout.state().active.id, 55);
  await h.checkout.start({items: [1]});
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 1);
  await h.checkout.action('refresh');
  assert.match(h.calls.at(-1).path, /intents\/55\/refresh$/);
});
test('late uncertain error payment is ignored after branch changes', async () => {
  const h = harness(); await h.checkout.loadConfig(); let resolve;
  h.intercept = async (path, options) => options.method === 'POST' ? new Promise(r => { resolve = r; }) : undefined;
  const pending = h.checkout.start({items: [1]}); await new Promise(setImmediate);
  h.scope = 'new-branch';
  resolve({ok: false, json: async () => ({success: false, payment: {id: 55, status: 'UNKNOWN'}})});
  await pending; assert.equal(h.checkout.state().active, null);
});
test('render carries the 15-minute deadline and caches the active order', async () => {
  const h = harness(); await h.checkout.loadConfig();
  const renders = []; let remembered = null;
  h.env.render = x => { renders.push(x); };
  h.env.remember = payment => { remembered = payment; };
  await h.checkout.start({items: [1]});
  const last = renders.at(-1);
  assert.ok(last.deadline > Date.now(), 'deadline must be in the future');
  assert.ok(last.deadline <= Date.now() + 900000 + 1000, 'deadline must be within 15 minutes');
  assert.equal(remembered.id, 7);
  assert.equal(remembered.expires_at, h.checkout.state().active.expires_at);
});

test('a reload restores the cached order and never posts a second create', async () => {
  const h = harness(); await h.checkout.loadConfig();
  h.env.restore = () => ({id: 7, order_id: 'order1'});
  await h.checkout.loadConfig();
  await new Promise(setImmediate);
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
  assert.equal(h.checkout.state().active.id, 7);
  assert.ok(h.calls.some(c => /\/intents\/7$/.test(c.path)));
});

test('checkout opened while config loads starts automatically once ready', async () => {
  const h = harness();
  h.env.checkoutReady = () => h.checkout.start({items: [1]});
  await h.checkout.start({items: [1]});
  await new Promise(setImmediate);
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 1);
  assert.equal(h.checkout.state().active.id, 7);
});

test('automatic checkout waits for cached-order recovery instead of creating again', async () => {
  const h = harness(); let finishRecovery;
  h.env.restore = () => ({id: 7});
  h.env.checkoutReady = () => h.checkout.start({items: [1]});
  h.intercept = async path => path.endsWith('/intents/7')
    ? new Promise(resolve => { finishRecovery = resolve; }) : undefined;
  const loading = h.checkout.loadConfig();
  await new Promise(setImmediate);
  await h.checkout.start({items: [1]});
  finishRecovery({ok: true, json: async () => ({payment: {id: 7, status: 'PENDING'}})});
  await loading;
  assert.equal(h.checkout.state().active.id, 7);
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
});

for (const abandon of ['close', 'branch switch']) {
  test(`no automatic create after ${abandon} while readiness is loading`, async () => {
    const h = harness(); let finishConfig;
    h.env.checkoutReady = () => h.checkout.start({items: [1]});
    h.intercept = async path => path.endsWith('/config')
      ? new Promise(resolve => { finishConfig = resolve; }) : undefined;
    await h.checkout.start({items: [1]});
    if (abandon === 'close') h.checkout.close(); else h.scope = 'other-branch';
    finishConfig({ok: true, json: async () => ({enabled: true, configured: true})});
    await new Promise(setImmediate);
    assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
  });
}

test('a cached order that is already terminal is dropped without a new create', async () => {
  const h = harness(); h.status = 'CANCELLED';
  h.env.restore = () => ({id: 7, order_id: 'order1'});
  let forgotten = false; h.env.forget = () => { forgotten = true; };
  await h.checkout.loadConfig();
  await new Promise(setImmediate);
  assert.equal(forgotten, true);
  assert.equal(h.checkout.state().active, null);
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
});

for (const manualAllowed of [true, false]) {
test(`disabled integration permits saved provider recovery (manual=${manualAllowed}) but never new provider creation`, async () => {
  const h = harness(); h.enabled = false; h.configured = false;
  h.intercept = async path => path.endsWith('/config') ? {
    ok: true, json: async () => ({enabled: false, configured: false, manual_allowed: manualAllowed})
  } : undefined;
  await h.checkout.loadConfig(); assert.equal(h.checkout.mode(), manualAllowed ? 'legacy' : 'blocked');
  h.payments = [{id: 7, status: 'PENDING'}];
  await h.checkout.list(); assert.deepEqual(h.listed, h.payments);
  h.checkout.preview(); h.checkout.resume(h.payments[0]); await new Promise(setImmediate);
  assert.equal(h.checkout.state().active.id, 7);
  await h.checkout.start({items: [99]});
  assert.equal(h.calls.filter(c => c.path.endsWith('/intents') && c.options.method === 'POST').length, 0);
  await h.checkout.action('refresh'); assert.match(h.calls.at(-1).path, /\/7\/refresh$/);
  h.status = 'SUCCESS'; await h.tick();
  await h.checkout.action('complete'); assert.equal(h.completed.length, 1);
  assert.equal(h.completed[0][1], false);
  assert.equal(h.calls.at(-1).options.body, undefined);
  h.status = 'PENDING'; h.checkout.resume({id: 7, status: 'PENDING'}); await new Promise(setImmediate);
  h.status = 'CANCELLED'; await h.checkout.action('cancel');
  assert.match(h.calls.at(-1).path, /\/7\/cancel$/);
  assert.equal(h.checkout.state().active, null);
});
}