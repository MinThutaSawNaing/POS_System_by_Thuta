"""Exercise actual POS cards, offline search, barcode and cart code in Chrome."""
import subprocess
from pathlib import Path

import pytest

from test_delivery_actions_browser import BROWSER
from test_offline_sync_javascript import _function

ROOT = Path(__file__).parent


@pytest.mark.skipif(not BROWSER, reason="Local Chrome/Chromium/Edge required")
def test_product_card_search_and_barcode_work_after_server_disconnect(tmp_path):
    source = (ROOT / "templates/dashboard.html").read_text(encoding="utf-8")
    helpers = "\n".join(_function(source, name) for name in (
        "posCatalogScopeStillCurrent",
        "loadProductsForPOS", "rememberPOSProducts", "posCartScopeCurrent", "posIsOffline",
        "findCachedProduct", "getCachedProducts", "saveCachedProducts", "cachedProductsKey",
        "scopedOfflineCacheKey", "offlineCacheSet", "offlineCacheGet", "addToCart",
        "addToCartToInternal", "posResponseJSON", "searchProducts", "processBarcodeScan",
        "renderProductsFromCacheForPOS",
    ))
    script = r"""
let currentBranch={id:7}, CACHE_USER_ID='offline-cart-browser', CACHE_USER_ROLE='cashier';
const PRODUCT_CACHE_KEY='pos_products_all_cache';
let cachedProducts=[],cachedProductsBranchId=null,productsCacheTimestamp=0;
let dashboardSessionStale=false,dashboardBranchStale=false,branchSwitchInFlight=false;
let checkoutServerUnavailable=false, offline=false,cart=[],currentSection='pos';
const POS_PER_PAGE=50;
let posProductsLoading=false,posProductCursor=null,posProductsHaveMore=false;
let posCategoryFilterId=null,posProductPage=1,posProductTotalPages=1;
let requests=0;
function isBrowserOffline(){return offline;}
function setPOSCatalogNotice(){}function showPOSCatalogError(){}
function showToast(){}function updateCartDisplay(){}
function posGridState(){return '';}
function getProductPhotoHtml(){return '';}
function escapeHtml(value){return String(value);}
function formatCurrency(value){return String(value);}
function posStockHtml(){return '';}
function normalizeCurrencyDisplay(){}
function renderPOSCategoryChips(){}
function toCents(v){return Math.round(Number(v)*100);}
function centsToNumber(v){return v/100;}
const product={id:42,name:"Farmer's Rice",price:25,stock:10,tax_rate:0,unit_symbol:'kg',barcode:'0042',sku:'RICE'};
window.fetch=async()=>{requests++;if(offline)throw TypeError('Disconnected');
  return {ok:true,status:200,json:async()=>({items:[product],has_more:false})};};
async function tick(){for(let i=0;i<8;i++)await new Promise(r=>setTimeout(r,0));}
function check(ok,message){if(!ok)throw Error(message);}
(async()=>{
  localStorage.removeItem(scopedOfflineCacheKey(cachedProductsKey()));
  loadProductsForPOS();await tick();
  check(document.querySelectorAll('.product-card').length===1,'Online card must render');
  offline=true;checkoutServerUnavailable=true;
  document.querySelector('.product-card').click();await tick();
  check(cart.length===1&&cart[0].quantity===1,'Visible card must add offline');
  document.getElementById('product-search').value='RICE';searchProducts();
  check(document.querySelectorAll('.product-card').length===1,'Saved SKU search must render');
  document.querySelector('.product-card').click();await tick();
  await processBarcodeScan('0042');
  check(cart[0].quantity===3,'Offline card, search and barcode must use the same cart item');
  check(cart[0].unit==='kg','Unit metadata must survive offline lookup');
  check(requests===1,'No offline interaction may require server requests');
  // Exercise disk fallback rather than only the warm in-memory snapshot.
  cachedProducts=[];cachedProductsBranchId=null;
  await addToCart(42,'Offline Rice',25,1,0);
  check(cart[0].quantity===4,'Persistent product snapshot must also support offline adds');
  // A full/unavailable disk must not disable the already-open product catalogue.
  localStorage.removeItem(scopedOfflineCacheKey(cachedProductsKey()));
  const originalSetItem=Storage.prototype.setItem;
  Storage.prototype.setItem=function(){throw new DOMException('Storage full','QuotaExceededError');};
  rememberPOSProducts([product],7);
  await addToCart(42,'Offline Rice',25,1,0);
  check(cart[0].quantity===5,'Warm product memory must survive persistent-cache write failure');
  Storage.prototype.setItem=originalSetItem;
  currentBranch={id:8};
  check(getCachedProducts().length===0,'Offline stock must not leak across branches');
  currentBranch={id:7};
  localStorage.removeItem(scopedOfflineCacheKey(cachedProductsKey()));
  document.body.dataset.testResult='passed';
})().catch(error=>{document.body.dataset.testResult='failed';document.body.append(error.stack);});
"""
    page = tmp_path / "offline-cart.html"
    page.write_text(
        '<!doctype html><html><body><input id="product-search"><div id="product-grid"></div>'
        f'<script>{helpers}\n{script}</script></body></html>', encoding="utf-8",
    )
    result = subprocess.run([
        BROWSER, "--headless", "--disable-gpu", "--no-sandbox",
        f"--user-data-dir={tmp_path / 'profile'}", "--virtual-time-budget=3000",
        "--dump-dom", page.as_uri(),
    ], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert 'data-test-result="passed"' in result.stdout, result.stdout[-6000:] + result.stderr[-1000:]