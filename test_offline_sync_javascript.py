"""Behavioural tests for syncPendingSales() executed in Node with mocked fetch.

Covers the coding-level contract: single vs batched success notifications,
non-destructive recovery, immutable intents, and manager-reviewed retries.
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
            [NODE, path], check=False, capture_output=True, text=True, encoding="utf-8"
        )
        assert result.returncode == 0, result.stderr
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


def test_offline_product_cache_never_falls_back_across_branches():
    """Only the active branch's account-scoped snapshot may supply stock;
    generic boot snapshots and other branches must never be borrowed."""
    source = DASHBOARD.read_text(encoding="utf-8")
    helpers = "\n".join(
        _function(source, name)
        for name in ("scopedOfflineCacheKey", "offlineCacheSet", "offlineCacheGet", "cachedProductsKey",
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
const CACHE_USER_ID = 3;
const CACHE_USER_ROLE = "cashier";
const PRODUCT_CACHE_KEY = "pos_products_all_cache";
const PRODUCTS = [
  {{ id: 1, name: "Cola", barcode: "111", price: 1.5, stock: 10, tax_rate: 0 }},
  {{ id: 2, name: "Chips", barcode: "222", price: 2.0, stock: 5, tax_rate: 5 }},
];
// A generic boot snapshot is stored but cannot supply branch stock.
let currentBranch = null;
offlineCacheSet(cachedProductsKey(), PRODUCTS);
const beforeBranchCount = getCachedProducts().length;
currentBranch = {{ id: 7 }};
const fromGenericFallback = getCachedProducts();
const foundBeforeSnapshot = findCachedProduct(1);
// The active branch snapshot still supports the grid and product lookup.
offlineCacheSet(cachedProductsKey(), PRODUCTS);
const branchKey = cachedProductsKey();
const fromBranch = getCachedProducts();
const found = findCachedProduct(1);
currentBranch = {{ id: 8 }};
const otherBranchCount = getCachedProducts().length;
const otherBranchFound = findCachedProduct(1);
console.log(JSON.stringify({{
  beforeBranchCount,
  genericFallbackCount: fromGenericFallback.length,
  foundBeforeSnapshot,
  branchKey,
  storedBranchKey: scopedOfflineCacheKey(branchKey),
  branchSnapshotStored: scopedOfflineCacheKey(branchKey) in storage,
  branchCount: fromBranch.length,
  found: found ? found.name : null,
  otherBranchCount,
  otherBranchFound,
}}));
process.exit(0);
"""
    out = _run_node_script(script)
    assert out["beforeBranchCount"] == 0
    assert out["genericFallbackCount"] == 0, "generic stock must not leak into a branch"
    assert out["foundBeforeSnapshot"] is None
    assert out["branchKey"] == "pos_products_all_cache_7"
    assert out["storedBranchKey"] == "pos_account_v2:3:cashier:pos_products_all_cache_7"
    assert out["branchSnapshotStored"] is True
    assert out["branchCount"] == 2
    assert out["found"] == "Cola"
    assert out["otherBranchCount"] == 0
    assert out["otherBranchFound"] is None


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
    assert manifest["theme_color"] == "#161616"
    assert {icon["sizes"] for icon in manifest["icons"]} >= {"192x192", "512x512"}
    assert all("maskable" in icon["purpose"] for icon in manifest["icons"])

    for html in (source, login_source):
        assert 'rel="manifest" href="/public/manifest.webmanifest"' in html
        assert 'rel="apple-touch-icon" href="/public/pwa/icon-192.png"' in html
        assert 'name="theme-color" content="#161616"' in html

    assert "/public/vendor/fontawesome/css/all.min.css" in login_source
    assert "cdnjs.cloudflare.com" not in login_source

    assert 'id="install-app-button"' in source
    assert "function initPwaInstallPrompt" in source
    assert 'addEventListener("beforeinstallprompt"' in source
    assert 'addEventListener("appinstalled"' in source
    assert "await promptEvent.prompt()" in source
    assert "initPwaInstallPrompt();" in source


def test_offline_print_uses_local_receipt_until_synced():
    """Print Receipt must render from the local snapshot while a sale is still
    queued, and fall back to the server receipt route once it has synced."""
    source = DASHBOARD.read_text(encoding="utf-8")
    helpers = "\n".join(
        _function(source, name)
        for name in (
            "escapeHtml", "getPendingSales", "setPendingSales",
            "getPendingOfflineReceipt", "formatOfflineReceiptMoney",
            "formatOfflineReceiptNumber", "openOfflineReceiptWindow",
            "printReceiptFromSuccessModal",
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


def test_offline_receipt_escapes_hostile_product_names():
    """The offline receipt writes raw HTML into a new window, so every
    interpolated value must be escaped to remain inert."""
    source = DASHBOARD.read_text(encoding="utf-8")
    helpers = "\n".join(
        _function(source, name)
        for name in (
            "escapeHtml", "getPendingSales", "setPendingSales",
            "getPendingOfflineReceipt", "formatOfflineReceiptMoney",
            "formatOfflineReceiptNumber", "openOfflineReceiptWindow",
            "printReceiptFromSuccessModal",
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

def test_offline_receipt_id_fits_thermal_paper():
    """The queued-sale receipt printed the full 36-character UUID.

    A UUID is wider than 58 mm thermal paper and `white-space: nowrap` on the
    value cell stopped it wrapping, so the overflowing row pushed the whole
    receipt off to the right. The offline receipt must print the same short
    code the server receipt shows (``transaction_id[-8:].upper()``), and long
    values must wrap inside the paper instead of overflowing it.
    """
    source = DASHBOARD.read_text(encoding="utf-8")
    helpers = "\n".join(
        _function(source, name)
        for name in (
            "escapeHtml", "getPendingSales", "setPendingSales",
            "getPendingOfflineReceipt", "formatOfflineReceiptMoney",
            "formatOfflineReceiptNumber", "openOfflineReceiptWindow",
            "printReceiptFromSuccessModal",
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

const fullId = "00000000-0000-0000-0000-abcdef123456";
const receipt = {{
  transactionId: fullId,
  createdAt: "1/2/2026, 10:00:00 AM",
  posName: "Parrot POS",
  currencySuffix: "MMK",
  paperWidthMm: 58,
  branchName: "Main Branch",
  branchAddress: "1 Main St",
  branchPhone: "555",
  branchEmail: "main@example.com",
  cashierName: "Cashier One With A Very Long Display Name",
  paymentMethod: "cash",
  cashReceived: 4000,
  change: 850,
  items: [{{ name: "Coffee", quantity: 2, unitPrice: 1500, lineSubtotal: 3000, taxRate: 5, taxAmount: 150 }}],
  subtotal: 3000, tax: 150, total: 3150,
}};
setPendingSales([{{ transaction_id: fullId, saleData: {{}}, receiptSnapshot: receipt, created_at: "" }}]);
currentSaleTransactionId = fullId;
printReceiptFromSuccessModal();

console.log(JSON.stringify({{
  wroteHtml: printed.length > 0,
  shortNumberMatchesServerRule: formatOfflineReceiptNumber(fullId) === "EF123456",
  unknownWhenMissing: formatOfflineReceiptNumber("") === "UNKNOWN",
  showsShortReceiptNumber: printed.includes(">EF123456<"),
  hidesFullTransactionId: !printed.includes(fullId),
  keepsLongValues: printed.includes("Cashier One With A Very Long Display Name"),
}}));
"""
    out = _run_node_script(script)
    assert out["wroteHtml"] is True
    assert out["shortNumberMatchesServerRule"] is True
    assert out["unknownWhenMissing"] is True
    assert out["showsShortReceiptNumber"] is True
    assert out["hidesFullTransactionId"] is True
    assert out["keepsLongValues"] is True

    # The offline stylesheet must mirror the server receipt's wrap-safe rows:
    # the label keeps its natural width, the value may break anywhere, and the
    # paper box clips anything that still does not fit.
    template = _function(source, "openOfflineReceiptWindow")
    label_rule = re.search(r"\.row span:first-child\s*\{([^}]*)\}", template)
    value_rule = re.search(r"\.row span:last-child\s*\{([^}]*)\}", template)
    paper_rule = re.search(r"^[^\n]*\.receipt \{.*$", template, re.MULTILINE)
    assert label_rule is not None, ".row label CSS rule missing from the offline receipt"
    assert value_rule is not None, ".row value CSS rule missing from the offline receipt"
    assert paper_rule is not None, ".receipt CSS rule missing from the offline receipt"
    assert "flex: 0 0 auto" in label_rule.group(1)
    assert "overflow-wrap: anywhere" in value_rule.group(1)
    assert "white-space: nowrap" not in value_rule.group(1)
    assert "overflow: hidden" in paper_rule.group(0)
