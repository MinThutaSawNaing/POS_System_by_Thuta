"""Run the local payment state-machine regressions with the normal test runner."""
from pathlib import Path
import shutil
import subprocess

import pytest

from test_returns_dashboard_javascript import DASHBOARD, _function, _run_node_script


def test_myanmyanpay_checkout_state_machine():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for JavaScript runtime tests')
    root = Path(__file__).resolve().parent
    result = subprocess.run([node, '--test', str(root / 'public' / 'myanmyanpay.test.js')],
                            cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + '\n' + result.stderr


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js is required')
def test_opening_mmqr_automatically_starts_validated_checkout():
    source = DASHBOARD.read_text(encoding='utf-8')
    script = """
const assert = require('node:assert/strict');
const fields = {'payment-method': 'mobile_payment', 'pos-customer-select': '',
  'split-method-one': 'cash', 'split-method-two': 'mobile_payment',
  'split-amount-one': '200', 'split-amount-two': '800'};
const document = {getElementById: id => ({value: fields[id], checked: false})};
let cart = [{product_id: 5, price: 1000, quantity: 1, tax_rate: 0}];
let isSaleProcessing = false, dashboardSessionStale = false;
let dashboardBranchStale = false, branchSwitchInFlight = false;
const starts = [], notices = [];
let previews = 0, active = null, mode = 'provider';
const MyanMyanPayCheckout = {mode: () => mode, state: () => ({active}),
  start: sale => starts.push(sale), preview: () => { previews++; }};
function showToast(text) { notices.push(text); }
function t(text) { return text; }
function splitUsesMobilePayment() { return true; }
function toCents(value) { return Math.round(Number(value) * 100); }
function centsToNumber(value) { return value / 100; }
function getCurrentCartTotal() { return 1000; }
__HELPERS__
showMMQRPaymentModal();
assert.equal(starts.length, 1, 'opening MMQR must create without another button');
assert.equal(starts[0].items[0].product_id, 5);
fields['payment-method'] = 'split_payment';
showMMQRPaymentModal();
assert.deepEqual(starts[1].payment_breakdown, {cash: 200, mobile_payment: 800});
fields['split-amount-two'] = '700';
showMMQRPaymentModal();
assert.equal(starts.length, 2, 'invalid split must not generate a QR');
assert.equal(notices.length, 1);
active = {id: 7, status: 'PENDING'};
showMMQRPaymentModal();
assert.equal(starts.length, 2, 'reopening must retain the original order');
assert.equal(previews, 1);
console.log(JSON.stringify({ok: true}));
"""
    helpers = '\n'.join(_function(source, name) for name in (
        'showMMQRPaymentModal', 'completeSale'))
    assert _run_node_script(script.replace('__HELPERS__', helpers)) == {'ok': True}


def test_missing_checkout_script_never_calls_manual_sale_confirmation():
    source = DASHBOARD.read_text(encoding='utf-8')
    helpers = '\n'.join(_function(source, name) for name in (
        'showMMQRPaymentModal', 'confirmMMQRPayment', 'completeSale'))
    script = """
const assert = require('node:assert/strict');
let cart = [{product_id: 5, price: 1000, quantity: 1}];
let isSaleProcessing = false, dashboardSessionStale = false;
let dashboardBranchStale = false, branchSwitchInFlight = false;
const notices = [];
function showToast(text) { notices.push(text); }
function requireCapability() { return true; }
function splitUsesMobilePayment() { return true; }
const fields = {'payment-method': 'mobile_payment', 'pos-customer-select': ''};
const document = {getElementById: id => ({value: fields[id]})};
function fetch() { throw new Error('Must not submit a sale without checkout verification'); }
__HELPERS__
showMMQRPaymentModal();
confirmMMQRPayment();
completeSale(true);
fields['payment-method'] = 'split_payment';
completeSale(true);
assert.equal(notices.length, 4);
assert.ok(notices.every(text => /verification is unavailable/.test(text)));
assert.equal(cart.length, 1);
console.log(JSON.stringify({ok: true}));
"""
    assert _run_node_script(script.replace('__HELPERS__', helpers)) == {'ok': True}


def test_sale_success_notice_is_text_only_and_resets_between_payments():
    source = DASHBOARD.read_text(encoding='utf-8')
    script = """
const assert = require('node:assert/strict');
const elements = new Map();
const document = {getElementById(id) {
  if (!elements.has(id)) elements.set(id, {textContent: '', style: {},
    classList: {toggle(name, hidden) { this.hidden = hidden; }}});
  return elements.get(id);
}};
function showModalAfterHiding(id) { assert.equal(id, 'saleSuccessModal'); }
let currentSaleTransactionId;
__HELPERS__
showSaleSuccessModal('manual', null, false, 'Manual — not verified by MyanMyanPay');
const notice = document.getElementById('salePaymentNotice');
assert.match(notice.textContent, /not verified/);
assert.equal(notice.classList.hidden, false);
showSaleSuccessModal('cash', null, false);
assert.equal(notice.textContent, '');
assert.equal(notice.classList.hidden, true);
console.log(JSON.stringify({ok: true}));
"""
    assert _run_node_script(script.replace('__HELPERS__', _function(source, 'showSaleSuccessModal'))) == {'ok': True}