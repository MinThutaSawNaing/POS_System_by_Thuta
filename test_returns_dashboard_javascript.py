"""Behavioural tests for Returns & Exchanges and debt-payment dashboard flows."""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


DASHBOARD = Path(__file__).parent / "templates" / "dashboard.html"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is required")


def _run_node_script(script):
    handle, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(script)
        result = subprocess.run(
            [NODE, path], check=False, capture_output=True, text=True, encoding="utf-8"
        )
        if result.returncode:
            raise AssertionError(f"Node harness failed:\n{result.stderr}")
        return json.loads(result.stdout)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _function(source, name):
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", source)
    assert match, f"function {name} not found"
    depth = 0
    for index in range(match.end() - 1, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[match.start():index + 1]
    raise AssertionError(f"function {name} has unbalanced body")


HARNESS = """
const fields = __FIELDS__;
const opened = [];
const toasts = [];
const sectionsShown = [];
const invalidated = [];
const loads = [];
const pageState = { returns: 1 };
const pageSize = { returns: 10 };
const CACHE_USER_ID = 7, CACHE_USER_ROLE = 'manager';
const currentBranch = {id: 3};
let dashboardSessionStale = false;
globalThis.window = {
  location: {href: 'https://pos.test/', origin: 'https://pos.test'},
  open: (url, target) => {
    opened.push({ url, target });
    return { closed: false, opener: null, location: { replace: (u) => opened.push({ replaced: u }) } };
  },
};
globalThis.document = {
  getElementById: (id) => {
    if (!(id in fields)) fields[id] = "";
    return { get value() { return fields[id]; }, set value(v) { fields[id] = v; } };
  },
};
function showToast(message, kind) { toasts.push({ message, kind }); }
function invalidateCacheGroups(groups) { invalidated.push(groups); }
function loadReturns() { loads.push(true); }
function showSection(id) { sectionsShown.push(id); }
function loadUrlInPrintFrame(url) { opened.push({ printFrame: dashboardIdentityUrl(url) }); return true; }
__HELPERS__

const today = yangonToday();
const results = {
  exportPdf: exportReturns("pdf"),
  exportExcel: exportReturns("xlsx"),
  exportFallback: exportReturns("csv"),
  receipt: openReturnExchangeReceipt("wf-1"),
};
setReturnRangePreset("today");
const todayStart = fields["returns-start-filter"];
const todayEnd = fields["returns-end-filter"];
setReturnRangePreset("all");
const allStart = fields["returns-start-filter"];
const allEnd = fields["returns-end-filter"];
showReturnsForTransaction("txn-route-1");
console.log(JSON.stringify({
  opened, toasts, today, results,
  todayStart, todayEnd, allStart, allEnd,
  routedSearch: fields["returns-search"],
  sectionsShown, invalidated,
}));
"""


def _run(field_values=None):
    source = DASHBOARD.read_text(encoding="utf-8")
    helpers = "\n".join(
        _function(source, name)
        for name in (
            "yangonToday", "returnsFilterParams", "applyReturnFilters",
            "setReturnRangePreset", "openReturnExchangeReceipt", "exportReturns",
            "showReturnsForTransaction", "openReportDownload", "dashboardIdentityUrl",
        )
    )
    script = HARNESS.replace("__FIELDS__", json.dumps(field_values or {}))
    script = script.replace("__HELPERS__", helpers)
    return _run_node_script(script)


def test_section_nav_and_cache_are_wired():
    source = DASHBOARD.read_text(encoding="utf-8")
    assert 'id="returns-section"' in source
    assert 'id="returns-table"' in source
    assert "onclick=\"showSection('returns')\"" in source
    assert "returns: () => loadReturns()," in source
    assert 'returns: ["returns"],' in source
    assert '["/api/returns_exchanges", "returns"]' in source
    assert '"returns-section h2": "returns",' in source
    assert 'returns: "Returns",' in source
    # The Sales History row routes to the new tab for a transaction.
    assert "showReturnsForTransaction(" in source


def test_export_buttons_use_the_shared_report_component():
    """The Returns tab downloads must be the same button component the Warehouse
    and Purchase & Receiving tabs use (danger PDF + success Excel in a btn-group)."""
    source = DASHBOARD.read_text(encoding="utf-8")
    section = source[source.index('id="returns-section"'):source.index('id="reports-section"')]
    assert ('<div class="btn-group" role="group" '
            'aria-label="Download returns and exchanges report">') in section
    assert 'onclick="exportReturns(\'pdf\')"' in section
    assert 'onclick="exportReturns(\'xlsx\')"' in section
    assert "bi-file-earmark-pdf" in section
    assert "bi-file-earmark-excel" in section
    assert "btn-outline-danger" in section
    assert "btn-outline-success" in section
    # The odd-one-out icon/style must be gone.
    assert "bi-file-earmark-spreadsheet" not in section


def test_export_sends_the_tab_filters():
    out = _run({
        "returns-start-filter": "2026-10-01",
        "returns-end-filter": "2026-10-31",
        "returns-mode-filter": "exchange",
        "returns-search": "  txn-1  ",
    })
    urls = [entry["url"] for entry in out["opened"] if "url" in entry]
    expected = ("/api/returns_exchanges/export?start=2026-10-01&end=2026-10-31"
                "&mode=exchange&q=txn-1&format=pdf&pos_user_id=7&pos_role=manager&pos_branch_id=3")
    assert urls[0] == expected
    assert out["results"]["exportExcel"] is True
    urls_xlsx = [e["url"] for e in out["opened"] if e.get("url", "").endswith("format=xlsx&pos_user_id=7&pos_role=manager&pos_branch_id=3")]
    assert urls_xlsx[0] == expected.replace("format=pdf", "format=xlsx")
    # Unknown formats fall back to PDF rather than a broken download.
    urls_pdf = [e["url"] for e in out["opened"] if e.get("url", "").endswith("format=pdf&pos_user_id=7&pos_role=manager&pos_branch_id=3")]
    assert urls_pdf[-1] == expected


def test_export_omits_empty_filters():
    out = _run({"returns-start-filter": "  ", "returns-search": ""})
    urls = [entry["url"] for entry in out["opened"] if "url" in entry]
    assert urls[0] == "/api/returns_exchanges/export?format=pdf&pos_user_id=7&pos_role=manager&pos_branch_id=3"


def test_presets_set_and_clear_the_range():
    out = _run()
    assert out["todayStart"] == out["today"]
    assert out["todayEnd"] == out["today"]
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", out["today"])
    assert out["allStart"] == ""
    assert out["allEnd"] == ""
    assert out["invalidated"], "applying a preset must invalidate the returns cache"


def test_receipt_button_opens_the_hidden_print_url():
    out = _run()
    frames = [e["printFrame"] for e in out["opened"] if "printFrame" in e]
    assert frames == ["/api/returns_exchanges/wf-1/print?pos_user_id=7&pos_role=manager&pos_branch_id=3"]
    assert out["results"]["receipt"] is True
    assert not any("replaced" in entry for entry in out["opened"])


def test_sales_row_routes_to_returns_tab_with_the_transaction():
    out = _run()
    assert out["routedSearch"] == "txn-route-1"
    assert out["sectionsShown"][-1] == "returns"


SUBMISSION_HARNESS = """
const fields = {
  "return-exchange-settlement-method": "cash",
  "return-exchange-notes": "  Keep this payload  ",
};
const requests = [];
const toasts = [];
const controls = {bodyInput: {disabled: false}, footerClose: {disabled: false}};
const submit = {disabled: false, innerHTML: ""};
const status = {textContent: "", hidden: true, classList: {toggle() {}}};
const modal = {querySelectorAll: () => Object.values(controls), hidden: false};
const rows = __ROWS__;
const exchangeRows = [];
for (const row of rows) row.querySelector = (selector) => row[selector];
let modalHidden = false;
globalThis.document = {
  getElementById(id) {
    if (id === "returnExchangeModal") return modal;
    if (id === "return-exchange-submit") return submit;
    if (id === "return-exchange-status") return status;
    return {get value() { return fields[id] || ""; }, set value(value) { fields[id] = value; }};
  },
  querySelectorAll(selector) {
    if (selector === ".return-item-row") return rows;
    if (selector === ".exchange-item-row") return exchangeRows;
    return [];
  },
};
const CACHE_USER_ID = 7;
let CURRENT_USER_ROLE = __ROLE__;
const currentBranch = {id: 3};
let dashboardSessionStale = false;
let dashboardBranchStale = false;
let branchSwitchInFlight = false;
let currentSection = "dashboard";
let returnExchangeSaleData = {transaction_id: "txn-retained"};
const returnExchangeSubmission = {pending: false, uncertain: false, key: null, payload: null, identity: null};
function hasManagerOrBossAccess() { return ["manager", "boss"].includes(CURRENT_USER_ROLE); }
function showToast(message, kind) { toasts.push({message, kind}); }
function markBranchFormSaved() {}
function invalidateProductsCache() { if (__REFRESH_ERROR__) throw new Error("Refresh failed"); }
function sectionStateOf() { return {}; }
function loadProducts() {}
function loadProductsForPOS() {}
function loadSales() {}
function loadDashboardStats() {}
function openReturnExchangeReceipt(id) { fields.receiptId = id; return true; }
const bootstrap = {Modal: {getInstance: () => ({hide() { modalHidden = true; }})}};
let uuid = 0;
globalThis.crypto = {randomUUID: () => `00000000-0000-4000-8000-${String(++uuid).padStart(12, "0")}`};
const outcomes = __OUTCOMES__;
globalThis.fetch = async (url, options) => {
  const payload = JSON.parse(options.body);
  requests.push(payload);
  const outcome = outcomes.shift();
  if (outcome.networkError) throw new TypeError("Failed to fetch");
  return {
    ok: outcome.status >= 200 && outcome.status < 300,
    status: outcome.status,
    json: async () => outcome.body || null,
  };
};
__HELPERS__
(async () => {
  const snapshots = [];
  async function snapshot(label) {
    snapshots.push({
      label,
      requests: requests.length,
      key: returnExchangeSubmission.key,
      payload: returnExchangeSubmission.payload,
      uncertain: returnExchangeSubmission.uncertain,
      pending: returnExchangeSubmission.pending,
      submitDisabled: submit.disabled,
      bodyDisabled: controls.bodyInput.disabled,
      closeDisabled: controls.footerClose.disabled,
      submitText: submit.innerHTML,
      statusText: status.textContent,
      identityNow: returnExchangeIdentity(),
    });
  }
  await submitReturnExchange();
  await snapshot("network-error");
  await submitReturnExchange();
  await snapshot("conflict");
  await submitReturnExchange();
  await snapshot("server-error");
  await submitReturnExchange();
  await snapshot("success");
  outcomes.push({status: 400, body: {success: false, message: "Invalid return quantity"}});
  await submitReturnExchange();
  await snapshot("definite-error");
  await submitReturnExchange();
  await snapshot("fresh-request");
  console.log(JSON.stringify({snapshots, requests, toasts, receiptId: fields.receiptId, modalHidden}));
})().catch((error) => { console.error(error); process.exit(1); });
"""


def _run_return_submission(refresh_error=False):
    source = DASHBOARD.read_text(encoding="utf-8")
    helpers = "\n".join(_function(source, name) for name in (
        "returnExchangeIdentity", "setReturnExchangeSubmissionUI", "resetReturnExchangeSubmission",
        "collectReturnExchangePayload", "collectReturnLegacyCosts", "submitReturnExchange",
    ))
    rows = [{
        "select": {"value": "101"},
        ".return-item-select": {"value": "101"},
        ".return-item-qty": {"value": "1"},
        ".return-item-cost": {"value": "12.50"},
    }]
    script = SUBMISSION_HARNESS.replace("__ROWS__", json.dumps(rows))
    script = script.replace("__ROLE__", "'manager'")
    script = script.replace("__REFRESH_ERROR__", "true" if refresh_error else "false")
    script = script.replace("__OUTCOMES__", json.dumps([
        {"networkError": True},
        {"status": 409, "body": {"success": False, "message": "Accounting database is busy"}},
        {"status": 503, "body": {"success": False, "message": "Busy"}},
        {"status": 201, "body": {"success": True, "workflow_id": "wf-retry"}},
    ]))
    return _run_node_script(script.replace("__HELPERS__", helpers))


def test_return_retries_reuse_exact_payload_and_key():
    out = _run_return_submission()
    states = {item["label"]: item for item in out["snapshots"]}
    requests = out["requests"]
    assert requests[0]["idempotency_key"] == requests[1]["idempotency_key"] == requests[2]["idempotency_key"] == requests[3]["idempotency_key"]
    assert requests[0] == requests[1] == requests[2] == requests[3]
    assert requests[0]["legacy_costs"] == {"101": "12.50"}
    assert requests[0]["notes"] == "Keep this payload"
    for label in ("network-error", "conflict", "server-error"):
        state = states[label]
        assert state["uncertain"] is True
        assert state["pending"] is False
        assert state["submitDisabled"] is False
        assert state["bodyDisabled"] is True
        assert state["closeDisabled"] is True
        assert "Retry the same request" in state["submitText"]
    assert states["success"]["key"] is None
    assert states["success"]["submitDisabled"] is False
    assert out["receiptId"] == "wf-retry"
    assert out["modalHidden"] is True
    assert states["definite-error"]["uncertain"] is False
    assert states["definite-error"]["key"] is None
    assert requests[5]["idempotency_key"] != requests[0]["idempotency_key"]


def test_confirmed_return_refresh_error_does_not_require_retry():
    out = _run_return_submission(refresh_error=True)
    state = next(item for item in out["snapshots"] if item["label"] == "success")
    assert state["uncertain"] is False
    assert state["key"] is None
    assert state["submitDisabled"] is False
    assert state["bodyDisabled"] is False
    assert "was saved" in state["statusText"]
    assert len(out["requests"]) == 6


def _run_legacy_costs(role, costs):
    source = DASHBOARD.read_text(encoding="utf-8")
    helper = _function(source, "collectReturnLegacyCosts")
    rows = []
    for index, cost in enumerate(costs, 101):
        rows.append({
            "select": {"value": str(index)},
            ".return-item-select": {"value": str(index)},
            ".return-item-cost": {"value": cost},
        })
    script = f"""
const rows = {json.dumps(rows)};
for (const row of rows) row.querySelector = (selector) => row[selector];
const CURRENT_USER_ROLE = {json.dumps(role)};
globalThis.document = {{querySelectorAll: () => rows}};
function hasManagerOrBossAccess() {{ return ["manager", "boss"].includes(CURRENT_USER_ROLE); }}
{helper}
let result, error;
try {{ result = collectReturnLegacyCosts(rows.map(r => ({{sale_item_id: Number(r.select.value), quantity: 1}}))); }}
catch (exc) {{ error = exc.message; }}
console.log(JSON.stringify({{result, error}}));
"""
    return _run_node_script(script)


def test_legacy_recovery_costs_require_approval_and_valid_values():
    assert _run_legacy_costs("manager", ["0", "12.5", ""])["result"] == {
        "101": "0", "102": "12.5",
    }
    assert "Only a manager or boss" in _run_legacy_costs("cashier", ["12.50"])["error"]
    assert "at most two decimals" in _run_legacy_costs("boss", ["1.234"])["error"]
    assert "non-negative" in _run_legacy_costs("manager", ["-1"])["error"]


DEBT_PAYMENT_HARNESS = """
const fields = {
  "payment-debt-id": "12",
  "payment-amount": "5.005",
  "payment-notes": "  Counter cash  ",
};
const requests = [];
const toasts = [];
const amountControl = {disabled: false, get value() { return fields["payment-amount"]; }, set value(value) { fields["payment-amount"] = value; }};
const notesControl = {disabled: false, get value() { return fields["payment-notes"]; }, set value(value) { fields["payment-notes"] = value; }};
const submit = {disabled: false, textContent: ""};
const close = {disabled: false};
const status = {textContent: "", hidden: true, className: "", focus() {}};
let modalHidden = false;
let debtPaymentDraft = null;
let dashboardSessionStale = false;
let dashboardBranchStale = false;
let branchSwitchInFlight = false;
let checkoutServerUnavailable = false;
globalThis.navigator = {onLine: true};
globalThis.window = {addEventListener() {}};
globalThis.document = {
  getElementById(id) {
    if (id === "payment-amount") return amountControl;
    if (id === "payment-notes") return notesControl;
    if (id === "make-payment-submit") return submit;
    if (id === "payment-status") return status;
    return {get value() { return fields[id] || ""; }, set value(value) { fields[id] = value; }};
  },
  querySelectorAll() { return [close]; },
};
function inventoryIdentity() { return "user-7-branch-3"; }
function showToast(message, kind) { toasts.push({message, kind}); }
function formatCurrency(value) { return Number(value).toFixed(2); }
async function loadDebts() {}
async function loadCustomers() {}
function markBranchFormSaved() {}
const bootstrap = {Modal: {getInstance: () => ({hide() { modalHidden = true; }})}};
let uuid = 0;
globalThis.crypto = {randomUUID: () => `00000000-0000-4000-8000-${String(++uuid).padStart(12, "0")}`};
const outcomes = __OUTCOMES__;
globalThis.fetch = async (url, options) => {
  const payload = JSON.parse(options.body);
  requests.push({url, payload});
  const outcome = outcomes.shift();
  if (outcome.networkError) throw new TypeError("Failed to fetch");
  return {
    ok: outcome.status >= 200 && outcome.status < 300,
    status: outcome.status,
    redirected: false,
    json: async () => outcome.body || null,
  };
};
__HELPERS__
function freshDraft() {
  debtPaymentDraft = {debtId: "12", identity: inventoryIdentity(), loading: false,
    pending: false, uncertain: false, key: null, payload: null, balance: 10};
}
function snapshot(label) {
  return {label, requests: requests.length, key: debtPaymentDraft?.key || null,
    payload: debtPaymentDraft?.payload || null, uncertain: Boolean(debtPaymentDraft?.uncertain),
    amountDisabled: amountControl.disabled, notesDisabled: notesControl.disabled,
    closeDisabled: close.disabled, submitDisabled: submit.disabled, submitText: submit.textContent,
    statusText: status.textContent};
}
(async () => {
  const snapshots = [];
  freshDraft();
  await makePayment();
  snapshots.push(snapshot("network-error"));
  fields["payment-amount"] = "9.00";
  fields["payment-notes"] = "changed after uncertainty";
  await makePayment();
  snapshots.push(snapshot("conflict"));
  await makePayment();
  snapshots.push(snapshot("server-error"));
  await makePayment();
  snapshots.push(snapshot("success"));

  freshDraft();
  fields["payment-amount"] = "2.00";
  fields["payment-notes"] = "definite failure";
  await makePayment();
  snapshots.push(snapshot("definite-error"));
  fields["payment-amount"] = "3.00";
  await makePayment();
  snapshots.push(snapshot("fresh-request"));
  console.log(JSON.stringify({snapshots, requests, toasts, modalHidden}));
})().catch((error) => { console.error(error); process.exit(1); });
"""


def _run_debt_payment():
    source = DASHBOARD.read_text(encoding="utf-8")
    helpers = "\n".join(_function(source, name) for name in (
        "toCents", "centsToNumber", "debtPaymentUsable", "showDebtPaymentStatus",
        "updateDebtPaymentControls", "makePayment",
    ))
    script = DEBT_PAYMENT_HARNESS.replace("__HELPERS__", helpers)
    script = script.replace("__OUTCOMES__", json.dumps([
        {"networkError": True},
        {"status": 409, "body": {"success": False, "message": "Accounting database is busy"}},
        {"status": 503, "body": {"success": False, "message": "Busy"}},
        {"status": 200, "body": {"success": True, "replayed": True, "remaining_balance": 4.99}},
        {"status": 400, "body": {"success": False, "message": "Invalid payment amount"}},
        {"status": 200, "body": {"success": True, "remaining_balance": 7.00}},
    ]))
    return _run_node_script(script)


def test_debt_payment_retries_reuse_exact_payload_and_key():
    out = _run_debt_payment()
    states = {item["label"]: item for item in out["snapshots"]}
    requests = [item["payload"] for item in out["requests"]]
    assert requests[0] == requests[1] == requests[2] == requests[3]
    assert requests[0]["idempotency_key"] == requests[1]["idempotency_key"] == requests[2]["idempotency_key"] == requests[3]["idempotency_key"]
    assert requests[0]["amount"] == 5.01
    assert requests[0]["notes"] == "Counter cash"
    for label in ("network-error", "conflict", "server-error"):
        state = states[label]
        assert state["uncertain"] is True
        assert state["amountDisabled"] is True
        assert state["notesDisabled"] is True
        assert state["closeDisabled"] is True
        assert state["submitDisabled"] is False
        assert state["submitText"] == "Retry the same payment"
    assert states["success"]["key"] is None
    assert states["success"]["uncertain"] is False
    assert out["modalHidden"] is True
    assert states["definite-error"]["key"] is None
    assert states["definite-error"]["uncertain"] is False
    assert states["definite-error"]["amountDisabled"] is False
    assert "Not saved" in states["definite-error"]["statusText"]
    assert requests[5]["idempotency_key"] not in (requests[0]["idempotency_key"], requests[4]["idempotency_key"])


def test_debt_payment_wires_uncertainty_guards():
    source = DASHBOARD.read_text(encoding="utf-8")
    assert 'id="payment-status"' in source
    assert 'id="make-payment-submit"' in source
    assert 'addEventListener("hide.bs.modal"' in source
    assert 'addEventListener("beforeunload"' in source
    assert "idempotency_key: draft.key" in source
