"""Execute the real warehouse transfer modal/retry JavaScript in Node."""

import shutil
import subprocess
from pathlib import Path

import pytest

from test_offline_sync_javascript import _function, _run_node_script


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
SOURCE = (Path(__file__).parent / "templates/dashboard.html").read_text(encoding="utf-8")
TRANSFER = SOURCE.split("// ===== WAREHOUSE TRANSFER RETRY =====", 1)[1].split(
    "// ===== END WAREHOUSE TRANSFER RETRY =====", 1
)[0]

HARNESS = r"""
const assert=require('node:assert/strict');
const fields={}, storage={}, requests=[], toasts=[];
const CACHE_USER_ID=7;
let currentBranch={id:1}, branchesList=[{id:1,name:'One',is_active:true},{id:2,name:'Two',is_active:true}];
let authorized=true, mode='success', release=null, shown=0, hidden=0, refreshed=0;
let failStorage=false;
const sessionStorage={getItem:k=>storage[k]||null,
 setItem:(k,v)=>{if(failStorage)throw Error('quota');storage[k]=v;},
 removeItem:k=>delete storage[k]};
for(const id of ['restock-product-id','restock-product-name','restock-available-qty',
 'restock-main-stock','restock-batch-number','restock-quantity','restock-notes',
 'warehouse-target-branch','warehouseRestockModal','warehouse-transfer-status']){
 fields[id]={value:'',hidden:true,textContent:''};
}
const button={disabled:false,innerHTML:'Transfer to Stock',dataset:{}};
const document={getElementById:id=>fields[id],querySelector:()=>button};
const bootstrap={Modal:class{show(){shown++}static getInstance(){return{hide(){hidden++}}}}};
function requireCapability(){return authorized}
function showToast(message,kind){toasts.push({message,kind})}
function escapeHtml(value){return value}
function invalidateProductsCache(){refreshed++}
function loadWarehouseSummary(){}
function loadWarehouseInventory(){}
function loadProducts(){}
function loadProductsForPOS(){}
async function fetch(url,init){
 requests.push({url,body:JSON.parse(init.body)});
 if(mode==='pending') await new Promise(resolve=>release=resolve);
 if(mode==='network')throw Error('Lost response');
 if(mode==='html')return {ok:true,json:async()=>{throw Error('HTML instead of JSON')}};
 return {status:mode==='validation'?400:mode==='conflict'?409:mode==='server'?500:200,
 ok:!['server','conflict','validation'].includes(mode),json:async()=>({
   success:!['server','conflict','validation','application'].includes(mode),
   transfer_id:42,replayed:mode==='replay',message:mode
 })};
}
function open(){showRestockModal(10,'Product',20,3,'batch-1')}
function fill(qty='4'){fields['restock-quantity'].value=qty;fields['restock-notes'].value=' notes ';}
"""


def run_transfer(body):
    script = HARNESS + "\n" + _function(SOURCE, "generateClientTxnId") + "\n" + TRANSFER
    script += "\n(async()=>{\n" + body + "\nconsole.log(JSON.stringify({passed:true}));\n})().catch(e=>{console.error(e);process.exit(1)});"
    try:
        assert _run_node_script(script)["passed"]
    except subprocess.CalledProcessError as error:
        pytest.fail(error.stdout + error.stderr)


def test_success_new_intent_gets_new_key_and_payload_is_complete():
    run_transfer(r"""
open();fill();await confirmRestock();
assert.equal(requests.length,1);assert.equal(hidden,1);assert.equal(button.disabled,false);
const first=requests[0].body;
assert.deepEqual({...first,idempotency_key:null},{product_id:10,quantity:4,batch_number:'batch-1',
 target_branch_id:'1',notes:'notes',idempotency_key:null});
assert.ok(first.idempotency_key.length>0 && first.idempotency_key.length<=128);
assert.equal(Object.keys(storage).length,0);assert.equal(refreshed,1);
open();fill('5');await confirmRestock();
assert.notEqual(requests[1].body.idempotency_key,first.idempotency_key);
assert.equal(requests[1].body.quantity,5);
""")


@pytest.mark.parametrize("mode", ["network", "server", "conflict", "html", "application"])
def test_uncertain_response_retries_exact_payload_and_key(mode):
    run_transfer("mode=" + repr(mode) + r""";
open();fill();await confirmRestock();
assert.equal(hidden,0);assert.equal(button.disabled,false);assert.equal(warehouseTransferInFlight,false);
assert.ok(fields['warehouse-transfer-status'].textContent.includes('SAME request'));
const pending=readWarehouseTransferRetry();
assert.equal(pending.key,requests[0].body.idempotency_key);
assert.ok(storage[warehouseTransferStorageKey()]);
// The server may already have consumed the stock; allow an exact replay anyway.
fields['restock-available-qty'].value='0';mode='replay';await confirmRestock();
assert.deepEqual(requests[1].body,requests[0].body);
assert.equal(hidden,1);assert.equal(readWarehouseTransferRetry(),null);
assert.equal(fields['warehouse-transfer-status'].hidden,true);
""")


@pytest.mark.parametrize("field,value", [
    ("restock-product-id", "11"), ("restock-quantity", "5"),
    ("warehouse-target-branch", "2"), ("restock-batch-number", "batch-2"),
    ("restock-notes", "changed notes"),
])
def test_changed_payload_is_blocked_until_original_request_is_resolved(field, value):
    run_transfer(r"""
open();fill();mode='network';await confirmRestock();
const key=readWarehouseTransferRetry().key;
""" + f"fields[{field!r}].value={value!r};" + r"""
await confirmRestock();assert.equal(requests.length,1);
assert.ok(fields['warehouse-transfer-status'].textContent.includes('Reopen Transfer'));
assert.equal(readWarehouseTransferRetry().key,key);
// Closing then opening a different product must restore the unresolved intent.
showRestockModal(99,'Another product',100,0,'other-batch');
assert.equal(Number(fields['restock-product-id'].value),10);
assert.equal(Number(fields['restock-quantity'].value),4);
assert.equal(fields['warehouse-target-branch'].value,'1');
assert.equal(fields['restock-batch-number'].value,'batch-1');
assert.equal(fields['restock-notes'].value,'notes');
mode='replay';await confirmRestock();
assert.deepEqual(requests[1].body,requests[0].body);
""")


def test_inflight_double_submit_and_modal_reset_are_blocked():
    run_transfer(r"""
open();fill();mode='pending';
const pending=confirmRestock();
await confirmRestock();showRestockModal(99,'Other',200,0,'other');
assert.equal(requests.length,1);assert.equal(warehouseTransferInFlight,true);
assert.equal(button.disabled,true);assert.equal(Number(fields['restock-product-id'].value),10);
assert.equal(shown,1);release();await pending;
assert.equal(button.disabled,false);assert.equal(warehouseTransferInFlight,false);
""")


@pytest.mark.parametrize("reopen", [False, True])
def test_definitive_validation_rejection_allows_corrected_new_intent(reopen):
    run_transfer(r"""
open();fill();mode='validation';await confirmRestock();
assert.equal(readWarehouseTransferRetry().rejected,true);
assert.ok(fields['warehouse-transfer-status'].textContent.includes('rejected without completion'));
const first=requests[0].body;
""" + ("open();" if reopen else "") + r"""
fill('5');mode='success';await confirmRestock();
assert.equal(requests.length,2);assert.equal(requests[1].body.quantity,5);
assert.notEqual(requests[1].body.idempotency_key,first.idempotency_key);
""")


def test_retry_survives_reloaded_state_and_is_scoped_to_original_user():
    run_transfer(r"""
open();fill();mode='network';await confirmRestock();
const first=requests[0].body;
// These helpers read sessionStorage, not a previous modal's in-memory key.
for(const field of Object.values(fields))field.value='';
warehouseTransferInFlight=false;currentBranch={id:2};
showRestockModal(99,'Different',500,0,'different');
assert.equal(fields['warehouse-target-branch'].value,'1');
assert.equal(warehouseTransferStorageKey(),'pos_transfer_retry_v1:7');
mode='success';await confirmRestock();assert.deepEqual(requests[1].body,first);
""")


@pytest.mark.parametrize("qty", ["", "0", "-1", "1.5", "2abc", "21", "Infinity"])
def test_invalid_quantities_do_not_allocate_key_or_send(qty):
    run_transfer(f"open();fill({qty!r});await confirmRestock();" + r"""
assert.equal(requests.length,0);assert.equal(readWarehouseTransferRetry(),null);
assert.equal(button.disabled,false);
""")


def test_storage_failure_or_corruption_fails_closed_before_network():
    run_transfer(r"""
open();fill();failStorage=true;await confirmRestock();
assert.equal(requests.length,0);assert.equal(button.disabled,false);
failStorage=false;storage[warehouseTransferStorageKey()]='not JSON';
await confirmRestock();assert.equal(requests.length,0);
showRestockModal(99,'Other',200,0,'other');assert.equal(shown,1);
""")


def test_permission_denied_does_not_touch_transfer_or_retry():
    run_transfer(r"""
authorized=false;open();await confirmRestock();
assert.equal(requests.length,0);assert.equal(shown,0);assert.deepEqual(storage,{});
""")