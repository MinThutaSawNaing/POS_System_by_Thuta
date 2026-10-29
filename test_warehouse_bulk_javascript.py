"""Execute warehouse selection and durable bulk transfers without application data."""
import shutil
import subprocess
from pathlib import Path

import pytest

from test_offline_sync_javascript import _function, _run_node_script

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js required")
SOURCE = (Path(__file__).parent / "templates/dashboard.html").read_text(encoding="utf-8")

HARNESS = r"""
const assert = require('node:assert/strict');
const fields = {}, storage = {}, requests = [], toasts = [];
let currentBranch = {id:1}, dashboardBranchStale = false, dashboardSessionStale = false,
 branchSwitchInFlight = false, authorized = true, failStorage = false, mode = 'success', release;
const CACHE_USER_ID = 7, CACHE_USER_ROLE = 'manager';
const branchesList = [{id:1,name:'One',is_active:true},{id:2,name:'Two',is_active:true}];
class Element {
 constructor(){this.value='';this.children=[];this.listeners={};this.dataset={};this.disabled=false;}
 set innerHTML(html){this.html=html;this.children=[];}
 get innerHTML(){return this.html||'';}
 appendChild(el){this.children.push(el);}
 querySelector(selector){return (this.parts ||= {})[selector] ||= new Element();}
 addEventListener(name,fn){this.listeners[name]=fn;}
 setAttribute(){}
}
const document = {getElementById:id=>fields[id] ||= new Element(), createElement:()=>new Element(),
 querySelector:()=>new Element()};
const sessionStorage = {getItem:k=>storage[k]||null,setItem:(k,v)=>{
 if(failStorage)throw Error('quota');storage[k]=v;},removeItem:k=>{if(failStorage)throw Error('quota');delete storage[k];}};
const bootstrap = {Modal:class {show(){} static getInstance(){return {hide(){}}}}};
function requireCapability(){return authorized;}
function hasCapability(){return authorized;}
function showToast(message,kind){toasts.push({message,kind});}
function escapeHtml(value){return String(value||'').replace(/</g,'&lt;').replace(/"/g,'&quot;');}
function formatCurrency(value){return '$'+value;}
let refreshed=0;
function invalidateProductsCache(){refreshed++;}
function loadWarehouseSummary(){} function loadProducts(){} function loadProductsForPOS(){}
function loadWarehouseInventory(){}
async function fetch(url,init){
 const body=JSON.parse(init.body);requests.push(body);
 if(mode==='pending')await new Promise(resolve=>release=resolve);
 if(mode==='network' || (mode==='second-network' && body.product_id===20))throw Error('lost response');
 const status=mode==='reject'?400:mode==='conflict'?409:200;
 return {ok:status===200,status,json:async()=>({success:status===200,message:mode})};
}
function item(id,qty=10){return {id,product_id:id,product_name:'Product '+id,quantity:qty,batch_number:'batch-'+id};}
function select(items=[item(10),item(20)]){
 syncWarehouseSelectionIdentity();warehousePageItems=items;
 toggleWarehousePageSelection(true);
}
function quantities(values=[2,3]){
 fields['warehouse-bulk-items'].children.forEach((row,i)=>row.querySelector('input').value=String(values[i]));
}
"""


def run_bulk(body, extra=""):
    bulk = SOURCE.split("// ===== WAREHOUSE BULK TRANSFER =====", 1)[1].split(
        "// ===== END WAREHOUSE BULK TRANSFER =====", 1)[0]
    transfer = SOURCE.split("// ===== WAREHOUSE TRANSFER RETRY =====", 1)[1].split(
        "// ===== END WAREHOUSE TRANSFER RETRY =====", 1)[0]
    script = HARNESS + "\n" + _function(SOURCE, "inventoryIdentity") + "\n"
    script += _function(SOURCE, "generateClientTxnId") + "\n" + transfer + "\n" + bulk + extra
    script += "\n(async()=>{" + body + "\nconsole.log(JSON.stringify({passed:true}));})().catch(e=>{console.error(e);process.exit(1)});"
    try:
        assert _run_node_script(script)["passed"]
    except subprocess.CalledProcessError as error:
        pytest.fail(error.stdout + error.stderr)


def test_page_selection_persists_across_pages_and_clears_on_identity_change():
    run_bulk(r"""
select();assert.equal(warehouseSelection.size,2);
warehousePageItems=[item(30)];syncWarehouseSelectionUi();
assert.equal(fields['warehouse-select-page'].checked,false);
toggleWarehousePageSelection(true);assert.equal(warehouseSelection.size,3);
warehousePageItems=[item(10),item(20)];toggleWarehousePageSelection(false);
assert.deepEqual([...warehouseSelection.keys()],['30']);
currentBranch={id:2};syncWarehouseSelectionIdentity();assert.equal(warehouseSelection.size,0);
select();dashboardSessionStale=true;syncWarehouseSelectionIdentity();assert.equal(warehouseSelection.size,0);
""")


def test_sequential_success_persists_before_send_and_clears_selection():
    run_bulk(r"""
select();showWarehouseBulkTransferModal();quantities();
fields['warehouse-bulk-notes'].value=' notes ';fields['warehouse-bulk-branch'].value='2';
mode='pending';const pending=confirmWarehouseBulkTransfer();
assert.equal(requests.length,1);const queue=readWarehouseBulkQueue();
assert.equal(queue.items.length,2);assert.equal(queue.items[0].key,requests[0].idempotency_key);
await confirmWarehouseBulkTransfer();showRestockModal(99,'Other',10,0,'batch');
assert.equal(requests.length,1);
mode='success';release();await pending;
assert.equal(requests.length,2);assert.notEqual(requests[0].idempotency_key,requests[1].idempotency_key);
assert.deepEqual(requests.map(r=>r.quantity),[2,3]);
assert.ok(requests.every(r=>r.target_branch_id==='2'&&r.notes==='notes'));
assert.equal(readWarehouseBulkQueue(),null);assert.equal(warehouseSelection.size,0);
""")


def test_partial_failure_reload_restores_immutable_queue_and_replays_same_key():
    run_bulk(r"""
select();showWarehouseBulkTransferModal();quantities();mode='second-network';
await confirmWarehouseBulkTransfer();
assert.equal(readWarehouseBulkQueue().items[0].done,true);
const failed=requests[1];warehouseSelection.clear();warehouseBulkDraft=[];
currentBranch={id:2};showWarehouseBulkTransferModal(true);
fields['warehouse-bulk-notes'].value='tampered';
fields['warehouse-bulk-branch'].value='2';quantities([9,9]);
mode='success';await confirmWarehouseBulkTransfer();
assert.equal(requests.length,3);assert.deepEqual(requests[2],failed);
assert.equal(readWarehouseBulkQueue(),null);
""")


@pytest.mark.parametrize("qty", ["", "0", "-1", "1.5", "11", "2abc"])
def test_invalid_quantity_prevents_entire_queue(qty):
    run_bulk("select();showWarehouseBulkTransferModal();quantities([2," + repr(qty) + r"]);" + r"""
await confirmWarehouseBulkTransfer();assert.equal(requests.length,0);assert.equal(readWarehouseBulkQueue(),null);
""")


def test_storage_failure_corruption_permission_and_stale_guards_fail_closed():
    run_bulk(r"""
select();showWarehouseBulkTransferModal();quantities();
failStorage=true;await confirmWarehouseBulkTransfer();assert.equal(requests.length,0);
failStorage=false;authorized=false;await confirmWarehouseBulkTransfer();assert.equal(requests.length,0);
authorized=true;dashboardBranchStale=true;await confirmWarehouseBulkTransfer();assert.equal(requests.length,0);
dashboardBranchStale=false;currentBranch={id:2};await confirmWarehouseBulkTransfer();assert.equal(requests.length,0);
storage[warehouseBulkStorageKey()]='broken';showWarehouseBulkTransferModal(true);
await confirmWarehouseBulkTransfer();assert.equal(requests.length,0);
""")


def test_stale_identity_mid_queue_stops_remaining_and_preserves_progress():
    run_bulk(r"""
select();showWarehouseBulkTransferModal();quantities();mode='pending';
const pending=confirmWarehouseBulkTransfer();dashboardBranchStale=true;mode='success';release();await pending;
assert.equal(requests.length,1);assert.equal(readWarehouseBulkQueue().items[0].done,true);
assert.equal(readWarehouseBulkQueue().items[1].done,false);
""")


def test_definitive_rejection_can_be_skipped_but_ambiguous_failure_cannot():
    run_bulk(r"""
select();showWarehouseBulkTransferModal();quantities();mode='conflict';await confirmWarehouseBulkTransfer();
skipRejectedWarehouseBulkItem();assert.equal(readWarehouseBulkQueue().items[0].done,false);
mode='reject';await confirmWarehouseBulkTransfer();assert.equal(readWarehouseBulkQueue().items[0].rejected,true);
skipRejectedWarehouseBulkItem();assert.equal(readWarehouseBulkQueue().items[0].skipped,true);
mode='success';await confirmWarehouseBulkTransfer();assert.equal(requests.length,3);
assert.equal(requests[2].product_id,20);assert.equal(readWarehouseBulkQueue(),null);
""")


def test_pending_single_transfer_must_be_reconciled_before_bulk():
    run_bulk(r"""
sessionStorage.setItem(warehouseTransferStorageKey(),JSON.stringify({key:'single',payload:{},view:{}}));
select();showWarehouseBulkTransferModal();await confirmWarehouseBulkTransfer();
assert.equal(requests.length,0);assert.equal(readWarehouseBulkQueue(),null);
""")


def test_real_loader_bounded_paging_filters_and_stale_responses_preserve_selection():
    helpers = "\n".join(_function(SOURCE, name) for name in (
        "beginInventoryPage", "renderInventoryPagination", "loadWarehouseInventory"))
    run_bulk(r"""
const pending=[];
fetch=(url)=>new Promise((resolve,reject)=>pending.push({url,resolve,reject}));
const reply=(i,items,more=false)=>pending[i].resolve({ok:true,json:async()=>({items,has_more:more,next_cursor:more?'signed-next':null})});
const first=loadWarehouseInventory();reply(0,[item(10),item(20)],true);await first;
assert.deepEqual(Object.fromEntries(new URL(pending[0].url,'https://test').searchParams),{view:'register',per_page:'30'});
toggleWarehousePageSelection(true);assert.equal(warehouseSelection.size,2);
assert.equal(fields['warehouse-table'].children[0].querySelector('.warehouse-row-select').checked,true);
const oldRow=fields['warehouse-table'].children[0];
fields['warehouse-pagination'].children[0].children[2].listeners.click();
assert.equal(new URL(pending[1].url,'https://test').searchParams.get('cursor'),'signed-next');
reply(1,[item(30)]);await new Promise(resolve=>setImmediate(resolve));
toggleWarehousePageSelection(true);assert.equal(warehouseSelection.size,3);
fields['warehouse-search'].value='new';const filtered=loadWarehouseInventory();
assert.equal(new URL(pending[2].url,'https://test').searchParams.has('cursor'),false);
reply(2,[]);await filtered;assert.equal(warehouseSelection.size,3);
const stale=loadWarehouseInventory();currentBranch={id:2};const fresh=loadWarehouseInventory();
reply(4,[item(50)]);await fresh;reply(3,[item(60)]);await stale;
assert.equal(warehouseSelection.size,0);assert.equal(warehousePageItems[0].id,50);
oldRow.querySelector('.warehouse-row-select').listeners.change({target:{checked:true}});
assert.equal(warehouseSelection.size,0);
""", "\nconst inventoryCursorState=Object.create(null),pageState={warehouse:1};\n" + helpers)


def test_save_progress_failure_after_server_commit_replays_original_key():
    run_bulk(r"""
select([item(10)]);showWarehouseBulkTransferModal();quantities([2]);
const originalFetch=fetch;
fetch=async (...args)=>{const response=await originalFetch(...args);failStorage=true;return response;};
await confirmWarehouseBulkTransfer();assert.equal(requests.length,1);
assert.equal(readWarehouseBulkQueue().items[0].done,false);
const original=requests[0];failStorage=false;fetch=originalFetch;
showWarehouseBulkTransferModal(true);await confirmWarehouseBulkTransfer();
assert.deepEqual(requests[1],original);assert.equal(readWarehouseBulkQueue(),null);
""")


def test_rejected_retry_with_lost_response_is_no_longer_skippable():
    run_bulk(r"""
select([item(10)]);showWarehouseBulkTransferModal();quantities([2]);
mode='reject';await confirmWarehouseBulkTransfer();assert.equal(readWarehouseBulkQueue().items[0].rejected,true);
mode='network';await confirmWarehouseBulkTransfer();assert.equal(readWarehouseBulkQueue().items[0].rejected,false);
skipRejectedWarehouseBulkItem();assert.equal(readWarehouseBulkQueue().items[0].done,false);
""")