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
// Quick category filters: the POS browse grid only adds ?category_id when a
// chip is active, so the harness starts on "All products".
let posCategoryFilterId = null;
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


def test_selected_category_narrows_the_catalog_request():
    out = run_js("""
let requested = null;
globalThis.fetch = async (input) => { requested = String(input); return { ok: true, status: 200, json: async () => ({ items: [], has_more: false, next_cursor: null }) }; };
posCategoryFilterId = 7;
loadProductsForPOS();
await new Promise(resolve => setTimeout(resolve, 0));
console.log(JSON.stringify({ requested, html: grid.innerHTML, loading: posProductsLoading }));
""")
    assert "view=pos" in out["requested"]
    assert "category_id=7" in out["requested"]
    assert "No products in this category" in out["html"]
    assert "Show all products" in out["html"]
    assert out["loading"] is False


def test_browsing_without_a_chip_requests_the_whole_branch():
    out = run_js("""
let requested = null;
globalThis.fetch = async (input) => { requested = String(input); return { ok: true, status: 200, json: async () => ({ items: [], has_more: false, next_cursor: null }) }; };
loadProductsForPOS();
await new Promise(resolve => setTimeout(resolve, 0));
console.log(JSON.stringify({ requested, html: grid.innerHTML }));
""")
    assert "category_id" not in out["requested"]
    assert "No products in this branch" in out["html"]


def run_quick_access_js(body):
    """Run the cashier quick-access helpers against a tiny DOM stub."""
    helpers = "\n".join(_function(SOURCE, name) for name in (
        "posCategoryAccent", "posCategoriesFromCache", "renderPOSCategoryChips",
        "setPOSCategoryFilter", "clearPOSSearchAndFilter", "focusPOSSearchInput",
        "renderPOSQuickPicks",
    ))
    script = """
function makeElement(tagName) {
  const element = {
    tagName: String(tagName).toUpperCase(), children: [], className: "", type: "",
    title: "", hidden: false, textContent: "", style: {}, attrs: {},
    appendChild(child) { this.children.push(child); return child; },
    setAttribute(name, value) { this.attrs[name] = String(value); },
    addEventListener(name, handler) { this.listener = { name: name, handler: handler }; },
    focus() { focused += 1; },
    select() { selected += 1; },
  };
  let html = "";
  Object.defineProperty(element, "innerHTML", {
    get: () => html,
    set: (value) => { html = value; element.children = []; },
  });
  return element;
}
let focused = 0, selected = 0, loads = 0;
const bar = makeElement("div");
const strip = makeElement("div");
const searchInput = makeElement("input");
searchInput.value = "";
globalThis.document = {
  getElementById: (id) => id === "pos-category-chips" ? bar
    : id === "pos-quick-picks" ? strip
    : id === "product-search" ? searchInput : null,
  createElement: (tag) => makeElement(tag),
};
let posCategories = [];
let posCategoryFilterId = null;
let cachedCatalog = [];
function getCachedProducts() { return cachedCatalog; }
function loadProductsForPOS() { loads += 1; }
function formatCurrency(value) { return Number(value).toFixed(2) + " $"; }
function addToCart() {}
""" + helpers + "\n(async () => {\n" + body + "\n})().catch(e => { console.error(e); process.exit(1); });"
    result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)




def test_category_chips_offer_all_products_plus_each_branch_category():
    out = run_quick_access_js("""
posCategories = [{id: 3, name: 'Drinks', color: '#0d6efd'}, {id: 4, name: '<script>alert(1)</script>', color: 'red;background:url(x)'}];
renderPOSCategoryChips();
console.log(JSON.stringify({
  hidden: bar.hidden,
  chips: bar.children.map(c => ({
    text: c.children.map(child => child.textContent).join(''),
    pressed: c.attrs['aria-pressed'],
    cls: c.className,
    dot: c.children[0] && c.children[0].style ? (c.children[0].style.backgroundColor || '') : '',
  })),
}));
""")
    assert out["hidden"] is False
    assert [chip["text"] for chip in out["chips"]] == [
        "All products", "Drinks", "<script>alert(1)</script>",
    ]
    assert out["chips"][0]["pressed"] == "true"
    assert "is-active" in out["chips"][0]["cls"]
    assert out["chips"][1]["dot"] == "#0d6efd"
    # A manager-authored colour that is not plain hex must never reach the style.
    assert out["chips"][2]["dot"] == ""


def test_tapping_a_category_clears_the_search_and_reloads_the_grid():
    out = run_quick_access_js("""
posCategories = [{id: 3, name: 'Drinks', color: ''}];
searchInput.value = 'cola';
renderPOSCategoryChips();
bar.children[1].listener.handler();
console.log(JSON.stringify({
  filter: posCategoryFilterId,
  search: searchInput.value,
  loads: loads,
  pressed: bar.children.map(c => c.attrs['aria-pressed']),
  active: bar.children.map(c => c.className.includes('is-active')),
}));
""")
    assert out["filter"] == 3
    assert out["search"] == ""
    assert out["loads"] == 1
    assert out["pressed"] == ["false", "true"]
    assert out["active"] == [False, True]


def test_reset_returns_to_the_whole_catalog_and_refocuses_the_scanner():
    out = run_quick_access_js("""
posCategories = [{id: 3, name: 'Drinks', color: ''}];
posCategoryFilterId = 3;
renderPOSCategoryChips();
clearPOSSearchAndFilter();
console.log(JSON.stringify({ filter: posCategoryFilterId, loads: loads, focused: focused, selected: selected, cls: bar.children[0].className }));
""")
    assert out["filter"] is None
    assert out["loads"] == 1
    assert out["focused"] == 1
    assert out["selected"] == 1
    assert "is-active" in out["cls"]


def test_quick_picks_add_best_sellers_as_text_and_skip_out_of_stock():
    out = run_quick_access_js("""
renderPOSQuickPicks([
  {id: 1, name: '<img src=x onerror=alert(1)>', price: 2.5, stock: 4},
  {id: 2, name: 'Sold out', price: 9, stock: 0},
]);
const picks = strip.children.slice(1).map(p => ({
  name: p.children[0].textContent,
  price: p.children[1].textContent,
  title: p.title,
}));
console.log(JSON.stringify({ hidden: strip.hidden, picks: picks, stripHtml: strip.innerHTML }));
""")
    assert out["hidden"] is False
    assert len(out["picks"]) == 1
    assert out["picks"][0]["name"] == "<img src=x onerror=alert(1)>"
    assert out["picks"][0]["price"] == "2.50 $"
    assert "Sold out" not in out["picks"][0]["title"]
    # Product names are written as text, never as markup.
    assert out["stripHtml"] == ""


def test_chips_and_quick_picks_stay_hidden_with_nothing_to_offer():
    out = run_quick_access_js("""
renderPOSCategoryChips();
renderPOSQuickPicks([]);
console.log(JSON.stringify({ chipsHidden: bar.hidden, picksHidden: strip.hidden, chips: bar.children.length }));
""")
    assert out["chipsHidden"] is True
    assert out["picksHidden"] is True
    assert out["chips"] == 0


def test_offline_categories_come_from_the_saved_catalog():
    out = run_quick_access_js("""
cachedCatalog = [
  {category_id: 4, category: 'Bakery'},
  {category_id: 3, category: 'Drinks'},
  {category_id: null, category: 'Legacy free text'},
  {category_id: 3, category: 'Drinks'},
];
console.log(JSON.stringify(posCategoriesFromCache()));
""")
    assert out == [
        {"id": 4, "name": "Bakery", "color": ""},
        {"id": 3, "name": "Drinks", "color": ""},
    ]
