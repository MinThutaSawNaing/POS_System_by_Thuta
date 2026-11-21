"""Create/Cancel PO modals must never leave their submit button spinning.

The markup for both modals is static, so a button left in its "Creating..."
state survived tab switches and only a full page reload cleared it.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_offline_sync_javascript import _function


SOURCE = (Path(__file__).parent / "templates" / "dashboard.html").read_text(encoding="utf-8")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js required")

def test_invoice_form_renders_conflict_and_can_retry():
    script = _function(SOURCE, "openPurchaseDocument") + """
const assert = require('node:assert/strict');
let purchaseDocumentRequest = 0;
const button = {disabled: false};
const printButton = {};
const error = {textContent: ''};
let submit, postCount = 0, refreshes = 0;
const form = {
  addEventListener(_, callback) { submit = callback; },
  querySelector(selector) { return selector === 'button' ? button : error; },
};
const content = {querySelector: selector => selector === '[data-print-po]' ? printButton : form};
const title = {};
const document = {getElementById(id) { return id.endsWith('content') ? content : title; }};
const bootstrap = {Modal: {getOrCreateInstance: () => ({show() {}})}};
const inventoryIdentity = () => 'manager/branch';
const escapeHtml = (value) => String(value).replaceAll('<', '&lt;');
const formatCurrency = String;
const loadPurchaseOrders = () => { refreshes++; };
const showToast = () => {};
let printed;
const printPurchaseOrderDirect = id => { printed = id; };
const FormData = class { [Symbol.iterator]() { return Object.entries({supplier_reference: '<REF>', invoice_date: '2026-11-18', total_amount: '30.00'})[Symbol.iterator](); } };
const invoice = {supplier_reference: '<REF>', invoice_date: '2026-11-18', total_amount: '30.00',
  currency: 'USD', po_number: 'PO-1', supplier_name: 'Supplier', items: []};
const fetch = async (url, options) => {
  assert.equal(url, '/api/purchase_orders/1/invoice');
  if (!options) return {ok: true, json: async () => ({invoice})};
  assert.equal(JSON.parse(options.body).total_amount, '30.00');
  return ++postCount === 1 ? {ok: false, json: async () => ({message: 'Total does not match'})}
    : {ok: true, json: async () => ({invoice})};
};
(async () => {
  await openPurchaseDocument({id: 1, po_number: 'PO-1', supplier_name: 'Supplier', total_amount: 30}, 'invoice');
  assert.equal(title.textContent, 'Create Purchase Invoice');
  await submit({preventDefault() {}});
  assert.equal(error.textContent, 'Total does not match');
  assert.equal(button.disabled, false);
  assert.doesNotMatch(content.innerHTML, /data-print-po/);
  await submit({preventDefault() {}});
  await new Promise(setImmediate);
  assert.equal(title.textContent, 'Purchase Invoice');
  assert.match(content.innerHTML, /&lt;REF>/);
  assert.equal(refreshes, 1);
  printButton.onclick();
  assert.equal(printed, 1);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([NODE, "-"], input=script, text=True, encoding="utf-8", capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_po_details_print_waits_for_the_current_invoiced_order():
    script = _function(SOURCE, 'viewPurchaseOrderDetails') + """
const assert = require('node:assert/strict');
let purchaseOrderDetailsRequest = 0;
let printed;
const printPurchaseOrderDirect = id => { printed = id; };
const elements = {};
const document = {getElementById: id => elements[id] ||= {}};
const bootstrap = {Modal: {getOrCreateInstance: () => ({show() {}})}};
const inventoryIdentity = () => 'manager/branch';
const escapeHtml = String, formatCurrency = String;
const pending = {};
const fetch = url => new Promise(resolve => { pending[url.split('/').pop()] = resolve; });
const finish = (id, invoice) => pending[id]({ok: true, json: async () => ({
  id, status: 'received', created_at: '2026-11-18', items: [], invoice,
})});
(async () => {
  const print = document.getElementById('po-print-btn');
  const note = document.getElementById('po-print-note');
  const old = viewPurchaseOrderDetails(1);
  assert.equal(print.hidden, true);
  const fresh = viewPurchaseOrderDetails(2);
  finish(2, null);
  await fresh;
  assert.equal(print.hidden, true);
  assert.equal(note.hidden, false);
  finish(1, {supplier_reference: 'INV-1'});
  await old;
  assert.equal(print.hidden, true);
  const invoiced = viewPurchaseOrderDetails(3);
  finish(3, {supplier_reference: 'INV-3'});
  await invoiced;
  assert.equal(print.hidden, false);
  assert.equal(note.hidden, true);
  print.onclick();
  assert.equal(printed, 3);
  viewPurchaseOrderDetails(4);
  assert.equal(print.hidden, true);
  assert.equal(print.onclick, null);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([NODE, '-'], input=script, text=True, encoding='utf-8', capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


HARNESS = """
function makeButton(html) {
  return { disabled: false, innerHTML: html, tagName: "BUTTON" };
}
const saveButton = makeButton('<i class="bi bi-check-lg me-1"></i>Create PO');
const cancelButton = makeButton('<i class="bi bi-x-lg me-1"></i>Cancel PO');
const supplier = { value: "7" };
const notes = { value: "" };
const expected = { value: "" };
const field = (value) => ({ value: value });
const itemRow = {
  querySelector: (selector) => selector === ".po-item-product" ? field("3")
    : selector === ".po-item-qty" ? field("2")
    : selector === ".po-item-cost" ? field("4.50") : field(""),
};
const cancelled = { hidden: false };
const toasts = [];
globalThis.document = {
  getElementById: (id) => id === "po-supplier-id" ? supplier
    : id === "po-notes" ? notes
    : id === "po-expected-delivery" ? expected
    : id === "addPurchaseOrderModal" ? { id: id }
    : id === "cancelPurchaseOrderModal" ? { id: id }
    : null,
  querySelector: (selector) => selector === "#addPurchaseOrderModal .btn-primary" ? saveButton
    : selector === "#cancelPurchaseOrderModal .btn-danger" ? cancelButton
    : null,
  querySelectorAll: () => [itemRow],
};
globalThis.bootstrap = { Modal: {
  getInstance: () => ({ hide() { cancelled.hidden = true; } }),
  getOrCreateInstance: () => ({ hide() { cancelled.hidden = true; } }),
} };
globalThis.window = globalThis;
let purchaseOrderSaving = false;
let purchaseOrderSaveButtonHtml = null;
let cancelingPurchaseOrder = false;
let cancelPurchaseOrderButtonHtml = null;
let cancelPOId = null;
function showToast(message, type) { toasts.push({ message, type }); }
let reloads = 0;
function loadPurchaseOrders() { reloads += 1; }
let requestCount = 0;
let respond = async () => ({ ok: true, status: 201, json: async () => ({ success: true }) });
globalThis.fetch = async (url, options) => {
  requestCount += 1;
  return respond(url, options);
};
"""


def run_po_js(body, functions):
    helpers = "\n".join(_function(SOURCE, name) for name in functions)
    script = HARNESS + helpers + "\n(async () => {\n" + body + "\n})().catch(e => { console.error(e); process.exit(1); });"
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=False, encoding="utf-8")
    if result.returncode != 0:
        raise AssertionError(f"node failed: {result.stderr or result.stdout}")
    return json.loads(result.stdout)


CREATE_FUNCTIONS = ("toCents", "centsToNumber", "setPurchaseOrderSaving", "savePurchaseOrder")



def test_successful_create_restores_the_button_and_closes_the_modal():
    out = run_po_js("""
savePurchaseOrder();
await new Promise(resolve => setTimeout(resolve, 0));
console.log(JSON.stringify({
  disabled: saveButton.disabled,
  html: saveButton.innerHTML,
  hidden: cancelled.hidden,
  reloads,
  requests: requestCount,
  toasts,
}));
""", CREATE_FUNCTIONS)

    assert out["requests"] == 1
    assert out["disabled"] is False, "the Create PO button must not stay disabled"
    assert "Create PO" in out["html"]
    assert "Creating" not in out["html"], "the spinner label must be cleared"
    assert out["hidden"] is True
    assert out["reloads"] == 1
    assert out["toasts"][0]["type"] == "success"


def test_rejected_create_restores_the_button_and_keeps_the_modal_open():
    out = run_po_js("""
respond = async () => ({ ok: false, status: 400, json: async () => ({ success: false, message: "Supplier inactive" }) });
savePurchaseOrder();
await new Promise(resolve => setTimeout(resolve, 0));
console.log(JSON.stringify({
  disabled: saveButton.disabled,
  html: saveButton.innerHTML,
  hidden: cancelled.hidden,
  toasts,
}));
""", CREATE_FUNCTIONS)

    assert out["disabled"] is False
    assert "Creating" not in out["html"]
    assert out["hidden"] is False
    assert out["toasts"] == [{"message": "Supplier inactive", "type": "error"}]


def test_network_failure_restores_the_button():
    out = run_po_js("""
respond = async () => { throw new Error("offline"); };
globalThis.console.error = () => {};
savePurchaseOrder();
await new Promise(resolve => setTimeout(resolve, 0));
console.log(JSON.stringify({ disabled: saveButton.disabled, html: saveButton.innerHTML, toasts }));
""", CREATE_FUNCTIONS)

    assert out["disabled"] is False
    assert "Create PO" in out["html"]
    assert out["toasts"][0]["type"] == "error"



def test_double_tap_creates_only_one_purchase_order():
    out = run_po_js("""
let release;
respond = () => new Promise(resolve => { release = () => resolve({ ok: true, status: 201, json: async () => ({ success: true }) }); });
savePurchaseOrder();
savePurchaseOrder();
savePurchaseOrder();
await new Promise(resolve => setTimeout(resolve, 0));
release();
await new Promise(resolve => setTimeout(resolve, 0));
console.log(JSON.stringify({ requests: requestCount, reloads, disabled: saveButton.disabled }));
""", CREATE_FUNCTIONS)

    assert out["requests"] == 1, "a busy Create PO button must ignore extra taps"
    assert out["reloads"] == 1
    assert out["disabled"] is False


def test_reopening_the_modal_clears_a_stale_spinner():
    """The exact field report: a spinner left from an earlier save kept spinning
    after returning to the Purchase Orders tab until the page was reloaded."""
    out = run_po_js("""
setPurchaseOrderSaving(true);
const stuck = { disabled: saveButton.disabled, html: saveButton.innerHTML };
setPurchaseOrderSaving(false);
console.log(JSON.stringify({ stuck, cleared: { disabled: saveButton.disabled, html: saveButton.innerHTML } }));
""", CREATE_FUNCTIONS)

    assert out["stuck"]["disabled"] is True
    assert "Creating" in out["stuck"]["html"]
    assert out["cleared"]["disabled"] is False
    assert "Create PO" in out["cleared"]["html"]


CANCEL_FUNCTIONS = ("setCancelPurchaseOrderSaving", "confirmCancelPurchaseOrder")


def test_cancel_success_restores_the_confirm_button():
    out = run_po_js("""
cancelPOId = 12;
const original = document.getElementById;
document.getElementById = (id) => id === "cancel-po-reason" ? { value: "customer changed mind" } : original(id);
confirmCancelPurchaseOrder();
await new Promise(resolve => setTimeout(resolve, 0));
console.log(JSON.stringify({
  disabled: cancelButton.disabled,
  html: cancelButton.innerHTML,
  hidden: cancelled.hidden,
  reloads,
  toasts,
}));
""", CANCEL_FUNCTIONS)

    assert out["disabled"] is False
    assert "Cancelling" not in out["html"]
    assert out["hidden"] is True
    assert out["reloads"] == 1
    assert out["toasts"][0]["type"] == "success"
