"""Price Check reads branch prices without changing the live POS sale."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_offline_sync_javascript import _function


SOURCE = (Path(__file__).parent / "templates" / "dashboard.html").read_text(encoding="utf-8")
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js required")


def run_price_check(body):
    helpers = "\n".join(_function(SOURCE, name) for name in (
        "setPOSPriceCheckOpen", "checkPOSPrice",
    ))
    script = """
const assert = require('node:assert/strict');
let cart = [{name:'Existing sale'}], currentBranch = {id: 2};
let dashboardSessionStale = false, dashboardBranchStale = false, branchSwitchInFlight = false;
let offline = false, cached = [], requests = [], posPriceCheckRequest = 0;
function posCartScopeCurrent(id) { return id === currentBranch.id && !dashboardSessionStale && !dashboardBranchStale && !branchSwitchInFlight; }
function posIsOffline() { return offline; }
function getCachedProducts() { return cached; }
function formatCurrency(value) { return Number(value).toFixed(2) + ' MMK'; }
function posResponseJSON(response) { if (!response.ok) { const error = Error('Request failed'); error.status=response.status; throw error; } return response.json(); }
function element() { return {hidden:false, value:'', textContent:'', children:[], attributes:{},
  focus(){}, setAttribute(key,value){this.attributes[key]=value;}, replaceChildren(){this.children=[];},
  append(...items){this.children.push(...items);}, addEventListener(key,fn){this[key]=fn;},
  closest(selector){return selector === '[hidden]' && nodes['pos-price-check'].hidden ? nodes['pos-price-check'] : null;}}; }
const nodes = Object.fromEntries(['pos-price-check','pos-price-check-toggle','pos-price-check-query',
  'pos-price-check-status','pos-price-check-results'].map(id => [id,element()]));
nodes['pos-price-check'].hidden = true;
const document = {getElementById:id=>nodes[id],createElement:element};
function field(id) { return nodes[id]; }
function fetch(url) { return new Promise(resolve => requests.push({url,resolve})); }
""" + helpers + "\n(async () => {\n" + body + "\nconsole.log(JSON.stringify({cart, status:field('pos-price-check-status').textContent, results:field('pos-price-check-results').children.map(button => ({name:button.children[0].children[0].textContent, price:button.children[1].textContent}))}));\n})().catch(e => { console.error(e); process.exit(1); });"
    result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_price_check_searches_active_branch_and_never_adds_to_cart():
    result = run_price_check("""
setPOSPriceCheckOpen(true);
field('pos-price-check-query').value = ' 0007 ';
const done = checkPOSPrice();
assert.equal(requests[0].url, '/api/products/search?q=0007&branch_id=2');
requests[0].resolve({ok:true,json:async()=>[
  {name:'Other',price:5,barcode:'123'}, {name:'Tea',price:12.5,barcode:'0007'}]});
await done;
assert.equal(field('pos-price-check-toggle').attributes['aria-expanded'],'true');
field('pos-price-check-results').children[0].click();
""")
    assert result["cart"] == [{"name": "Existing sale"}]
    assert result["results"] == [{"name": "Tea", "price": "12.50 MMK"}, {"name": "Other", "price": "5.00 MMK"}]
    assert "Tea: 12.50 MMK" in result["status"]


def test_offline_results_are_marked_saved_and_scoped():
    result = run_price_check("""
setPOSPriceCheckOpen(true);
offline = true;
cached = [{name:'Tea',sku:'T1',price:8},{name:'Rice',sku:'R1',price:9}];
field('pos-price-check-query').value = 'T1';
await checkPOSPrice();
assert.equal(requests.length,0);
""")
    assert result["results"] == [{"name": "Tea", "price": "8.00 MMK"}]
    assert "Saved prices" in result["status"]


def test_late_response_cannot_overwrite_closed_or_other_branch_results():
    result = run_price_check("""
setPOSPriceCheckOpen(true);
field('pos-price-check-query').value = 'Tea';
const old = checkPOSPrice();
setPOSPriceCheckOpen(false);
requests[0].resolve({ok:true,json:async()=>[{name:'Tea',price:1}]});
await old;
setPOSPriceCheckOpen(true);
const next = checkPOSPrice();
currentBranch = {id:3};
requests[1].resolve({ok:true,json:async()=>[{name:'Tea',price:2}]});
await next;
""")
    assert result["results"] == []


def test_authorization_failure_never_uses_cached_price():
    result = run_price_check("""
setPOSPriceCheckOpen(true);
cached = [{name:'Tea',price:1}];
field('pos-price-check-query').value = 'Tea';
const done = checkPOSPrice();
requests[0].resolve({ok:false,status:403});
await done;
""")
    assert result["results"] == []
    assert "access changed" in result["status"]


def test_connection_failure_marks_cached_price_as_saved():
    result = run_price_check("""
setPOSPriceCheckOpen(true);
cached = [{name:'Tea',price:7}];
field('pos-price-check-query').value = 'Tea';
const done = checkPOSPrice();
requests[0].resolve({ok:false,status:503});
await done;
""")
    assert result["results"] == [{"name": "Tea", "price": "7.00 MMK"}]
    assert "Saved prices (may be outdated)" in result["status"]


def test_new_query_supersedes_earlier_results():
    result = run_price_check("""
setPOSPriceCheckOpen(true);
field('pos-price-check-query').value = 'Tea';
const first = checkPOSPrice();
field('pos-price-check-query').value = 'Rice';
const second = checkPOSPrice();
requests[1].resolve({ok:true,json:async()=>[{name:'Rice',price:9}]});
await second;
requests[0].resolve({ok:true,json:async()=>[{name:'Tea',price:3}]});
await first;
""")
    assert result["results"] == [{"name": "Rice", "price": "9.00 MMK"}]


def test_price_check_scanner_input_is_not_redirected_to_cart():
    scanner = SOURCE[SOURCE.index("function initGlobalBarcodeScanner() {"):SOURCE.index("// Process scanned barcode")]
    assert 'activeElement?.id === "pos-price-check-query"' in scanner
    assert scanner.index('activeElement?.id === "pos-price-check-query"') < scanner.index('processBarcodeScan(barcodeBuffer)')