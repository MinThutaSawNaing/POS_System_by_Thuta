"""Exercise real dashboard handlers with asynchronous Bootstrap modal hiding."""

import pytest

from test_returns_dashboard_javascript import DASHBOARD, NODE, _function, _run_node_script

pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is required")


@pytest.mark.parametrize("source_id", ["customerDetailsModal", "debtDetailsModal", None])
def test_payment_waits_for_existing_dialog_to_close(source_id):
    out = _run_handler("showMakePaymentModal", "showMakePaymentModal(7)", source_id)
    assert out["before"] == ([source_id] if source_id else ["makePaymentModal"])
    assert out["after"] == ["makePaymentModal"]
    assert out["overlaps"] == []
    assert out["fields"]["payment-debt-id"] == 7
    assert out["fields"]["payment-amount"] == "25.00"


def test_bulk_due_date_waits_for_hide_transition():
    out = _run_handler("bulkUpdateDueDate", "bulkUpdateDueDate()", "bulkActionsModal")
    assert out["before"] == ["bulkActionsModal"]
    assert out["after"] == ["updateDueDateModal"]
    assert out["overlaps"] == []


def test_payment_fetch_failure_keeps_customer_details_open():
    out = _run_handler("showMakePaymentModal", "showMakePaymentModal(7)",
                       "customerDetailsModal", fail_fetch=True)
    assert out["after"] == ["customerDetailsModal"]
    assert out["toasts"] == ["Failed to load debt details"]


def _run_handler(name, invocation, source_id, fail_fetch=False):
    import json

    source = DASHBOARD.read_text(encoding="utf-8")
    helpers = _function(source, name)
    if "function showModalAfterHiding" in source:
        helpers = _function(source, "showModalAfterHiding") + "\n" + helpers
    script = """
const elements = new Map(), instances = new Map(), fields = {}, pending = [];
const overlaps = [], toasts = [];
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    id, visible: false, listeners: {},
    classList: { contains: (name) => name === 'show' && element(id).visible },
    get value() { return fields[id]; }, set value(v) { fields[id] = v; },
    addEventListener(name, callback, options) {
      (this.listeners[name] ||= []).push({ callback, once: options?.once });
    },
    removeEventListener(name, callback) {
      this.listeners[name] = (this.listeners[name] || []).filter(x => x.callback !== callback);
    },
    emit(name) {
      const listeners = this.listeners[name] || [];
      this.listeners[name] = listeners.filter(x => !x.once);
      listeners.forEach(x => x.callback());
    }
  });
  return elements.get(id);
}
function visible() { return [...elements.values()].filter(e => e.visible).map(e => e.id); }
globalThis.document = {
  getElementById: element,
  querySelectorAll: () => [...elements.values()].filter(e => e.visible),
};
class Modal {
  constructor(el) { this.el = el; instances.set(el.id, this); }
  static getInstance(el) { return instances.get(el.id); }
  static getOrCreateInstance(el) { return this.getInstance(el) || new Modal(el); }
  show() {
    this.el.visible = true;
    if (visible().length > 1) overlaps.push(visible());
  }
  hide() { pending.push(() => { this.el.visible = false; this.el.emit('hidden.bs.modal'); }); }
}
globalThis.bootstrap = { Modal };
// Modal transition scenarios assume an authorized manager, not a permission denial.
function requireCapability(capability) { return capability === 'debts'; }
function showToast(message) { toasts.push(message); }
console.error = () => {};
globalThis.fetch = () => __FAIL__ ? Promise.reject(new Error('offline')) :
  Promise.resolve({ json: () => Promise.resolve({
    id: 7, customer_name: 'Test customer', amount: 50, balance: 25,
  }) });
__HELPERS__
(async () => {
  const sourceId = __SOURCE__;
  if (sourceId) Modal.getOrCreateInstance(element(sourceId)).show();
  __INVOKE__;
  await new Promise(resolve => setImmediate(resolve));
  const before = visible();
  while (pending.length) pending.shift()();
  console.log(JSON.stringify({ before, after: visible(), overlaps, fields, toasts }));
})();
"""
    return _run_node_script(script.replace("__HELPERS__", helpers)
                            .replace("__SOURCE__", json.dumps(source_id))
                            .replace("__FAIL__", json.dumps(fail_fetch))
                            .replace("__INVOKE__", invocation))