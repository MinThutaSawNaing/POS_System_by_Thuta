"""Execute dashboard record-management handlers in Node, without a browser."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from test_offline_sync_javascript import _function

SOURCE = (Path(__file__).parent / "templates/dashboard.html").read_text(encoding="utf-8")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js required")


def run_js(body):
    helpers = "\n".join(_function(SOURCE, name) for name in (
        "savePurchaseOrderEdit", "showRecordDeletion", "confirmRecordDeletion"))
    script = r'''
let allowed = true, recordManagementSaving = false;
let editingPurchaseOrder = {id: 7, identity: "branch-a", locked: false};
let deletingRecord = null;
const fields = {};
const field = id => fields[id] ||= {value: "", disabled: false, textContent: ""};
field("edit-po-supplier").value = "4";
field("edit-po-notes").value = " note ";
field("edit-po-expected").value = "2026-12-01";
const line = {querySelector: s => ({value: s.includes("product") ? "31" : s.includes("quantity") ? "2" : "4.25"})};
globalThis.document = {getElementById: id => id === "edit-po-form" ? {reportValidity: () => true} : field(id), querySelectorAll: () => [line]};
let hidden = 0, shown = 0;
globalThis.bootstrap = {Modal: {getInstance: () => ({hide: () => hidden++}), getOrCreateInstance: () => ({show: () => shown++})}};
const requests = [], toasts = [], refreshes = [];
function hasManagerOrBossAccess() {return allowed;}
function inventoryIdentity() {return "branch-a";}
function showToast(message, type) {toasts.push({message, type});}
function refreshManagedRecords(kind) {refreshes.push(kind);}
let response = {ok: true, json: async () => ({success: true})};
globalThis.fetch = async (url, options) => {requests.push({url, ...options}); return response;};
'''
    result = subprocess.run([NODE, "-e", script + helpers + "\n(async () => {" + body + "})().catch(e => {console.error(e);process.exit(1)});"], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_cashier_cannot_open_or_submit_record_controls():
    out = run_js('''allowed = false;
showRecordDeletion("sales", "abc"); await confirmRecordDeletion(); await savePurchaseOrderEdit();
console.log(JSON.stringify({requests, shown}));''')
    assert out == {"requests": [], "shown": 0}


def test_edit_uses_product_id_not_purchase_order_item_id():
    out = run_js('''await savePurchaseOrderEdit(); console.log(JSON.stringify({requests, refreshes, hidden, button: field("edit-po-save")}));''')
    request = out["requests"][0]
    assert request["method"] == "PUT"
    assert json.loads(request["body"]) == {"supplier_id": 4, "notes": "note", "expected_delivery_date": "2026-12-01", "items": [{"product_id": 31, "quantity": 2, "unit_cost": 4.25}]}
    assert out["refreshes"] == ["purchase_orders"]
    assert out["hidden"] == 1
    assert not out["button"]["disabled"]


def test_received_order_only_sends_metadata():
    out = run_js('''editingPurchaseOrder.locked = true; await savePurchaseOrderEdit(); console.log(JSON.stringify(requests));''')
    assert json.loads(out[0]["body"]) == {"notes": "note", "expected_delivery_date": "2026-12-01"}


def test_cancelled_order_omits_lines_but_allows_supplier_and_metadata():
    out = run_js('''editingPurchaseOrder.linesLocked = true; await savePurchaseOrderEdit(); console.log(JSON.stringify({requests, refreshes}));''')
    assert json.loads(out["requests"][0]["body"]) == {
        "notes": "note", "expected_delivery_date": "2026-12-01", "supplier_id": 4,
    }
    assert out["refreshes"] == ["purchase_orders"]


def test_editor_uses_explicit_placeholders_and_resets_supplier_search():
    editor = _function(SOURCE, "editPurchaseOrder")
    line = _function(SOURCE, "addPurchaseOrderEditLine")
    assert 'new Option("Select supplier", "")' in editor
    assert 'new Option("Select product", "")' in line
    assert 'supplierSearch.value = ""' in editor
    assert 'supplier.remoteLookup?.load(po.supplier_id)' in editor
    assert 'po.status === "cancelled"' in editor
    assert 'field.disabled = editingPurchaseOrder.linesLocked' in line


def test_sale_delete_confirms_and_encodes_identifier():
    out = run_js('''showRecordDeletion("sales", "x/'<script>"); await confirmRecordDeletion(); console.log(JSON.stringify({requests, refreshes, label: field("delete-record-label").textContent, hidden}));''')
    assert out["requests"][0]["url"] == "/api/sales/x%2F'%3Cscript%3E"
    assert out["requests"][0]["method"] == "DELETE"
    assert json.loads(out["requests"][0]["body"]) == {"confirm": True}
    assert out["label"] == "Sale: x/'<script>"
    assert out["refreshes"] == ["sales"]


@pytest.mark.parametrize("operation", ["delete", "edit"])
def test_conflict_retains_modal_restores_button_and_does_not_refresh(operation):
    action = 'showRecordDeletion("sales", "linked"); await confirmRecordDeletion();' if operation == "delete" else 'await savePurchaseOrderEdit();'
    out = run_js('''response = {ok: false, status: 409, json: async () => ({message: "Linked record cannot be deleted"})};''' + action + '''console.log(JSON.stringify({hidden, refreshes, toasts, saving: recordManagementSaving}));''')
    assert out["hidden"] == 0
    assert out["refreshes"] == []
    assert out["saving"] is False
    assert out["toasts"] == [{"message": "Linked record cannot be deleted", "type": "error"}]


def test_branch_change_blocks_stale_mutation():
    out = run_js('''editingPurchaseOrder.identity = "old"; await savePurchaseOrderEdit(); showRecordDeletion("sales", "a"); deletingRecord.identity = "old"; await confirmRecordDeletion(); console.log(JSON.stringify(requests));''')
    assert out == []


def test_network_failure_and_duplicate_submission_restore_controls():
    out = run_js('''let release;
globalThis.fetch = async (url, options) => {requests.push({url, ...options}); return new Promise((resolve, reject) => {release = reject;});};
showRecordDeletion("purchase_orders", 7);
const pending = confirmRecordDeletion(); await confirmRecordDeletion();
release(new Error("Network unavailable")); await pending;
console.log(JSON.stringify({requests, refreshes, hidden, button: field("delete-record-confirm"), saving: recordManagementSaving, toasts}));''')
    assert len(out["requests"]) == 1
    assert out["hidden"] == 0
    assert out["refreshes"] == []
    assert not out["button"]["disabled"]
    assert out["saving"] is False
    assert out["toasts"][0]["message"] == "Network unavailable"


def test_ai_refresh_is_manager_guarded_and_only_matches_record_tools():
    start = SOURCE.index('window.addEventListener("ai-records-changed"')
    listener = SOURCE[start:SOURCE.index("\n      });", start) + len("\n      });")]
    helpers = _function(SOURCE, "refreshManagedRecords")
    script = '''let handler, allowed = true; const calls = [];
const window = {addEventListener: (name, callback) => {handler = callback;}};
function hasManagerOrBossAccess() {return allowed;}
function invalidateCacheGroups(groups) {calls.push(groups);}
function invalidateSalesReport() {calls.push("report");}
function loadPurchaseOrders() {calls.push("purchases");}
function loadSales() {calls.push("sales");}
function loadDashboardStats() {calls.push("dashboard");}
''' + helpers + listener + '''
handler({detail: {tools: ["get_sales"]}});
allowed = false; handler({detail: {tools: ["delete_sale_history"]}});
const ignored = calls.length;
allowed = true; handler({detail: {tools: ["delete_sale_history", "update_purchase_order"]}});
console.log(JSON.stringify({ignored, calls}));'''
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["ignored"] == 0
    assert {"purchases", "sales", "reports", "dashboard"} <= set(out["calls"][0])
    assert out["calls"][1:] == ["report", "purchases", "sales", "dashboard"]


def test_controls_use_event_handlers_not_inline_transaction_ids():
    sales = _function(SOURCE, "loadSales")
    assert 'hasManagerOrBossAccess()' in sales
    assert 'data-sale-action="delete"' in sales
    assert "onclick=" not in sales
    assert "escapeHtml(sale.transaction_id)" in sales
    assert 'window.addEventListener("ai-records-changed"' in SOURCE
    assert 'It does not reverse stock movements or payments' in SOURCE