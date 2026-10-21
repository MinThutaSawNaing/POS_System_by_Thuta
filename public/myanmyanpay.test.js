// Run: node --test public/myanmyanpay.test.js
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {createCheckout} = require('./myanmyanpay.js');
function harness() {
  const h = {scope: 'user:branch', snapshot: 'cart1', online: true, calls: [], texts: [], completed: [], payments: [], status: 'PENDING', configured: true, enabled: true};
  h.env = {
    scope: () => h.scope, snapshot: () => h.snapshot, online: () => h.online, uuid: () => 'stable-txn',
    open() {}, render: x => h.texts.push(x.text), busy() {}, list: p => { h.listed = p; },
    setTimeout: fn => { h.tick = fn; return 1; }, clearTimeout() {}, completed: (...args) => h.completed.push(args),
    fetch: async (path, options) => {
      h.calls.push({path, options});
      if (h.intercept) { const result = await h.intercept(path, options); if (result) return result; }
      const payment = {id: 7, order_id: 'order1', status: h.status, amount: 1000, currency: 'MMK'};
      let data = path.endsWith('/config') ? {enabled: h.enabled, configured: h.configured} :
        path.endsWith('/intents') && options.method === 'GET' ? {success: true, payments: h.payments} :
        path.endsWith('/complete') ? {success: true, transaction_id: 'sale1'} : {success: true, payment};
      return {ok: true, json: async () => data};
    }
  };
  h.checkout = createCheckout(h.env); return h;
}
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
  h.status = 'SUCCESS'; await h.tick(); h.snapshot = 'new-cart'; await h.checkout.action('complete');
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
  h.online = true; h.status = 'SUCCESS'; await h.checkout.start({items: [1]});
  h.intercept = async path => { if (path.endsWith('/complete')) throw Error('inventory changed'); };
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
test('disabled integration permits saved provider recovery but never new provider creation', async () => {
  const h = harness(); h.enabled = false; h.configured = false;
  await h.checkout.loadConfig(); assert.equal(h.checkout.mode(), 'legacy');
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