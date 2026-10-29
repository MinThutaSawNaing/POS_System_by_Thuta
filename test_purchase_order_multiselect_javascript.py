"""Run the purchase-order bulk picker and its real line builder in Node."""

import shutil

import pytest

from test_inventory_paging_javascript import HARNESS, run_js as run_inventory_js
from test_purchase_order_modal_javascript import CREATE_FUNCTIONS, run_po_js


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")

PICKER = (
    "inventoryIdentity", "loadPurchaseOrderProducts", "renderPurchaseOrderProducts",
    "updatePurchaseOrderProductCount", "selectPurchaseOrderProducts",
    "addSelectedPurchaseOrderProducts",
)
SETUP = r"""
const poProductPicker = {selected:new Map(), items:[], sequence:0, controller:null,
  identity:inventoryIdentity()};
let purchaseOrderSaving = false, authorized = true;
function requireCapability() { return authorized; }
const first = {id:1, name:'Coffee <dark>', branch_name:'One', price:7, cost:3.25};
const second = {id:2, name:'Tea', branch_name:'Two', price:4, cost:1.50};
"""


def run_js(names, script, harness=HARNESS):
    return run_inventory_js(tuple(dict.fromkeys(("inventoryIdentity",) + names)), script, harness + SETUP)


def test_selection_survives_search_and_select_results_then_clears():
    run_js(PICKER, r"""
const load = loadPurchaseOrderProducts();
assert.equal(new URL(pending[0].url, 'https://test').searchParams.get('limit'), '30');
reply(0, [first]); await load;
let label = elements['po-products-results'].children[0];
assert.equal(label.children[1].textContent, 'Coffee <dark> — One | Cost: $3.25');
label.children[0].checked = true; label.children[0].emit('change');
assert.equal(elements['po-products-add'].textContent, 'Add selected (1)');
elements['po-products-search'].value = ' Tea ';
const next = loadPurchaseOrderProducts();
assert.equal(new URL(pending[1].url, 'https://test').searchParams.get('q'), 'Tea');
reply(1, [second]); await next;
selectPurchaseOrderProducts(true);
assert.deepEqual([...poProductPicker.selected.keys()], ['1','2']);
elements['po-products-results'].children[0].children[0].checked = false;
elements['po-products-results'].children[0].children[0].emit('change');
assert.deepEqual([...poProductPicker.selected.keys()], ['1']);
poProductPicker.items = [first]; renderPurchaseOrderProducts();
assert.equal(elements['po-products-results'].children[0].children[0].checked, true);
selectPurchaseOrderProducts(false);
assert.equal(elements['po-products-add'].disabled, true);
""")


def test_late_failed_and_wrong_branch_searches_do_not_replace_results_or_selection():
    run_js(PICKER, r"""
poProductPicker.selected.set('1', first);
const old = loadPurchaseOrderProducts();
const newer = loadPurchaseOrderProducts();
assert.equal(pending[0].init.signal.aborted, true);
reply(1, [second]); await newer;
reply(0, [first]); await old;
assert.equal(poProductPicker.items[0].id, 2);
const failed = loadPurchaseOrderProducts(); reply(2, {}, 500); await failed;
assert.ok(elements['po-products-status'].textContent.includes('Search failed'));
assert.equal(poProductPicker.selected.size, 1);
const wrongBranch = loadPurchaseOrderProducts(); currentBranch = {id:4};
reply(3, [first]); await wrongBranch;
assert.equal(poProductPicker.items.length, 0);
""")


def test_bulk_add_skips_duplicates_removes_only_untouched_rows_and_guards_writes():
    run_js(PICKER, r"""
function row(product='', qty='', cost='') {
  return {fields:{'.po-item-product':{value:product}, '.po-item-qty':{value:qty}, '.po-item-cost':{value:cost}},
    removed:false, querySelector(key){return this.fields[key]}, remove(){this.removed=true}};
}
const existing = row('1', '8', '9.75'), blank = row(), partial = row('', '4');
document.querySelectorAll = () => [existing, blank, partial];
const added = [];
globalThis.addPurchaseOrderItemRow = product => added.push(product);
poProductPicker.selected.set('1', first); poProductPicker.selected.set('2', second);
purchaseOrderSaving = true; addSelectedPurchaseOrderProducts();
purchaseOrderSaving = false; authorized = false; addSelectedPurchaseOrderProducts();
authorized = true; currentBranch = {id:4}; addSelectedPurchaseOrderProducts();
assert.equal(added.length, 0); assert.equal(poProductPicker.selected.size, 2);
currentBranch = {id:3}; addSelectedPurchaseOrderProducts();
assert.deepEqual(added, [second]);
assert.equal(blank.removed, true); assert.equal(partial.removed, false);
assert.equal(existing.fields['.po-item-qty'].value, '8');
assert.equal(poProductPicker.selected.size, 0);
assert.equal(elements['po-products-add'].disabled, true);
""")


def test_real_bulk_line_builder_sets_product_quantity_and_cost_and_keeps_manual_entry():
    run_js(("addPurchaseOrderItemRow",), r"""
const created = [];
document.createElement = tag => {
  const element = new Element(tag);
  if (tag === 'div') {
    element.fields = {};
    element.querySelector = key => element.fields[key] ||= new Element();
  }
  return element;
};
globalThis.makeRemoteInventorySelect = (select, endpoint, label, product) => {
  created.push({select, endpoint, product});
};
addPurchaseOrderItemRow({id:9, name:'Product', branch_name:'Branch', price:5, cost:2.75});
const row = elements['po-items-container'].children[0];
assert.equal(row.querySelector('.po-item-product').value, '9');
assert.equal(row.querySelector('.po-item-product').children[0].dataset.unitCost, '2.75');
assert.equal(row.querySelector('.po-item-qty').value, '1');
assert.equal(row.querySelector('.po-item-cost').value, '2.75');
assert.equal(created[0].endpoint, '/api/purchase_orders/products');
const select = row.querySelector('.po-item-product');
select.children[0].dataset.unitCost = '4.50'; select.emit('change');
assert.equal(row.querySelector('.po-item-cost').value, '4.50');
addPurchaseOrderItemRow();
assert.equal(elements['po-items-container'].children[1].querySelector('.po-item-qty').value, '');
""", HARNESS)


def test_reopening_order_resets_the_bulk_picker():
    run_js(PICKER + ("preparePurchaseOrderModal",), r"""
let manualRows = 0, resets = 0;
globalThis.addPurchaseOrderItemRow = () => {manualRows++};
globalThis.setPurchaseOrderSaving = value => {purchaseOrderSaving=value};
globalThis.makeRemoteInventorySelect = () => ({reset(){resets++}});
poProductPicker.selected.set('1', first);
elements['po-products-search'] = new Element(); elements['po-products-search'].value = 'old';
elements['po-supplier-id'] = new Element(); elements['po-supplier-id'].remoteLookup = {};
preparePurchaseOrderModal();
assert.equal(poProductPicker.selected.size, 0);
assert.equal(elements['po-products-search'].value, '');
assert.equal(manualRows, 1); assert.equal(resets, 1);
reply(0, []); await tick();
""")


def test_save_sends_multiple_lines_together_with_their_edited_quantities_and_costs():
    out = run_po_js(r"""
document.querySelectorAll = () => [
  {querySelector: key => field(key === '.po-item-product' ? '3' : key === '.po-item-qty' ? '6' : '4.50')},
  {querySelector: key => field(key === '.po-item-product' ? '9' : key === '.po-item-qty' ? '2' : '7.25')},
];
let payload;
respond = async (url, options) => {
  payload = JSON.parse(options.body);
  return {ok:true, json:async () => ({success:true})};
};
savePurchaseOrder(); await new Promise(resolve => setTimeout(resolve, 0));
console.log(JSON.stringify({payload, requests:requestCount}));
""", CREATE_FUNCTIONS)
    assert out["requests"] == 1
    assert out["payload"]["items"] == [
        {"product_id": 3, "ordered_qty": 6, "unit_cost": 4.5},
        {"product_id": 9, "ordered_qty": 2, "unit_cost": 7.25},
    ]