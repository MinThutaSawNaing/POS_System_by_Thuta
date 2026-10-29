"""Executable Node regressions for bounded inventory workflows (no app/data import)."""

from pathlib import Path
import re
import shutil
import subprocess

import pytest

from test_offline_sync_javascript import _function, _run_node_script


SOURCE = (Path(__file__).parent / "templates" / "dashboard.html").read_text(encoding="utf-8")
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")


HARNESS = r"""
const assert = require('node:assert/strict');
class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {};
    this.listeners = {}; this.attributes = {}; this._value = ''; this._html = '';
    this.textContent = ''; this.isConnected = true; this.scrollTop = 999;
  }
  set innerHTML(value) { this._html = value; this.children = []; }
  get innerHTML() { return this._html; }
  get options() { return this.children; }
  get selectedOptions() { return this.children.filter(o => o.value === this.value); }
  get value() { return this._value; }
  set value(value) { this._value = String(value); }
  appendChild(child) { this.children.push(child); child.parentNode = this; return child; }
  insertBefore(child, before) {
    const index = this.children.indexOf(before);
    if (index < 0) this.appendChild(child);
    else { this.children.splice(index, 0, child); child.parentNode = this; }
  }
  replaceChildren(...children) { this.children = []; children.forEach(c => this.appendChild(c)); }
  cloneNode() { const clone = new Element(this.tagName); clone.value = this.value;
    clone.textContent = this.textContent; clone.dataset = {...this.dataset}; return clone; }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(key, fn) { (this.listeners[key] ||= []).push(fn); }
  emit(key, event = {}) { (this.listeners[key] || []).forEach(fn => fn(event)); }
  focus() { document.activeElement = this; }
  querySelector() { return new Element(); }
}
const elements = {};
const document = { getElementById(id) { return elements[id] ||= new Element(); },
  createElement(tag) { return new Element(tag); } };
const CACHE_USER_ID = 7, CACHE_USER_ROLE = 'manager';
let currentBranch = {id: 3}, dashboardSessionStale = false, dashboardBranchStale = false;
const inventoryCursorState = Object.create(null);
const pageState = {products: 1, purchases: 1, warehouse: 1};
const pageSize = {products: 30, purchases: 30, warehouse: 30};
// The Products table keeps a multi-select map keyed by product id.
const productSelection = new Map();
const warehouseSelection = new Map();
let warehousePageItems = [];
function syncWarehouseSelectionIdentity() {}
function syncWarehouseSelectionUi() {}
const tick = () => new Promise(resolve => setImmediate(resolve));
const pending = [];
function fetch(url, init) { return new Promise((resolve, reject) => pending.push({url, init, resolve, reject})); }
function reply(index, data, status = 200) {
  pending[index].resolve({ok: status < 400, status, json: async () => data});
}
function formatCurrency(value) { return '$' + value; }
function escapeHtml(value) { return String(value); }
function hasManagerOrBossAccess() { return true; }
function hasCapability() { return true; }
function getProductPhotoHtml() { return ''; }
function saveProductsCache() { throw Error('cursor pages must not overwrite unrelated offline cache'); }
function getProductsCache() { throw Error('cursor pages must not read unrelated offline cache'); }
const toasts = [];
function showToast(message, kind) { toasts.push({message, kind}); }
function hasPurchaseReceivingRetry() { return false; }
function renderPagination() { throw Error('offset pagination is forbidden'); }
function paginateArray() { throw Error('client-side pagination is forbidden'); }
const emptyPage = {items: [], has_more: false, next_cursor: null};
"""


def run_js(names, script, harness=HARNESS):
    helpers = "\n".join(_function(SOURCE, name) for name in names)
    try:
        return _run_node_script(harness + "\n" + helpers + "\n(async () => {\n" + script +
                                "\nconsole.log(JSON.stringify({passed:true}));\n})()"
                                ".catch(error => { console.error(error); process.exit(1); });")
    except subprocess.CalledProcessError as error:
        pytest.fail(error.stdout + error.stderr)


CURSORS = ("inventoryIdentity", "beginInventoryPage", "renderInventoryPagination")


def test_cursor_stack_filter_and_branch_resets_and_identity_guards():
    run_js(CURSORS, r"""
let request;
function load() {
  const params = new URLSearchParams({view: 'management', per_page: '30', q: query});
  request = beginInventoryPage('products', params, 'pager', load);
  return params;
}
let query = 'a';
assert.equal(load().has('cursor'), false);
request.accept({items: [], has_more: true, next_cursor: 'opaque+signed/token=='});
const buttons = () => elements.pager.children[0].children;
assert.equal(buttons()[0].disabled, true);
assert.equal(buttons()[2].textContent, 'Next');
buttons()[2].emit('click');
assert.equal(inventoryCursorState.products.cursor, 'opaque+signed/token==');
assert.deepEqual(inventoryCursorState.products.previous, [null]);
assert.equal(pageState.products, 2);
request.accept({items: [], has_more: true, next_cursor: 'another-token'});
buttons()[0].emit('click');
assert.equal(inventoryCursorState.products.cursor, null);
request.accept({items: [], has_more: true, next_cursor: 'changed-token'});
buttons()[2].emit('click');
const stale = request;
query = 'b';
assert.equal(load().has('cursor'), false);
assert.equal(stale.current(), false);
assert.equal(pageState.products, 1);
assert.equal(inventoryCursorState.products.previous.length, 0);
request.accept({items: [], has_more: true, next_cursor: 'branch-token'});
buttons()[2].emit('click');
const oldBranch = request;
currentBranch = {id: 4};
assert.equal(oldBranch.current(), false);
assert.equal(load().has('cursor'), false);
const samePageOld = request;
load();
assert.equal(samePageOld.current(), false);
dashboardBranchStale = true;
assert.equal(request.current(), false);
""")


def test_real_products_loader_bounds_rows_and_ignores_out_of_order_results():
    run_js(CURSORS + ("parsePagedResponse", "renderProductsResponse", "syncProductSelectionUi",
                      "getProductPageCheckboxes", "loadProducts"), r"""
elements['products-search'] = new Element(); elements['products-search'].value = ' Widget ';
const first = loadProducts();
assert.deepEqual(Object.fromEntries(new URL(pending[0].url, 'https://test').searchParams),
  {view: 'management', per_page: '30', q: 'Widget'});
elements['products-search'].value = 'new';
const second = loadProducts();
const products = Array.from({length:30}, (_, i) => ({id:100-i, name:'NEW'+i, price:1, stock:2, tax_rate:0}));
reply(1, {items:products, has_more:true, next_cursor:'signed.token'}); await second;
assert.equal(elements['products-table'].children.length, 30);
assert.equal(elements['products-table-scroll'].scrollTop, 0);
reply(0, emptyPage); await first;
assert.equal(elements['products-table'].children.length, 30);
elements['products-pagination'].children[0].children[2].emit('click');
assert.equal(new URL(pending[2].url, 'https://test').searchParams.get('cursor'), 'signed.token');
reply(2, emptyPage); await tick();
assert.ok(elements['products-table'].innerHTML.includes('No products found'));
assert.equal(elements['products-pagination'].children[0].children[0].disabled, false);
""")


@pytest.mark.parametrize("loader,key,prefix,view", [
    ("loadWarehouseInventory", "warehouse", "warehouse", "register"),
    ("loadPurchaseOrders", "purchases", "purchase-orders", "register"),
])
def test_real_register_loaders_use_bounded_contract_and_ignore_old_errors(loader, key, prefix, view):
    run_js(CURSORS + (loader,), f"""
const load = {loader};
elements['{prefix}-search'] = new Element(); elements['{prefix}-search'].value = 'one';
const first = load();
const listRequests = () => pending.filter(p => !p.url.endsWith('/summary'));
assert.equal(new URL(listRequests()[0].url, 'https://test').searchParams.get('view'), '{view}');
assert.equal(new URL(listRequests()[0].url, 'https://test').searchParams.get('per_page'), '30');
elements['{prefix}-search'].value = 'two';
const second = load();
listRequests()[1].resolve({{ok:true, json:async () => emptyPage}}); await second;
listRequests()[0].reject(new Error('old network failure')); await first;
assert.ok(elements['{prefix}-table'].innerHTML.includes('No '));
assert.equal(inventoryCursorState.{key}.previous.length, 0);
""")


def test_lookup_caps_results_preserves_selection_metadata_aborts_and_is_keyboard_accessible():
    run_js(("inventoryIdentity", "makeRemoteInventorySelect"), r"""
const parent = new Element();
const select = new Element('select'); parent.appendChild(select);
const blank = new Element('option'); blank.textContent = 'Select product'; select.appendChild(blank);
const lookup = makeRemoteInventorySelect(select, '/api/purchase_orders/products', 'Purchase product', true);
assert.equal(new URL(pending[0].url, 'https://test').searchParams.get('limit'), '30');
reply(0, [{id:9, name:'Selected', cost:12.5, price:20}]); await tick();
select.value = '9';
const input = parent.children[0];
assert.equal(input.attributes['aria-label'], 'Search purchase product');
input.emit('keydown', {key:'ArrowDown', preventDefault(){}});
assert.equal(document.activeElement, select);
const request = lookup.load();
reply(1, Array.from({length:50}, (_, i) => ({id:100+i, name:'Other', unit_cost:2})));
await request;
assert.equal(select.options.length, 32); // placeholder + 30 results + selected
assert.equal(select.value, '9');
assert.equal(select.selectedOptions[0].dataset.unitCost, '12.5');
assert.equal(select.selectedOptions[0].dataset.cost, '12.5');
const stale = lookup.load();
input.value = 'new';
input.emit('input');
assert.equal(pending[2].init.signal.aborted, true);
input.emit('keydown', {key:'Enter', preventDefault(){}});
reply(3, [{id:8, name:'Fresh', unit_cost:4}]); await tick();
reply(2, [{id:7, name:'Stale'}]); await stale;
assert.equal(select.options.some(option => option.value === '7'), false);
assert.equal(select.selectedOptions[0].dataset.unitCost, '12.5');
const exact = lookup.load(99);
assert.equal(new URL(pending[4].url, 'https://test').searchParams.get('id'), '99');
currentBranch.id = 8;
reply(4, [{id:99, name:'Old branch'}]); await exact;
assert.equal(select.options.some(option => option.value === '99'), false);
""")


def test_receiving_replays_exact_persisted_body_after_ambiguous_failures():
    run_js(("purchaseReceivingStorageKey", "readPurchaseReceivingRetry", "sendPurchaseReceiving"), r"""
const store = new Map();
globalThis.sessionStorage = {getItem:key => store.get(key) || null,
  setItem:(key,value) => store.set(key,value), removeItem:key => store.delete(key)};
const body = JSON.stringify({items:[{purchase_order_item_id:5, received_qty:2}], idempotency_key:'stable-key'});
const first = sendPurchaseReceiving(42, body).catch(error => error);
assert.equal(store.get('pos_receiving_retry_v1:7:42'), body);
pending[0].reject(new Error('timeout')); await first;
const second = sendPurchaseReceiving(42, '{"items":[],"idempotency_key":"WRONG"}').catch(error => error);
assert.equal(pending[1].init.body, body);
reply(1, {success:false, message:'busy'}, 409); await second;
assert.equal(store.get('pos_receiving_retry_v1:7:42'), body);
const third = sendPurchaseReceiving(42, 'ignored');
reply(2, {success:true, replayed:true}); await third;
assert.equal(store.size, 0);
assert.equal(pending[2].init.body, body);
const unauthorized = sendPurchaseReceiving(42, body).catch(error => error);
reply(3, {success:false, message:'session expired'}, 403); await unauthorized;
assert.equal(store.get('pos_receiving_retry_v1:7:42'), body);
const signedOut = sendPurchaseReceiving(42, 'changed').catch(error => error);
reply(4, {success:false}, 401); await signedOut;
assert.equal(pending[4].init.body, body);
assert.equal(store.get('pos_receiving_retry_v1:7:42'), body);
const rejected = sendPurchaseReceiving(42, body).catch(error => error);
reply(5, {success:false, message:'invalid quantity'}, 400); await rejected;
assert.equal(store.size, 0);
sessionStorage.setItem = () => {throw Error('storage disabled')};
await assert.rejects(sendPurchaseReceiving(42, body), /storage disabled/);
assert.equal(pending.length, 6);
""")


def test_bounded_export_surfaces_413_json_and_closes_blank_window():
    run_js(("inventoryIdentity", "openBoundedInventoryExport"), r"""
const report = {closed:false, document:{body:{}}, close(){this.closed = true}};
globalThis.window = {location:{href:'https://test/'}, open:() => report};
assert.equal(openBoundedInventoryExport('/api/products/export?format=pdf'), true);
assert.equal(new URL(pending[0].url, 'https://test').searchParams.get('search_mode'), 'prefix');
reply(0, {message:'Too many rows; narrow filters'}, 413); await tick();
assert.equal(report.closed, true);
assert.equal(toasts[0].message, 'Too many rows; narrow filters');
""")


def test_dropdowns_and_po_setup_do_not_download_complete_catalogs():
    assert '/api/categories?active_only=true&counts=false' in _function(SOURCE, "loadCategoryDropdowns")
    assert 'fetch("/api/suppliers")' not in _function(SOURCE, "preparePurchaseOrderModal")
    assert 'fetch("/api/suppliers")' not in _function(SOURCE, "loadPOSupplierFilter")
    assert 'fetch("/api/purchase_orders/products")' not in _function(SOURCE, "addPurchaseOrderItemRow")
    assert 'searchable-select po-item-product' not in SOURCE
    assert re.search(r'products:\s*30', SOURCE)


def test_receiving_paged_lines_and_saved_retries_bypass_detail_fetch():
    harness = HARNESS + r"""
let receivingInFlight = false, receivingOpenSequence = 0, selectedReceivePO = null;
let savedBody = null;
function readPurchaseReceivingRetry() { return savedBody; }
globalThis.bootstrap = {Modal:{getOrCreateInstance:() => ({show(){}})}};
"""
    run_js(CURSORS + ("openReceivePurchaseOrder",), r"""
const first = openReceivePurchaseOrder(42);
const params = new URL(pending[0].url, 'https://test').searchParams;
assert.equal(params.get('view'), 'receiving');
assert.equal(params.get('per_page'), '30');
reply(0, {id:42, po_number:'BIG', items:Array.from({length:30}, (_, i) =>
  ({id:i+1, product_name:'P'+i, ordered_qty:5, received_qty:0})), has_more:true, next_cursor:'line-cursor'});
await first;
assert.equal(elements['receive-po-items-container'].children.length, 30);
elements['receive-po-pagination'].children[0].children[2].emit('click');
assert.equal(new URL(pending[1].url, 'https://test').searchParams.get('cursor'), 'line-cursor');
reply(1, {id:42, po_number:'BIG', ...emptyPage}); await tick();
savedBody = JSON.stringify({items:[{purchase_order_item_id:900, received_qty:2}], idempotency_key:'retry'});
await openReceivePurchaseOrder(42);
assert.equal(pending.length, 2);
assert.ok(elements['receive-po-items-container'].children[0].innerHTML.includes('Saved line #900'));
assert.ok(elements['receive-po-items-container'].children[0].innerHTML.includes('disabled'));
""", harness)


def test_migrated_consumers_use_summary_and_bounded_remote_branch_lookups():
    assert '/api/products/summary' in _function(SOURCE, "loadDashboardStats")
    for name in ("loadDashboardStats", "loadProductsForPromotionModal", "addExchangeItemRow", "loadProductsForBarcodeLabels"):
        assert "loadProductsCached()" not in _function(SOURCE, name)
    assert 'target.searchParams.set("search_mode", "prefix")' in _function(SOURCE, "openBoundedInventoryExport")


def test_barcode_cursor_search_retains_checked_rows_on_empty_results_and_rejects_stale():
    harness = HARNESS + r"""
const barcodeSelectedRows = new Map();
function requireCapability() { return true; }
function updateBarcodeLabelFilterStatus() {}
function updateBarcodeSelectAllState() {}
"""
    run_js(CURSORS + ("loadProductsForBarcodeLabels",), r"""
const retained = new Element('tr'); retained.quantity = 7;
barcodeSelectedRows.set('123', retained);
elements['barcode-product-search'] = new Element();
elements['barcode-product-search'].value = 'abc';
const first = loadProductsForBarcodeLabels();
assert.equal(new URL(pending[0].url, 'https://test').searchParams.get('per_page'), '30');
elements['barcode-product-search'].value = 'xyz';
const second = loadProductsForBarcodeLabels();
reply(1, emptyPage); await second;
assert.equal(elements['barcode-products-table'].children[0], retained);
assert.equal(retained.quantity, 7);
assert.equal(retained.hidden, true);
reply(0, emptyPage); await first;
assert.equal(elements['barcode-products-table'].children[0], retained);
""", harness)