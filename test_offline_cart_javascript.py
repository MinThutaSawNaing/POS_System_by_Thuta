"""Real POS page -> cached product -> offline cart regression scenarios."""
import shutil
from pathlib import Path

import pytest

from test_offline_sync_javascript import _function, _run_node_script

ROOT = Path(__file__).parent
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node required")


def run_cart(body):
    source = (ROOT / "templates/dashboard.html").read_text(encoding="utf-8")
    names = ["posCatalogScopeStillCurrent", "loadProductsForPOS", "findCachedProduct", "addToCart", "addToCartToInternal", "posResponseJSON", "searchProducts", "processBarcodeScan"]
    # Include new production helpers once implemented; initial run must exercise
    # the old page loader, which never saves displayed products for offline use.
    for name in ["rememberPOSProducts", "posCartScopeCurrent", "posIsOffline"]:
        if f"function {name}(" in source:
            names.append(name)
    helpers = "\n".join(_function(source, name) for name in names)
    return _run_node_script(r"""
const assert = require('node:assert/strict');
console.log = () => {};
let currentBranch = {id:1}, CACHE_USER_ID = 7, CACHE_USER_ROLE = 'cashier';
let dashboardSessionStale = false, dashboardBranchStale = false, branchSwitchInFlight = false;
let offline = false, checkoutServerUnavailable = false, snapshot = [], cart = [];
let cachedProducts = [], cachedProductsBranchId = null, productsCacheTimestamp = 0;
const storage = {}, notices = [], requests = [];
const POS_PER_PAGE = 50;
let posProductsLoading = false, posProductCursor = null, posProductsHaveMore = false;
let posCategoryFilterId = null, posProductPage = 1;
const grid = {innerHTML:'', children:[], appendChild(c){this.children.push(c)}, querySelector(){return null}};
const search = {value:'', focus(){}};
const document = {getElementById:id=>id==='product-search'?search:grid,
  createElement:()=>({innerHTML:'',className:''})};
function getCachedProducts(){return snapshot;}
function cachedProductsKey(){return 'products_1';}
function offlineCacheGet(){return null;}
function saveCachedProducts(rows){snapshot=JSON.parse(JSON.stringify(rows));}
function isBrowserOffline(){return offline;}
function hasCapability(){return true;}
function setPOSCatalogNotice(){}
function showPOSCatalogError(){}
function posGridState(){return '';}
function renderProductsFromCacheForPOS(rows){grid.offlineRows=rows;}
function escapeHtml(v){return String(v);}
function getProductPhotoHtml(){return '';}
function formatCurrency(v){return String(v);}
function posStockHtml(){return '';}
function normalizeCurrencyDisplay(){}
function updateCartDisplay(){}
let currentSection = 'pos';
function showSection(section){currentSection=section;}
function renderPOSCategoryChips(){}
function showToast(message,kind){notices.push({message,kind});}
function showConfirmDialog(){throw Error('unexpected stock confirmation');}
function toCents(v){return Math.round(Number(v)*100);}
function centsToNumber(v){return v/100;}
const item = {id:41,name:'Rice',price:12.50,stock:3,tax_rate:5,unit_symbol:'kg',barcode:'0041'};
let network = async()=>({ok:true,status:200,json:async()=>({items:[item],has_more:false})});
async function fetch(url,init){requests.push(String(url));return network(url,init);}
async function tick(){for(let i=0;i<8;i++)await new Promise(r=>setTimeout(r,0));}
""" + helpers + "\n(async()=>{\n" + body + r"""
process.stdout.write(JSON.stringify({passed:true}));
})().catch(e=>{console.error(e);process.exit(1)});
""")


def test_products_visible_online_can_be_added_without_network_when_offline():
    run_cart(r"""
loadProductsForPOS(); await tick();
offline = true;
network = async()=>{throw TypeError('Failed to fetch');};
const before = requests.length;
await addToCart(41,'Rice',12.5,1,5); await tick();
assert.equal(cart.length,1,'Displayed online products must be saved for offline cart use');
assert.equal(cart[0].quantity,1);
assert.equal(cart[0].unit,'kg');
assert.equal(requests.length,before,'Known offline state must not wait for network');
""")


def test_server_outage_allows_cached_additions_without_waiting_for_browser_offline():
    run_cart(r"""
snapshot = [item]; checkoutServerUnavailable = true;
network=async()=>{throw Error('must not contact unreachable server');};
await addToCart(41,'Rice',12.5,1,5); await tick();
assert.equal(cart.length,1); assert.equal(requests.length,0);
""")


@pytest.mark.parametrize("status", [401, 403, 404, 409])
def test_definite_server_refusal_never_falls_back_to_cached_product(status):
    run_cart(f"""
snapshot=[item]; network=async()=>({{ok:false,status:{status},json:async()=>({{}})}});
await addToCart(41,'Rice',12.5,1,5); await tick();
assert.equal(cart.length,0,'A server refusal must never become an offline addition');
""")


def test_products_loaded_while_branch_identity_resolves_still_render():
    """A cashier landing on POS loads products before /api/branches/current returns.

    The grid must accept that response: the server already scoped it to the
    session branch. Rejecting it left the cashier on \"Loading products\" until a
    manual reload because the branch arrived while the request was in flight.
    """
    run_cart(r"""
let release;
currentBranch = null;
network = () => new Promise(resolve => release = resolve);
loadProductsForPOS();
currentBranch = {id: 1};
release({ok:true,status:200,json:async()=>({items:[item],has_more:false,next_cursor:null})});
await tick();
assert.equal(grid.children.length, 1, 'the first product page must render');
assert.equal(snapshot.length, 1, 'the rendered page must also be saved for offline use');
assert.equal(posProductsLoading, false);
""")


def test_product_request_from_old_branch_is_still_discarded():
    run_cart(r"""
let release;
currentBranch = {id:1};
network = () => new Promise(resolve => release = resolve);
loadProductsForPOS();
currentBranch = {id:2};
release({ok:true,status:200,json:async()=>({items:[item],has_more:false,next_cursor:null})});
await tick();
assert.equal(grid.children.length, 0, 'a response from the previous branch must not render');
""")


@pytest.mark.parametrize("quantity", [0, -1, 4, float("inf")])
def test_offline_cart_rejects_invalid_or_excess_stock(quantity):
    literal = "Infinity" if quantity == float("inf") else str(quantity)
    run_cart(f"""
snapshot=[item]; offline=true;network=async()=>{{throw TypeError('offline');}};
await addToCart(41,'Rice',12.5,{literal},5); await tick();
assert.equal(cart.length,0,'Invalid quantities cannot enter an offline cart');
""")


def test_product_request_from_old_branch_cannot_modify_new_cart():
    run_cart(r"""
let release;
network=()=>new Promise(resolve=>release=resolve);
const pending=addToCart(41,'Rice',12.5,1,5);
currentBranch={id:2};
release({ok:true,status:200,json:async()=>item});
await pending; await tick();
assert.equal(cart.length,0,'An old branch response cannot add products to the new branch');
""")


def test_offline_search_barcode_and_paging_never_need_network():
    run_cart(r"""
snapshot=[item]; offline=true;
network=async()=>{throw Error('must not use network');};
search.value='0041'; searchProducts();
assert.equal(grid.offlineRows[0].id,41);
search.value='missing'; searchProducts();
assert.equal(grid.offlineRows.length,0);
await processBarcodeScan('0041'); await tick();
assert.equal(cart[0].quantity,1);
search.value='Rice';
loadProductsForPOS(false);
assert.equal(posProductPage,2,'Offline Load More must advance its local page');
assert.equal(requests.length,0);
""")


def test_search_results_are_saved_and_remain_addable_after_disconnection():
    run_cart(r"""
network=async()=>({ok:true,status:200,json:async()=>[item]});
search.value='Rice'; searchProducts(); await tick();
assert.equal(snapshot[0].id,41,'Search must save its results for offline use');
offline=true;
await addToCart(41,'Rice',12.5,1,5);
assert.equal(cart[0].quantity,1);
""")


def test_cart_combines_string_and_number_ids_and_enforces_aggregate_stock():
    run_cart(r"""
snapshot=[item]; offline=true;
await addToCart('41','Rice',12.5,2,5);
await addToCart(41,'Rice',12.5,2,5);
assert.equal(cart.length,1);assert.equal(cart[0].quantity,2);
""")


def test_saved_products_merge_pages_without_duplicates_and_ignore_old_branch():
    run_cart(r"""
rememberPOSProducts([item],1);
rememberPOSProducts([{...item,id:42,name:'Beans'}],1);
rememberPOSProducts([{...item,stock:2}],1);
assert.equal(snapshot.length,2);assert.equal(snapshot.find(p=>p.id===41).stock,2);
rememberPOSProducts([{...item,id:99}],2);
assert.equal(snapshot.length,2);
""")


def test_offline_snapshot_survives_mutation_invalidation():
    from test_section_cache_javascript import run_cache_tests

    run_cache_tests("""
  let deleted = 0;
  offlineCacheRemove = () => { deleted++; };
  invalidateProductsCache();
  check('invalidating live stock must not delete the last offline snapshot', deleted === 0);
""")


def test_server_failure_uses_snapshot_but_failed_scope_does_not():
    run_cart(r"""
snapshot=[item];network=async()=>({ok:false,status:503});
await addToCart(41,'Rice',12.5,1,5);
assert.equal(cart[0].quantity,1);
dashboardSessionStale=true;offline=true;
await addToCart(41,'Rice',12.5,1,5);
assert.equal(cart[0].quantity,1,'Stale session must never add cached stock');
""")