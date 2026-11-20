"""Cashier Hold and Resume actions on the POS dashboard."""
import pytest

from test_offline_sync_javascript import DASHBOARD, NODE, _function, _run_node_script

pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is required")


def run_pos(body, functions=()):
    source = DASHBOARD.read_text(encoding="utf-8")
    start = source.index("// ===== POS HELD SALES =====")
    end = source.index("// ===== END POS HELD SALES =====", start)
    return _run_node_script(r"""
const assert = require('node:assert/strict');
let cart = [], appliedCoupon = null, currentBranch = {id: 1}, CACHE_USER_ID = 5;
let dashboardSessionStale = false, dashboardBranchStale = false, branchSwitchInFlight = false;
let checkoutPreflight = false, isSaleProcessing = false, mobile = {}, failWrite = false;
const MyanMyanPayCheckout = {state: () => mobile};
const saved = new Map(), messages = [];
const sessionStorage = {
  getItem: key => saved.get(key) ?? null,
  setItem(key, value) { if (failWrite) throw Error('quota'); saved.set(key, value); },
};
const nodes = new Map();
function node() {
  return { value:'', checked:false, style:{}, options:[{value:''},{value:'3'}], children:[],
    append(...children){this.children.push(...children);}, replaceChildren(){this.children=[];},
    appendChild(child){this.options.push(child);},
    addEventListener(name,handler){this[name]=handler;}, setAttribute(){}, dispatchEvent(){},
    toggleAttribute(){}, focus(){}, classList:{contains:()=>false,toggle(){}} };
}
const document = {getElementById(id){if(!nodes.has(id)) nodes.set(id,node()); return nodes.get(id);}, createElement:node};
const field = id => document.getElementById(id);
const item = (name, id=1) => ({name, product_id:id, quantity:2, price:10, tax_rate:0});
function showToast(message, kind){messages.push({message,kind});}
function updateCartDisplay(){}
function resetAppliedCoupon(){appliedCoupon=null;}
function resetPOSCustomer(){field('pos-customer-select').value='';}
function resetSplitPaymentFields(){field('split-amount-one').value='';}
function handlePaymentMethodChange(){}
function clearLegacyMMQRDeadline(){}
function getCartSubtotalCents(){return 2000;}
function setCouponFeedback(){}
function posIsOffline(){return false;}
function posResponseJSON(response){return response.json();}
let finishRequest;
function fetch(){return new Promise(resolve=>{finishRequest=resolve;});}
""" + "\n".join(_function(source, name) for name in (
        "posCartScopeCurrent", "togglePosDeliveryFields", "ensurePOSCustomerOption",
    )) + "\n" +
        source[start:end] + "\n" + "\n".join(_function(source, name) for name in functions) +
        "\n(async()=>{\n" + body + '\nprocess.stdout.write(JSON.stringify({passed:true}));\n'
        '})().catch(error=>{console.error(error);process.exit(1);});')


def test_cashier_holds_first_sale_rings_next_and_resumes_first():
    source = DASHBOARD.read_text(encoding="utf-8")
    assert 'id="pos-hold-sale"' in source and 'onclick="holdPOSSale()"' in source
    run_pos(r"""
cart = [item('Rice')];
appliedCoupon = {code:'SAVE',discountType:'fixed',discountValue:2,minSubtotalCents:0};
field('pos-customer-select').value = '3';
field('pos-enable-delivery').checked = true;
field('pos-delivery-address').value = 'First address';
field('pos-delivery-fee').value = '5';
field('payment-method').value = 'split_payment';
field('split-amount-one').value = '3';
field('cash-received').value = '20';
holdPOSSale();
assert.deepEqual(cart, []);
assert.equal(appliedCoupon, null);
assert.equal(field('pos-customer-select').value, '');
assert.equal(field('pos-enable-delivery').checked, false);
assert.equal(field('pos-delivery-address').value, '');
assert.equal(field('cash-received').value, '');
assert.equal(field('split-amount-one').value, '');
assert.equal(field('payment-method').value, 'cash');
assert.equal(field('pos-held-sales-count').textContent, '1');

cart = [item('Tea', 2)];
const resume = () => field('pos-held-sales-list').children[0].children[1].click();
resume();
assert.equal(cart[0].name, 'Tea');
assert.equal(field('pos-held-sales-count').textContent, '1');
cart = [];  // The next sale has finished.
field('pos-customer-select').options = [{value:''}]; // List unavailable after reload.
renderPOSHeldSales();
resume();
assert.deepEqual(cart, [item('Rice')]);
assert.equal(appliedCoupon.code, 'SAVE');
assert.equal(field('pos-customer-select').value, '3');
assert.match(field('pos-customer-select').options[1].textContent, /Saved customer #3/);
assert.equal(field('pos-enable-delivery').checked, true);
assert.equal(field('pos-delivery-address').value, 'First address');
assert.equal(field('pos-delivery-fee').value, '5');
assert.equal(field('cash-received').value, '');
assert.equal(field('payment-method').value, 'cash');
assert.equal(field('pos-held-sales-count').textContent, '0');
""")


def test_multiple_drafts_stay_with_their_cashier_and_branch():
    run_pos(r"""
cart = [item('Rice')]; holdPOSSale();
cart = [item('Tea', 2)]; holdPOSSale();
const heldCount = () => field('pos-held-sales-count').textContent;
assert.equal(heldCount(), '2');
const resumeTea = field('pos-held-sales-list').children[0].children[1].click;
currentBranch = {id:2}; renderPOSHeldSales();
assert.equal(heldCount(), '0');
resumeTea(); assert.deepEqual(cart, []);
currentBranch = {id:1}; CACHE_USER_ID = 8; renderPOSHeldSales();
assert.equal(heldCount(), '0');
resumeTea(); assert.deepEqual(cart, []);
CACHE_USER_ID = 5; renderPOSHeldSales();
assert.equal(heldCount(), '2');
field('pos-held-sales-list').children[0].children[1].click();
assert.deepEqual(cart, [item('Tea', 2)]);
assert.equal(heldCount(), '1');
assert.match(field('pos-held-sales-list').children[0].children[0].children[0].textContent, /Rice/);
""")


def test_storage_failure_and_pending_payment_never_move_a_cart():
    run_pos(r"""
cart = [item('Rice')];
failWrite = true; holdPOSSale();
assert.equal(cart[0].name, 'Rice');
assert.match(messages.at(-1).message, /cart was kept/);
failWrite = false;
checkoutPreflight = true; holdPOSSale();
assert.equal(cart[0].name, 'Rice');
checkoutPreflight = false; mobile = {active:{status:'PENDING'}};
holdPOSSale(); assert.equal(cart[0].name, 'Rice');
mobile = {}; holdPOSSale();
const resume = field('pos-held-sales-list').children[0].children[1].click;
isSaleProcessing = true; resume(); assert.deepEqual(cart, []);
isSaleProcessing = false; failWrite = true; resume();
assert.deepEqual(cart, []);
assert.equal(field('pos-held-sales-count').textContent, '1');
assert.match(messages.at(-1).message, /held sale was kept/);
failWrite = false; resume(); assert.deepEqual(cart, [item('Rice')]);
""")


def test_late_product_and_coupon_responses_do_not_leak_into_the_new_sale():
    run_pos(r"""
cart = [item('Rice')];
const add = addToCart(2, 'Tea', 10, 1, 0);
const finishProduct = finishRequest;
field('pos-coupon-code').value = 'SAVE';
applyCouponCode();
const finishCoupon = finishRequest;
holdPOSSale();
cart.push(item('Sugar', 3));
finishProduct({json:async()=>({id:2,name:'Tea',price:10,stock:10})});
finishCoupon({ok:true,json:async()=>({success:true,code:'SAVE',discount_value:2})});
assert.equal(await add, false);
await new Promise(setImmediate);
assert.deepEqual(cart, [item('Sugar', 3)]);
assert.equal(appliedCoupon, null);
""", functions=("addToCart", "applyCouponCode"))