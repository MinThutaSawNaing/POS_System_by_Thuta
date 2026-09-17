"""Behavioural tests for syncPendingSales() executed in Node with mocked fetch.

Covers the coding-level contract: single vs batched success notifications,
4xx/5xx queue-dropping, HTML-response stop, and network-error retry semantics.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


DASHBOARD = Path(__file__).parent / "templates" / "dashboard.html"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is required")


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
    result = subprocess.run([NODE, "-e", script], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


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
