"""Execute cashier warning and recovery flows with the existing Node test pattern."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_offline_sync_javascript import _function


SOURCE = (Path(__file__).parent / "templates" / "dashboard.html").read_text(encoding="utf-8")
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js required")


def run_js(body):
    helpers = "\n".join(_function(SOURCE, name) for name in (
        "posGridState", "setPOSCatalogNotice", "posResponseJSON",
        "showPOSCatalogError", "posStockHtml", "loadProductsForPOS",
    ))
    script = """
const notice = { hidden: true, innerHTML: '', className: '' };
const moreButton = { disabled: false };
const grid = { innerHTML: 'existing products', querySelector: () => moreButton };
globalThis.document = { getElementById: id => id === 'pos-catalog-notice' ? notice : grid };
function escapeHtml(value) { return String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;'); }
const POS_PER_PAGE = 50;
let posProductsLoading = false, posProductCursor = 50, posProductsHaveMore = true;
let currentBranch = { id: 1 };
function getCachedProducts() { return []; }
""" + helpers + "\n(async () => {\n" + body + "\n})().catch(e => { console.error(e); process.exit(1); });"
    result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize("status,title,action", [
    (401, "Sign in to continue", "Sign in"),
    (403, "Product access restricted", "manager"),
    (503, "Products are temporarily unavailable", "Try again"),
])
def test_error_explains_recovery(status, title, action):
    out = run_js(f"showPOSCatalogError({{status: {status}}}); console.log(JSON.stringify(notice));")
    assert out["hidden"] is False
    assert title in out["innerHTML"]
    assert action in out["innerHTML"]


def test_pagination_failure_keeps_existing_products_and_enables_retry():
    out = run_js("""
globalThis.fetch = async () => ({ok: false, status: 503});
loadProductsForPOS(false);
await new Promise(resolve => setTimeout(resolve, 0));
console.log(JSON.stringify({html: grid.innerHTML, disabled: moreButton.disabled, loading: posProductsLoading, warning: notice.innerHTML}));
""")
    assert out["html"] == "existing products"
    assert out["disabled"] is False
    assert out["loading"] is False
    assert "Try again" in out["warning"]


def test_notice_escapes_content_and_clears_after_recovery():
    out = run_js("""
setPOSCatalogNotice('<script>', '<img>');
const html = notice.innerHTML;
setPOSCatalogNotice();
console.log(JSON.stringify({html, hidden: notice.hidden, cleared: notice.innerHTML}));
""")
    assert "<script>" not in out["html"]
    assert "&lt;script&gt;" in out["html"]
    assert out["hidden"] is True
    assert out["cleared"] == ""


def test_login_redirect_does_not_parse_or_use_catalog():
    out = run_js("""
let parsed = false, status = null;
try { posResponseJSON({status: 200, redirected: true, json: () => { parsed = true; }}); }
catch (error) { status = error.status; }
console.log(JSON.stringify({parsed, status}));
""")
    assert out == {"parsed": False, "status": 401}


def test_stock_badge_is_explicit_and_escaped():
    out = run_js("console.log(JSON.stringify([posStockHtml({stock: 0}), posStockHtml({stock: 5, unit_symbol: '<kg>'})]));")
    assert "Out of stock" in out[0]
    assert "Available: 5" in out[1]
    assert "&lt;kg&gt;" in out[1]