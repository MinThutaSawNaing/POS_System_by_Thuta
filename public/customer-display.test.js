/* Customer display contract test: run with `node public/customer-display.test.js`.
 *
 * Loads the real publisher and the real renderer, wires them through a fake
 * BroadcastChannel and a fake DOM, and asserts the states the cashier workflow
 * depends on. No browser and no server required.
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const outbox = [];
const intervals = [];

function classList() {
  const set = new Set();
  return {
    toggle(name, on) {
      if (on === undefined) set.has(name) ? set.delete(name) : set.add(name);
      else if (on) set.add(name);
      else set.delete(name);
    },
    add: (name) => set.add(name),
    remove: (name) => set.delete(name),
    contains: (name) => set.has(name),
  };
}

function element(id) {
  return {
    id,
    textContent: '',
    hidden: false,
    value: '',
    className: '',
    style: {},
    children: [],
    classList: classList(),
    _attrs: {},
    get src() { return this._attrs.src === undefined ? '' : this._attrs.src; },
    set src(value) { this._attrs.src = String(value); },
    setAttribute(key, value) { this._attrs[key] = String(value); },
    getAttribute(key) { return this._attrs[key] === undefined ? null : this._attrs[key]; },
    removeAttribute(key) { delete this._attrs[key]; },
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren() { this.children = []; },
    append(...nodes) { this.children.push(...nodes); },
  };
}

const nodes = {};
globalThis.document = {
  readyState: 'complete',
  getElementById: (id) => (nodes[id] = nodes[id] || element(id)),
  createElement: (tag) => element(tag),
  addEventListener: () => {},
};
globalThis.addEventListener = () => {};

// Deterministic clock so the countdown and the stale-publisher rule are testable.
let clock = 1000000;
Date.now = () => clock;

class FakeChannel {
  constructor(name) { this.name = name; FakeChannel.all.push(this); }
  postMessage(data) {
    outbox.push(data);
    FakeChannel.all
      .filter((channel) => channel !== this)
      .forEach((channel) => channel.onmessage && channel.onmessage({ data: JSON.parse(JSON.stringify(data)) }));
  }
  close() {}
}
FakeChannel.all = [];
globalThis.BroadcastChannel = FakeChannel;

globalThis.setInterval = (fn) => intervals.push(fn);
globalThis.clearInterval = () => {};
globalThis.setTimeout = () => 0;
globalThis.clearTimeout = () => {};
globalThis.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };

// Dashboard globals the publisher reads.
globalThis.formatCurrency = (value) => 'MMK ' + Number(value).toFixed(2);
globalThis.cart = [];
globalThis.getCurrentCartTotal = () => Number(String(nodes['total'].textContent).replace(/[^0-9.]/g, ''));

function setTotals(subtotal, tax, total) {
  document.getElementById('pos-delivery-fee-row').style = { display: 'none' };
  document.getElementById('subtotal').textContent = formatCurrency(subtotal);
  document.getElementById('tax').textContent = formatCurrency(tax);
  document.getElementById('total').textContent = formatCurrency(total);
}

const load = (file) => vm.runInThisContext(
  fs.readFileSync(path.join(__dirname, file), 'utf8'), { filename: file });

load('customer-display-publisher.js');
load('customer-display.js');

const snapshots = () => outbox.filter((message) => message.type === 'snapshot').map((message) => message.state);
const last = () => snapshots()[snapshots().length - 1];
const tick = () => intervals[intervals.length - 1]();
const report = {};

try {
  report.publisherReady = typeof CustomerDisplay === 'object';

  // 1. Live cart: names, quantities, prices and totals reach the screen.
  cart = [
    { name: 'Coffee', price: 2.5, quantity: 2, tax_rate: 0, unit: 'cup' },
    { name: 'Bread', price: 3, quantity: 1, tax_rate: 5 },
  ];
  setTotals(8, 0.15, 8.15);
  CustomerDisplay.publishCart();
  report.cart = last();
  report.cartSaleHidden = nodes['cd-sale'].hidden;
  report.cartTotalShown = nodes['cd-total'].textContent;
  report.cartItemsShown = nodes['cd-items'].children.length;

  // 2. The QR freezes the sale: amount and order come from the payment surface.
  document.getElementById('pos-mmqr-total').textContent = 'MMK 8.15';
  CustomerDisplay.publishPayment({
    mode: 'provider',
    payment: { id: 'intent-1', order_id: 'ORD-9', status: 'PENDING', amount: 8.15 },
    deadline: Date.now() + 60000,
    text: '',
    view: 'checkout',
    qrSrc: 'data:image/png;base64,AAA',
  });
  report.qr = last();
  report.qrPanelHidden = nodes['cd-qr-panel'].hidden;
  report.qrImage = nodes['cd-qr-image'].getAttribute('src');
  report.timerText = nodes['cd-timer'].textContent;

  // 3. A cart edit while the QR is live must not change what the customer pays.
  cart.push({ name: 'Tea', price: 1, quantity: 1, tax_rate: 0 });
  setTotals(9, 0.15, 9.15);
  CustomerDisplay.publishCart();
  report.afterEdit = last();

  // 4. Expiry is driven by the deadline, not by message frequency.
  report.expiredBefore = nodes['cd-qr-expired'].hidden;
  clock += 61000;
  tick();
  report.expiredAfter = nodes['cd-qr-expired'].hidden;
  report.expiredImageHidden = nodes['cd-qr-image'].hidden;

  // 5. Cashier tab closed: keep content, show the warning.
  clock += 11000;
  tick();
  report.bannerHidden = nodes['cd-banner'].hidden;
  report.heldQr = nodes['cd-qr-panel'].hidden;

  // 6. Logout / branch switch clears the screen.
  CustomerDisplay.reset();
  report.resetMessage = outbox[outbox.length - 1].type;
  report.resetIdleHidden = nodes['cd-idle'].hidden;
  report.resetSaleHidden = nodes['cd-sale'].hidden;

  // 7. Paid state carries the change for cash sales.
  cart = [{ name: 'Coffee', price: 2, quantity: 1, tax_rate: 0 }];
  setTotals(2, 0, 2);
  CustomerDisplay.publishCart();
  document.getElementById('cash-received').value = '5';
  CustomerDisplay.paid();
  report.paid = last();
  report.paidPanelHidden = nodes['cd-paid'].hidden;
  report.paidTotalShown = nodes['cd-paid-total'].textContent;
  report.paidChangeShown = nodes['cd-paid-change'].textContent;

  console.log(JSON.stringify(report));
} catch (error) {
  console.error(error);
  process.exit(1);
}
