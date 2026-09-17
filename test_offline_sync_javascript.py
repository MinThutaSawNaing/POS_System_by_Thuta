"""Behavioural tests for syncPendingSales() executed in Node with mocked fetch.

Covers the coding-level contract: single vs batched success notifications,
4xx/5xx queue-dropping, HTML-response stop, and network-error retry semantics.
"""

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
    """Run a Node script from a temp file.

    Large scripts must not be passed via ``node -e``: Windows overflows the
    process command line and aborts with a stack-buffer-overrun exit code.
    """
    handle, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(script)
        result = subprocess.run(
            [NODE, path], check=True, capture_output=True, text=True, encoding="utf-8"
        )
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


def _run_sync(scenarios):
    """Run syncPendingSales against scripted fetch responses.

    scenarios: list of dicts {status, json_body, content_type, network_error, sale}
    Returns the observed side effects as JSON.
    """
    source = DASHBOARD.read_text(encoding="utf-8")
    helpers = "\n".join(
        _function(source, name)
        for name in ("getPendingSales", "setPendingSales")
    )
    script = f"""
// --- mocks ---
globalThis.document = {{ getElementById: () => null }};
const storage = {{}};
globalThis.localStorage = {{
  getItem: (k) => (k in storage ? storage[k] : null),
  setItem: (k, v) => {{ storage[k] = String(v); }},
  removeItem: (k) => {{ delete storage[k]; }},
}};
let modalCalls = [];
let toastCalls = [];
function showSaleSuccessModal(id) {{ modalCalls.push(id); }}
function showToast(msg, kind) {{ toastCalls.push({{ msg, kind }}); }}
function updatePendingSalesBadge() {{ /* DOM-free no-op */ }}
function loadSales() {{ /* DOM-free no-op */ }}
function loadDashboardStats() {{ /* DOM-free no-op */ }}
function invalidateProductsCache() {{ /* DOM-free no-op */ }}
let lastStatus = null;
let connectionStatusTimer = null;
function setConnectionStatus(status) {{ lastStatus = status; }}
function isBrowserOffline() {{ return false; }}
let fetchIndex = 0;
const scenarios = {json.dumps(scenarios)};
async function fetch(url, opts) {{
  const s = scenarios[fetchIndex++];
  if (s.network_error) throw new TypeError("Failed to fetch");
  return {{
    ok: s.status >= 200 && s.status < 300,
    status: s.status,
    headers: {{ get: (h) => (h.toLowerCase() === "content-type" ? s.content_type : null) }},
    json: async () => s.json_body,
  }};
}}

// --- code under test ---
{helpers}
let isSyncingPendingSales = false;
{_function(source, "queueOfflineSale")}
{_function(source, "syncPendingSales")}

// --- seed queue ---
const seed = {json.dumps([s["sale"] for s in scenarios])};
setPendingSales(seed.map((saleData) => ({{
  transaction_id: saleData.transaction_id,
  saleData,
  created_at: new Date().toISOString(),
}})));

(async () => {{
  await syncPendingSales();
  const remaining = getPendingSales().map((p) => p.transaction_id);
  console.log(JSON.stringify({{ modalCalls, toastCalls, remaining, lastStatus }}));
  process.exit(0);
}})().catch((e) => {{ console.error(e); process.exit(1); }});
"""
    return _run_node_script(script)

def _sale(txn):
    return {
        "transaction_id": txn,
        "items": [{"product_id": 1, "price": 1000, "quantity": 1, "tax_rate": 0}],
        "payment_method": "cash",
        "cash_received": 1000,
    }


def test_single_success_shows_modal_and_empties_queue():
    out = _run_sync([
        {"status": 201, "content_type": "application/json",
         "json_body": {"success": True}, "sale": _sale("aa-1")},
    ])
    assert out["remaining"] == []
    assert out["modalCalls"] == ["aa-1"]


def test_multi_success_shows_one_toast_no_stacked_modals():
    out = _run_sync([
        {"status": 201, "content_type": "application/json", "json_body": {"success": True}, "sale": _sale("b-1")},
        {"status": 200, "content_type": "application/json", "json_body": {"success": True, "duplicate": True}, "sale": _sale("b-2")},
        {"status": 201, "content_type": "application/json", "json_body": {"success": True}, "sale": _sale("b-3")},
    ])
    assert out["remaining"] == []
    assert out["modalCalls"] == []  # no stacked modals
    assert len(out["toastCalls"]) == 1
    assert "synced: 3" in out["toastCalls"][0]["msg"]


def test_4xx_drops_sale_with_error_toast():
    out = _run_sync([
        {"status": 400, "content_type": "application/json",
         "json_body": {"success": False, "message": "Insufficient stock"}, "sale": _sale("c-1")},
    ])
    assert out["remaining"] == []  # dropped, not retried forever
    assert any("rejected" in t["msg"] and "Insufficient stock" in t["msg"] for t in out["toastCalls"])


def test_5xx_drops_sale_too():
    out = _run_sync([
        {"status": 500, "content_type": "application/json",
         "json_body": {"success": False, "message": "Error creating sale"}, "sale": _sale("d-1")},
    ])
    assert out["remaining"] == []
    assert any("rejected" in t["msg"] for t in out["toastCalls"])


def test_html_response_stops_sync_and_keeps_queue():
    out = _run_sync([
        {"status": 302, "content_type": "text/html", "json_body": {}, "sale": _sale("e-1")},
        {"status": 201, "content_type": "application/json", "json_body": {"success": True}, "sale": _sale("e-2")},
    ])
    # First entry kept (HTML stop), second never attempted
    assert out["remaining"] == ["e-1", "e-2"]
    assert out["modalCalls"] == []


def test_network_error_keeps_sale_queued():
    out = _run_sync([
        {"network_error": True, "sale": _sale("f-1")},
    ])
    assert out["remaining"] == ["f-1"]
    assert out["modalCalls"] == []


def test_mixed_batch_syncs_and_rejects_correctly():
    out = _run_sync([
        {"status": 201, "content_type": "application/json", "json_body": {"success": True}, "sale": _sale("g-1")},
        {"status": 400, "content_type": "application/json",
         "json_body": {"success": False, "message": "Product 9 not found"}, "sale": _sale("g-2")},
        {"status": 201, "content_type": "application/json", "json_body": {"success": True}, "sale": _sale("g-3")},
    ])
    assert out["remaining"] == []
    assert out["modalCalls"] == []  # mixed batch -> toast, not modal
    assert any("synced: 2" in t["msg"] for t in out["toastCalls"])
    assert any("rejected" in t["msg"] for t in out["toastCalls"])


def test_401_stops_sync_and_keeps_queue():
    """Session-expired JSON responses must NOT drop queued sales."""
    out = _run_sync([
        {"status": 401, "content_type": "application/json",
         "json_body": {"error": "Unauthorized"}, "sale": _sale("h-1")},
        {"status": 201, "content_type": "application/json",
         "json_body": {"success": True}, "sale": _sale("h-2")},
    ])
    # First entry kept, second never attempted after the 401 stop.
    assert out["remaining"] == ["h-1", "h-2"]
    assert out["modalCalls"] == []
    assert out["toastCalls"] == []


def test_403_stops_sync_and_keeps_queue():
    """Forbidden JSON responses keep the sale queued like 401 does."""
    out = _run_sync([
        {"status": 403, "content_type": "application/json",
         "json_body": {"error": "Forbidden"}, "sale": _sale("i-1")},
    ])
    assert out["remaining"] == ["i-1"]
    assert out["toastCalls"] == []


def test_status_shows_synced_after_successful_sync():
    """A fully-drained queue flips the sidebar indicator to 'Synced completed'."""
    out = _run_sync([
        {"status": 201, "content_type": "application/json",
         "json_body": {"success": True}, "sale": _sale("j-1")},
    ])
    assert out["remaining"] == []
    assert out["lastStatus"] == "synced"


def test_status_shows_offline_when_sync_cannot_reach_server():
    """A network failure leaves the queue intact and reports 'Offline mode detected'."""
    out = _run_sync([
        {"network_error": True, "sale": _sale("k-1")},
    ])
    assert out["remaining"] == ["k-1"]
    assert out["lastStatus"] == "offline"


def test_sidebar_nav_keeps_vertical_block_layout():
    """Regression guard: the sidebar nav must stay one vertical column.

    The first version of the pinned status footer turned .sidebar into a flex
    column and gave Bootstrap's .nav (display:flex; flex-wrap:wrap) a constrained
    height, which made nav links wrap side-by-side into multiple columns and
    destroyed the sidebar. The indicator must be pinned with sticky positioning
    while keeping the sidebar in its original block layout.
    """
    source = DASHBOARD.read_text(encoding="utf-8")

    sidebar_rule = re.search(r"^\s*\.sidebar\s*\{([^}]*)\}", source, re.MULTILINE)
    assert sidebar_rule is not None, "main .sidebar CSS rule not found"
    assert "display: flex" not in sidebar_rule.group(1)
    assert "flex-direction" not in sidebar_rule.group(1)

    # Never constrain the Bootstrap .nav height (that is what triggers the wrap).
    assert not re.search(r"\.sidebar\s*>\s*ul\.nav\s*\{", source)

    # Indicator must stay in normal flow via sticky, not absolute/flex pinning.
    indicator_rule = re.search(r"\.connection-status-indicator\s*\{([^}]*)\}", source)
    assert indicator_rule is not None, ".connection-status-indicator CSS rule not found"
    assert "position: sticky" in indicator_rule.group(1)
    assert "bottom:" in indicator_rule.group(1)


def test_offline_pwa_assets_vendored_and_helpers_exist():
    """Offline PWA guard: UI libraries must be local (not CDN), the Service
    Worker must be registered, and the persistent offline data-cache helpers
    must exist so POS can ring sales from cached data without a connection."""
    source = DASHBOARD.read_text(encoding="utf-8")

    # No CDN references for the four UI libraries anymore.
    assert "cdn.jsdelivr.net" not in source
    assert "cdnjs.cloudflare.com" not in source
    for asset in (
        "/public/vendor/bootstrap/bootstrap.min.css",
        "/public/vendor/bootstrap/bootstrap.bundle.min.js",
        "/public/vendor/bootstrap-icons/bootstrap-icons.css",
        "/public/vendor/chartjs/chart.umd.min.js",
    ):
        assert asset in source

    # Service Worker registration + offline data-cache helpers.
    assert 'navigator.serviceWorker.register("/sw.js")' in source
    for helper in (
        "function offlineCacheSet",
        "function offlineCacheGet",
        "function getCachedProducts",
        "function saveCachedProducts",
        "function findCachedProduct",
        "function renderProductsFromCacheForPOS",
        "function renderCategoriesView",
        "function fillCategoryDropdowns",
        "PRODUCT_CACHE_KEY",
    ):
        assert helper in source, f"missing helper {helper}"


def test_offline_product_cache_branch_fallback():
    """The offline product cache must fall back to the generic snapshot when the
    branch-scoped key is missing (boot order: loadProductsCached runs before the
    current branch is fetched), so the POS grid/barcode fallbacks still work."""
    source = DASHBOARD.read_text(encoding="utf-8")
    helpers = "\n".join(
        _function(source, name)
        for name in ("offlineCacheSet", "offlineCacheGet", "cachedProductsKey",
                     "getCachedProducts", "findCachedProduct")
    )
    script = f"""
globalThis.localStorage = {{
  getItem: (k) => (k in storage ? storage[k] : null),
  setItem: (k, v) => {{ storage[k] = String(v); }},
  removeItem: (k) => {{ delete storage[k]; }},
}};
const storage = {{}};
{helpers}
const PRODUCT_CACHE_KEY = "pos_products_all_cache";
const PRODUCTS = [
  {{ id: 1, name: "Cola", barcode: "111", price: 1.5, stock: 10, tax_rate: 0 }},
  {{ id: 2, name: "Chips", barcode: "222", price: 2.0, stock: 5, tax_rate: 5 }},
];
// Boot: currentBranch not set yet -> snapshot lands on the generic key.
let currentBranch = null;
offlineCacheSet(cachedProductsKey(), PRODUCTS);
// Later: branch selected, branch-scoped key not yet written.
currentBranch = {{ id: 7 }};
const fromGenericFallback = getCachedProducts();
const found = findCachedProduct(1);
// Now the branch-scoped key is written (refreshPersistentProductCache path).
offlineCacheSet(cachedProductsKey(), PRODUCTS);
const branchKey = cachedProductsKey();
const fromBranch = getCachedProducts();
console.log(JSON.stringify({{
  genericFallbackCount: fromGenericFallback.length,
  branchKey,
  branchCount: fromBranch.length,
  found: found ? found.name : null,
}}));
process.exit(0);
"""
    out = _run_node_script(script)
    assert out["genericFallbackCount"] == 2, "generic fallback must return cached products"
    assert out["branchKey"] == "pos_products_all_cache_7"
    assert out["branchCount"] == 2
    assert out["found"] == "Cola"


def test_pwa_manifest_and_install_prompt_are_wired():
    """Installability guard: dashboard/login must expose the manifest and the
    dashboard must safely handle Chromium's native install prompt."""
    source = DASHBOARD.read_text(encoding="utf-8")
    login_source = (Path(__file__).parent / "templates" / "login.html").read_text(encoding="utf-8")
    manifest = json.loads((Path(__file__).parent / "public" / "manifest.webmanifest").read_text(encoding="utf-8"))

    assert manifest["id"] == "/"
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert manifest["theme_color"] == "#343a40"
    assert {icon["sizes"] for icon in manifest["icons"]} >= {"192x192", "512x512"}
    assert all("maskable" in icon["purpose"] for icon in manifest["icons"])

    for html in (source, login_source):
        assert 'rel="manifest" href="/public/manifest.webmanifest"' in html
        assert 'rel="apple-touch-icon" href="/public/pwa/icon-192.png"' in html
        assert 'name="theme-color" content="#343a40"' in html

    assert "/public/vendor/fontawesome/css/all.min.css" in login_source
    assert "cdnjs.cloudflare.com" not in login_source

    assert 'id="install-app-button"' in source
    assert "function initPwaInstallPrompt" in source
    assert 'addEventListener("beforeinstallprompt"' in source
    assert 'addEventListener("appinstalled"' in source
    assert "await promptEvent.prompt()" in source
    assert "initPwaInstallPrompt();" in source


def test_offline_sale_keeps_printable_receipt_snapshot():
    """A queued sale must preserve enough immutable client-side data to print
    immediately while offline, before the server-side receipt exists."""
    source = DASHBOARD.read_text(encoding="utf-8")
    helpers = "\n".join(
        _function(source, name)
        for name in (
            "toCents", "centsToNumber", "getPendingSales", "setPendingSales",
            "generateClientTxnId", "buildOfflineReceiptSnapshot", "queueOfflineSale",
            "getPendingOfflineReceipt",
        )
    )
    script = f"""
const storage = {{}};
globalThis.localStorage = {{
  getItem: (k) => (k in storage ? storage[k] : null),
  setItem: (k, v) => {{ storage[k] = String(v); }},
}};
globalThis.document = {{
  getElementById: (id) => id === "username-display" ? {{ textContent: "Cashier One" }} : null,
}};
const APP_SETTINGS = {{ posName: "Parrot POS", currencySuffix: "MMK", receiptPaperSize: "THERMAL_58MM" }};
let currentBranch = {{ name: "Main", address: "1 Main St", phone: "555", email: "main@example.com" }};
let cart = [
  {{ product_id: 1, name: "Coffee", price: 100, quantity: 2, tax_rate: 5 }},
  {{ product_id: 2, name: "Tea", price: 50, quantity: 1, tax_rate: 0 }},
];
function updatePendingSalesBadge() {{}}
function setConnectionStatus() {{}}
function showToast() {{}}
{helpers}
const sale = {{ items: [], payment_method: "cash", cash_received: 300 }};
const receipt = buildOfflineReceiptSnapshot(sale);
queueOfflineSale(sale, receipt);
const stored = getPendingOfflineReceipt(sale.transaction_id);
console.log(JSON.stringify({{
  transactionId: stored.transactionId,
  saleTransactionId: sale.transaction_id,
  paperWidthMm: stored.paperWidthMm,
  cashierName: stored.cashierName,
  currencySuffix: stored.currencySuffix,
  itemCount: stored.items.length,
  subtotal: stored.subtotal,
  tax: stored.tax,
  total: stored.total,
  change: stored.change,
  firstTax: stored.items[0].taxAmount,
}}));
"""
    out = _run_node_script(script)
    # The stored receipt must reference the same client-generated transaction id
    # that was queued, so the Print Receipt action can find it while offline.
    assert isinstance(out["transactionId"], str) and out["transactionId"]
    assert out["transactionId"] == out["saleTransactionId"]
    assert out == {
        "transactionId": out["transactionId"],
        "saleTransactionId": out["saleTransactionId"],
        "paperWidthMm": 58,
        "cashierName": "Cashier One",
        "currencySuffix": "MMK",
        "itemCount": 2,
        "subtotal": 250,
        "tax": 10,
        "total": 260,
        "change": 40,
        "firstTax": 10,
    }


def test_offline_print_uses_local_receipt_until_synced():
    """Print Receipt must render from the local snapshot while a sale is still
    queued, and fall back to the server receipt route once it has synced."""
    source = DASHBOARD.read_text(encoding="utf-8")
    helpers = "\n".join(
        _function(source, name)
        for name in (
            "escapeHtml", "getPendingSales", "setPendingSales",
            "getPendingOfflineReceipt", "formatOfflineReceiptMoney",
            "openOfflineReceiptWindow", "printReceiptFromSuccessModal",
        )
    )
    script = f"""
const storage = {{}};
globalThis.localStorage = {{
  getItem: (k) => (k in storage ? storage[k] : null),
  setItem: (k, v) => {{ storage[k] = String(v); }},
}};
let printedHtml = "";
let serverReceiptCalls = [];
globalThis.window = {{
  open: () => ({{
    opener: null,
    document: {{ write: (html) => {{ printedHtml += html; }}, close: () => {{}} }},
    close: () => {{}},
  }}),
}};
function showToast() {{}}
function openReceiptWindow(id) {{ serverReceiptCalls.push(id); return true; }}
let currentSaleTransactionId = null;
{helpers}

const receipt = {{
  transactionId: "offline-txn-1",
  createdAt: "1/2/2026, 10:00:00 AM",
  posName: "Parrot POS",
  currencySuffix: "MMK",
  paperWidthMm: 58,
  branchName: "Main Branch",
  branchAddress: "1 Main St",
  branchPhone: "555",
  branchEmail: "main@example.com",
  cashierName: "Cashier One",
  paymentMethod: "cash",
  cashReceived: 300,
  change: 40,
  items: [
    {{ name: "Coffee", quantity: 2, unitPrice: 100, lineSubtotal: 200, taxRate: 5, taxAmount: 10 }},
    {{ name: "Tea", quantity: 1, unitPrice: 50, lineSubtotal: 50, taxRate: 0, taxAmount: 0 }},
  ],
  subtotal: 250,
  tax: 10,
  total: 260,
}};

setPendingSales([{{ transaction_id: "offline-txn-1", saleData: {{}}, receiptSnapshot: receipt, created_at: "" }}]);
currentSaleTransactionId = "offline-txn-1";
printReceiptFromSuccessModal();
const queuedHtml = printedHtml;

// Once synced the pending entry is gone -> server receipt route is used.
printedHtml = "";
setPendingSales([]);
printReceiptFromSuccessModal();

console.log(JSON.stringify({{
  usedLocalWhileQueued: queuedHtml.length > 0,
  htmlHasTotal: queuedHtml.includes("260.00 MMK"),
  htmlHasTax: queuedHtml.includes("10.00 MMK"),
  htmlHasItem: queuedHtml.includes("Coffee"),
  htmlHasBranch: queuedHtml.includes("Main Branch"),
  htmlMarksPendingSync: queuedHtml.includes("OFFLINE SALE"),
  htmlAutoPrints: queuedHtml.includes("window.print()"),
  htmlEscapedQuote: queuedHtml.includes("&quot;") || !queuedHtml.includes("<script>alert"),
  serverRouteAfterSync: serverReceiptCalls,
}}));
"""
    out = _run_node_script(script)
    assert out["usedLocalWhileQueued"] is True
    assert out["htmlHasTotal"] is True
    assert out["htmlHasTax"] is True
    assert out["htmlHasItem"] is True
    assert out["htmlHasBranch"] is True
    assert out["htmlMarksPendingSync"] is True
    assert out["htmlAutoPrints"] is True
    assert out["serverRouteAfterSync"] == ["offline-txn-1"]


def test_offline_sale_cycle_queues_receipt_but_posts_only_server_payload():
    """Full offline cycle guard: completing a sale offline must queue a printable
    receipt locally while the eventual sync POST carries only the sale payload the
    server expects — never client-only receipt data."""
    source = DASHBOARD.read_text(encoding="utf-8")
    helpers = "\n".join(
        _function(source, name)
        for name in (
            "toCents", "centsToNumber", "getPendingSales", "setPendingSales",
            "generateClientTxnId", "buildOfflineReceiptSnapshot", "queueOfflineSale",
            "getPendingOfflineReceipt", "finishOfflineSale", "syncPendingSales",
        )
    )
    script = f"""
const storage = {{}};
globalThis.localStorage = {{
  getItem: (k) => (k in storage ? storage[k] : null),
  setItem: (k, v) => {{ storage[k] = String(v); }},
  removeItem: (k) => {{ delete storage[k]; }},
}};
globalThis.document = {{
  getElementById: (id) => (id === "username-display" ? {{ textContent: "Cashier" }} : null),
}};
globalThis.navigator = {{ onLine: false }};
const APP_SETTINGS = {{ posName: "Parrot POS", currencySuffix: "$", receiptPaperSize: "THERMAL_80MM" }};
let currentBranch = {{ id: 3, name: "Main" }};
let cart = [{{ product_id: 9, name: "Coffee", price: 100, quantity: 2, tax_rate: 5 }}];
let isSaleProcessing = false;
let isSyncingPendingSales = false;
let connectionStatusTimer = null;
const SALE_COOLDOWN_MS = 0;
let posted = [];
let modalIds = [];
function setCompleteSaleButtonState() {{}}
function updateCartDisplay() {{}}
function showSaleSuccessModal(id) {{ modalIds.push(id); }}
function showToast() {{}}
function updatePendingSalesBadge() {{}}
function setConnectionStatus() {{}}
function isBrowserOffline() {{ return true; }}
function loadSales() {{}}
function loadDashboardStats() {{}}
function invalidateProductsCache() {{}}
async function fetch(url, opts) {{
  posted.push({{ url, body: JSON.parse(opts.body) }});
  return {{
    ok: true,
    status: 201,
    headers: {{ get: () => "application/json" }},
    json: async () => ({{ success: true, transaction_id: "server-1" }}),
  }};
}}
{helpers}

(async () => {{
// 1) Complete the sale while offline.
const saleData = {{
  items: [{{ product_id: 9, price: 100, quantity: 2, tax_rate: 5 }}],
  payment_method: "cash",
  cash_received: 300,
}};
finishOfflineSale(saleData);

const queuedId = saleData.transaction_id;
const queuedReceipt = getPendingOfflineReceipt(queuedId);
const cartCleared = cart.length === 0;

// 2) Reconnect and sync.
await syncPendingSales();

const remaining = getPendingSales().length;
const postedBody = posted[0] ? posted[0].body : null;
console.log(JSON.stringify({{
  queuedIdIsString: typeof queuedId === "string" && queuedId.length > 0,
  receiptQueued: !!queuedReceipt,
  receiptTotal: queuedReceipt ? queuedReceipt.total : null,
  receiptItemName: queuedReceipt ? queuedReceipt.items[0].name : null,
  cartCleared,
  modalShownForOfflineSale: modalIds.includes(queuedId),
  syncUrl: posted[0] ? posted[0].url : null,
  postedKeys: postedBody ? Object.keys(postedBody).sort() : null,
  postedLeaksReceipt: postedBody ? Object.keys(postedBody).some((k) => k.toLowerCase().includes("receipt")) : null,
  remainingAfterSync: remaining,
}}));
}})().catch((e) => {{ console.error(e); process.exit(1); }});
"""
    out = _run_node_script(script)

    assert out["queuedIdIsString"] is True
    assert out["receiptQueued"] is True
    assert out["receiptTotal"] == 210          # 200 subtotal + 10 tax
    assert out["receiptItemName"] == "Coffee"
    assert out["cartCleared"] is True
    assert out["modalShownForOfflineSale"] is True

    # API layer: the sync must hit the sales endpoint with only server fields.
    assert out["syncUrl"] == "/api/sales"
    assert out["postedKeys"] == [
        "cash_received", "items", "payment_method", "transaction_id",
    ]
    assert out["postedLeaksReceipt"] is False
    assert out["remainingAfterSync"] == 0


def test_offline_receipt_escapes_hostile_product_names():
    """The offline receipt writes raw HTML into a new window, so every
    interpolated value must be escaped to remain inert."""
    source = DASHBOARD.read_text(encoding="utf-8")
    helpers = "\n".join(
        _function(source, name)
        for name in (
            "escapeHtml", "getPendingSales", "setPendingSales",
            "getPendingOfflineReceipt", "formatOfflineReceiptMoney",
            "openOfflineReceiptWindow", "printReceiptFromSuccessModal",
        )
    )
    script = f"""
const storage = {{}};
globalThis.localStorage = {{
  getItem: (k) => (k in storage ? storage[k] : null),
  setItem: (k, v) => {{ storage[k] = String(v); }},
}};
let printed = "";
globalThis.window = {{
  open: () => ({{ opener: null, document: {{ write: (h) => {{ printed += h; }}, close: () => {{}} }}, close: () => {{}} }}),
}};
function showToast() {{}}
function openReceiptWindow() {{ return true; }}
let currentSaleTransactionId = null;
{helpers}

const hostile = '<img src=x onerror="alert(1)"><script>alert("xss")<\\/script>';
const receipt = {{
  transactionId: "txn-1",
  createdAt: "now",
  posName: hostile,
  currencySuffix: "$",
  paperWidthMm: 80,
  branchName: hostile,
  branchAddress: hostile,
  branchPhone: "",
  branchEmail: "",
  cashierName: hostile,
  paymentMethod: "cash",
  cashReceived: 10,
  change: 0,
  items: [{{ name: hostile, quantity: 1, unitPrice: 10, lineSubtotal: 10, taxRate: 0, taxAmount: 0 }}],
  subtotal: 10, tax: 0, total: 10,
}};
setPendingSales([{{ transaction_id: "txn-1", saleData: {{}}, receiptSnapshot: receipt, created_at: "" }}]);
currentSaleTransactionId = "txn-1";
printReceiptFromSuccessModal();

console.log(JSON.stringify({{
  wroteHtml: printed.length > 0,
  hasRawImgTag: printed.includes("<img src=x"),
  hasRawScriptTag: printed.includes("<script>alert"),
  escapedAngle: printed.includes("&lt;img"),
  // Only the receipt's own print script may exist.
  scriptTagCount: (printed.match(/<script/gi) || []).length,
}}));
"""
    out = _run_node_script(script)
    assert out["wroteHtml"] is True
    assert out["escapedAngle"] is True
    assert out["hasRawImgTag"] is False
    assert out["hasRawScriptTag"] is False
    assert out["scriptTagCount"] == 1
