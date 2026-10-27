"""Execute cashier warning and recovery flows with the existing Node test pattern."""
import json
import re
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
const searchInput = makeElement("input");
searchInput.value = "";
globalThis.document = {
  getElementById: (id) => id === "pos-category-chips" ? bar
    : id === "product-search" ? searchInput : null,
  createElement: (tag) => makeElement(tag),
};
let posCategories = [];
let posCategoryFilterId = null;
let cachedCatalog = [];
function getCachedProducts() { return cachedCatalog; }
function loadProductsForPOS() { loads += 1; }
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


def test_category_chips_stay_hidden_when_the_branch_has_no_categories():
    out = run_quick_access_js("""
renderPOSCategoryChips();
console.log(JSON.stringify({ chipsHidden: bar.hidden, chips: bar.children.length }));
""")
    assert out["chipsHidden"] is True
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


def test_pos_quick_actions_offer_receipts_history_but_no_catalog_shortcut():
    """Cashiers asked for a Receipts shortcut; the catalog reload and the
    best-seller strip were removed, so they must not come back unnoticed."""
    start = SOURCE.index('<div class="pos-quick-actions"')
    end = SOURCE.index('id="pos-category-chips"', start)
    markup = SOURCE[start:end]

    assert "Receipts" in markup
    assert "showSection('sales')" in markup
    assert "Catalog" not in markup
    assert "pos-quick-picks" not in markup
    assert "pos-quick-pick" not in SOURCE, "the best-seller strip was removed"
    assert "renderPOSQuickPicks" not in SOURCE
    assert "loadPOSQuickPicks" not in SOURCE


def test_auto_scanner_toggle_is_a_large_touch_target():
    rule = re.search(r"\.auto-barcode-scanner-control\s*\{(.*?)\}", SOURCE, re.S).group(1)
    min_height = int(re.search(r"min-height:\s*(\d+)px", rule).group(1))
    assert min_height >= 44
    assert re.search(r"\.auto-barcode-scanner-control\s+\.form-check-input\s*\{[^}]*width:\s*2\.7em", SOURCE, re.S)



def test_sales_history_offers_a_way_back_to_the_pos_tab():
    start = SOURCE.index('<div id="sales-section"')
    end = SOURCE.index('id="sales-start-date-filter"', start)
    markup = SOURCE[start:end]

    assert "Back to POS" in markup
    assert "showSection('pos')" in markup


def test_pos_panels_expose_a_keyboard_resizable_divider():
    """The catalogue and cart panels grow/shrink like the sidebar does."""
    assert 'id="pos-workspace"' in SOURCE
    assert 'id="pos-cart-resize-handle"' in SOURCE
    assert 'role="separator"' in SOURCE
    assert 'aria-valuemin="320"' in SOURCE
    assert 'aria-valuemax="620"' in SOURCE
    assert "initPOSSplitResizer();" in SOURCE
    split_rule = SOURCE[SOURCE.index(".pos-workspace > .pos-cart-column {"):]
    split_rule = split_rule[:split_rule.index("}")]
    assert "var(--pos-cart-width)" in split_rule


def test_pos_split_has_a_visible_pointer_and_a_grip():
    """A cashier must be able to discover the resize before trying it."""
    hint = SOURCE[SOURCE.index('<div id="pos-split-hint"'):]
    hint = hint[:hint.index("</div>")]
    assert "Drag the divider" in hint
    assert "d-lg-flex" in hint and "d-none" in hint, "the hint only makes sense while the split is active"
    assert "pos-split-hint" in SOURCE, "the hint needs styling"

    handle = SOURCE[SOURCE.index('id="pos-cart-resize-handle"'):]
    handle = handle[:handle.index("</div>")]
    assert 'aria-describedby="pos-split-hint"' in handle
    assert "bi-grip-vertical" in handle
    assert "double-click" in handle.lower()

    grip_rule = SOURCE[SOURCE.index(".pos-split-grip {"):]
    grip_rule = grip_rule[:grip_rule.index("}")]
    assert "border: 1px solid" in grip_rule
    assert "background: #fff" in grip_rule



def run_split_resizer_js(body):
    """Execute initPOSSplitResizer against a stubbed workspace/handle/cart."""
    helper = _function(SOURCE, "initPOSSplitResizer")
    script = """
const stored = {};
globalThis.localStorage = {
  getItem: (key) => (key in stored ? stored[key] : null),
  setItem: (key, value) => { stored[key] = String(value); },
};
globalThis.window = { innerWidth: 1400 };
const POS_CART_WIDTH_KEY = "pos_cart_width";
const POS_CART_MIN_WIDTH = 320;
const POS_CART_MAX_WIDTH = 620;
const POS_CART_DEFAULT_WIDTH = 400;
const handleListeners = {};
const documentListeners = {};
const classes = new Set();
const handle = {
  attrs: {},
  addEventListener: (name, fn) => { handleListeners[name] = fn; },
  setAttribute(name, value) { this.attrs[name] = String(value); },
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; },
  classList: {
    add: (name) => classes.add(name),
    remove: (name) => classes.delete(name),
    contains: (name) => classes.has(name),
  },
};
const workspace = {
  style: {
    props: {},
    setProperty(name, value) { this.props[name] = value; },
  },
};
const cartColumn = { getBoundingClientRect: () => ({ right: 1000 }) };
globalThis.document = {
  getElementById: (id) => id === "pos-workspace" ? workspace
    : id === "pos-cart-resize-handle" ? handle
    : id === "pos-cart-column" ? cartColumn : null,
  addEventListener: (name, fn) => { documentListeners[name] = fn; },
  body: { style: {} },
};
function event(extra) {
  return Object.assign({ cancelable: true, defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; } }, extra);
}
""" + helper + "\n(async () => {\n" + body + "\n})().catch(e => { console.error(e); process.exit(1); });"
    result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)



def test_cart_split_restores_a_saved_width_and_clamps_it():
    out = run_split_resizer_js("""
stored["pos_cart_width"] = "900";
initPOSSplitResizer();
console.log(JSON.stringify({
  width: workspace.style.props["--pos-cart-width"],
  aria: handle.attrs["aria-valuenow"],
  saved: stored["pos_cart_width"],
}));
""")
    assert out["width"] == "620px"
    assert out["aria"] == "620"
    # Restoring the saved layout is not a user change, so nothing is rewritten.
    assert out["saved"] == "900"


def test_cart_split_defaults_to_400px_without_a_saved_width():
    out = run_split_resizer_js("""
stored["pos_cart_width"] = "not-a-number";
initPOSSplitResizer();
console.log(JSON.stringify({ width: workspace.style.props["--pos-cart-width"], aria: handle.attrs["aria-valuenow"] }));
""")
    assert out == {"width": "400px", "aria": "400"}


def test_dragging_the_divider_resizes_and_persists_the_cart():
    out = run_split_resizer_js("""
initPOSSplitResizer();
handleListeners["mousedown"](event({}));
const dragging = classes.has("is-dragging");
documentListeners["mousemove"](event({ clientX: 640 }));
documentListeners["mouseup"](event({}));
console.log(JSON.stringify({
  dragging,
  width: workspace.style.props["--pos-cart-width"],
  aria: handle.attrs["aria-valuenow"],
  saved: stored["pos_cart_width"],
  released: classes.has("is-dragging"),
  cursor: document.body.style.cursor,
}));
""")
    # The cart's right edge sits at 1000px, so a pointer at 640 asks for 360px.
    assert out["dragging"] is True
    assert out["width"] == "360px"
    assert out["aria"] == "360"
    assert out["saved"] == "360"
    assert out["released"] is False
    assert out["cursor"] == ""


def test_dragging_is_ignored_while_the_panels_are_stacked():
    out = run_split_resizer_js("""
globalThis.window.innerWidth = 700;
initPOSSplitResizer();
handleListeners["mousedown"](event({}));
documentListeners["mousemove"](event({ clientX: 640 }));
documentListeners["mouseup"](event({}));
console.log(JSON.stringify({ width: workspace.style.props["--pos-cart-width"], saved: stored["pos_cart_width"] || null }));
""")
    assert out == {"width": "400px", "saved": None}


def test_cart_split_supports_keyboard_steps_and_reset():
    out = run_split_resizer_js("""
initPOSSplitResizer();
handleListeners["keydown"](event({ key: "ArrowLeft" }));
const afterLeft = workspace.style.props["--pos-cart-width"];
handleListeners["keydown"](event({ key: "Home" }));
const afterHome = workspace.style.props["--pos-cart-width"];
handleListeners["keydown"](event({ key: "ArrowRight", shiftKey: true }));
const afterShiftRight = workspace.style.props["--pos-cart-width"];
handleListeners["keydown"](event({ key: "End" }));
const afterEnd = workspace.style.props["--pos-cart-width"];
handleListeners["dblclick"](event({}));
console.log(JSON.stringify({
  afterLeft, afterHome, afterShiftRight, afterEnd,
  afterReset: workspace.style.props["--pos-cart-width"],
  saved: stored["pos_cart_width"],
}));
""")
    assert out["afterLeft"] == "416px"
    assert out["afterHome"] == "620px"
    assert out["afterShiftRight"] == "580px"
    assert out["afterEnd"] == "320px"
    assert out["afterReset"] == "400px"
    assert out["saved"] == "400"

