"""Real Node regressions for the delivery workbench, with no application import."""
from pathlib import Path
import shutil
import subprocess
import pytest
from test_offline_sync_javascript import _function, _run_node_script

SOURCE = (Path(__file__).parent / "templates/dashboard.html").read_text(encoding="utf-8")
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node required")
WORKBENCH = SOURCE[SOURCE.index("      const deliveryWorkbench ="):SOURCE.index("      function openCreateDeliveryModal()")]
UPDATE = SOURCE[SOURCE.index("      async function openUpdateDeliveryModal("):SOURCE.index("      function printDeliverySlip(")]
HARNESS = r"""
const assert = require('node:assert/strict');
class Element {
 constructor(){this.value='';this.children=[];this.dataset={};this.textContent='';this._html='';this.disabled=false;this.hidden=false;}
 set innerHTML(v){this._html=v;this.children=[];} get innerHTML(){return this._html;}
 appendChild(c){this.children.push(c);} replaceChildren(){this.children=[];this.textContent='';}
 querySelectorAll(){return [];} addEventListener(){}
}
const elements={}; const document={getElementById(id){return elements[id] ||= new Element();},createElement(){return new Element();},querySelectorAll(){return [];}};
let identity='branch1', allowed=true, dashboardSessionStale=false, dashboardBranchStale=false;
function inventoryIdentity(){return identity;}
function hasCapability(){return allowed;} function requireCapability(){return allowed;}
function escapeHtml(v){return String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function formatCurrency(v){return '$'+v;}
const toasts=[]; function showToast(...v){toasts.push(v);}
const shown=[];function showModalAfterHiding(id){shown.push(id);}
const bootstrap={Modal:{getInstance(){return {hide(){}};}}};
const pending=[];function fetch(url,init){return new Promise((resolve,reject)=>pending.push({url,init,resolve,reject}));}
function reply(i,data,status=200){pending[i].resolve({ok:status<400,status,json:async()=>data});}
const tick=()=>new Promise(r=>setImmediate(r));
const el=id=>document.getElementById(id);
const page=(items=[],n=1,pages=3)=>({items,total:55,page:n,pages,per_page:25});
"""

def run_js(script):
    try:
        _run_node_script(HARNESS + WORKBENCH + UPDATE + "\n(async()=>{\n" + script +
                         "\nconsole.log(JSON.stringify({passed:true}));})().catch(e=>{console.error(e);process.exit(1);});")
    except subprocess.CalledProcessError as error:
        pytest.fail(error.stdout + error.stderr)


def test_server_pagination_filters_and_current_page_selection():
    run_js(r"""
el('deliveries-courier-filter').value='  Ada  '; el('deliveries-attention-filter').value='failed';
let request=loadDeliveries();
assert.match(pending[0].url,/per_page=25/);assert.match(pending[0].url,/courier=Ada/);assert.match(pending[0].url,/attention=failed/);assert.match(pending[0].url,/page=1/);
reply(0,page([{id:7,delivery_number:'D7'}]));await request;
selectDeliveryPage(true);assert.deepEqual([...deliveryWorkbench.selected],[7]);
request=loadDeliveries(2);assert.match(pending[1].url,/page=2/);assert.equal(deliveryWorkbench.selected.size,0);
reply(1,page([{id:8}],2));await request;
el('deliveries-search').value='new';request=loadDeliveries(3);assert.match(pending[2].url,/page=1/);reply(2,page());await request;
identity='branch2';request=loadDeliveries(3);assert.match(pending[3].url,/page=1/);reply(3,page());await request;
""")


def test_delivery_strings_are_escaped_and_invalid_ids_never_rendered():
    run_js(r"""
const attack='<img src=x onerror="evil()">';let request=loadDeliveries();
reply(0,page([{id:7,delivery_number:attack,recipient_name:attack,recipient_phone:attack,stage_label:attack,priority:attack,payment_status_label:attack,sale_transaction_id:"x');evil()//",courier_name:attack,cod:{status:attack}},{id:'7);evil()//'}]));await request;
const rows=el('deliveries-table').children;assert.equal(rows.length,1);assert.ok(!rows[0].innerHTML.includes('<img'));assert.ok(rows[0].innerHTML.includes('&lt;img'));assert.ok(!rows[0].innerHTML.includes("viewSaleDetails("));assert.ok(rows[0].innerHTML.includes('openDeliveryOperations(7)'));
allowed=false;request=loadDeliveries();reply(1,page([{id:9}]));await request;assert.ok(!el('deliveries-table').children[0].innerHTML.includes('openUpdateDeliveryModal'));
""")


def test_mixed_stage_page_selects_only_active_deliveries():
    run_js(r"""
let request=loadDeliveries();
reply(0,page([{id:1,stage:'packaged'},{id:2,stage:'delivered'},{id:3,stage:'cancelled'},{id:4,stage:'delivering'}]));await request;
const rows=el('deliveries-table').children;
assert.ok(rows[0].innerHTML.includes('data-delivery-select="1"'));
assert.ok(!rows[1].innerHTML.includes('data-delivery-select'));
assert.ok(!rows[2].innerHTML.includes('data-delivery-select'));
selectDeliveryPage(true);assert.deepEqual([...deliveryWorkbench.selected],[1,4]);
assert.equal(el('deliveries-select-page').checked,true);assert.equal(el('deliveries-selected-count').textContent,'2 selected');
toggleDeliverySelection(2,true);toggleDeliverySelection(3,true);assert.deepEqual([...deliveryWorkbench.selected],[1,4]);
deliveryWorkbench.selected.add(2);syncDeliverySelection();assert.deepEqual([...deliveryWorkbench.selected],[1,4]);
el('deliveries-bulk-courier').value='Ada';request=bulkAssignDeliveries();
assert.deepEqual(JSON.parse(pending[1].init.body).delivery_ids,[1,4]);reply(1,{},400);await request;
request=loadDeliveries();reply(2,page([{id:2,stage:'delivered'},{id:3,stage:'cancelled'}]));await request;
selectDeliveryPage(true);assert.equal(deliveryWorkbench.selected.size,0);assert.equal(el('deliveries-select-page').disabled,true);assert.equal(el('deliveries-bulk-submit').disabled,true);
""")


def test_stale_lists_details_history_stats_and_writes_are_excluded():
    run_js(r"""
const first=loadDeliveries(),second=loadDeliveries();reply(1,page([{id:2}]));await second;reply(0,page([{id:1}]));await first;assert.equal(deliveryWorkbench.rows[0].id,2);
const detail=openDeliveryOperations(2);identity='branch2';reply(2,{id:2,recipient_name:'SECRET'});reply(3,{items:[{note:'SECRET'}]});await detail;assert.ok(!el('delivery-operations-details').textContent.includes('SECRET'));assert.equal(el('delivery-operations-history').children.length,0);
const stats=loadDeliveryStats();identity='branch3';reply(4,{total:999});await stats;assert.notEqual(el('delivery-total').textContent,999);
const write=deliveryWrite('a','/api/deliveries/2/retry',{note:'x'});assert.equal(await deliveryWrite('a','/api/deliveries/2/retry',{}),false);identity='branch4';reply(5,{success:true});assert.equal(await write,false);
""")


def test_bulk_and_operations_payloads_validation_and_duplicate_guard():
    run_js(r"""
deliveryWorkbench.identity=identity;deliveryWorkbench.rows=[{id:7}];deliveryWorkbench.selected.add(7);
el('deliveries-bulk-courier').value=' Ada ';el('deliveries-bulk-phone').value=' 123 ';
let request=bulkAssignDeliveries();assert.deepEqual(JSON.parse(pending[0].init.body),{delivery_ids:[7],courier_name:'Ada',courier_phone:'123'});reply(0,{message:'No'},400);await request;
deliveryWorkbench.detail={id:7,identity,loaded:true};el('delivery-attempt-outcome').value='failed';el('delivery-attempt-reason').value='Absent';el('delivery-attempt-proof').value='Called';
request=submitDeliveryOperation('attempts');assert.deepEqual(JSON.parse(pending[1].init.body),{outcome:'failed',reason:'Absent',proof_note:'Called'});await submitDeliveryOperation('attempts');assert.equal(pending.length,2);reply(1,{message:'No'},400);await request;
el('delivery-retry-note').value='Tomorrow';el('delivery-retry-scheduled').value='2026-11-06T10:30';request=submitDeliveryOperation('retry');assert.deepEqual(JSON.parse(pending[2].init.body),{note:'Tomorrow',scheduled_at:new Date('2026-11-06T10:30').toISOString()});reply(2,{},400);await request;
for(const op of ['collection','remittance']){el('delivery-'+op+'-amount').value='12.34';el('delivery-'+op+'-reference').value=' R ';const index=pending.length;request=submitDeliveryOperation(op);assert.deepEqual(JSON.parse(pending[index].init.body),{amount:'12.34',reference:'R'});reply(index,{},400);await request;}
const count=pending.length;el('delivery-collection-amount').value='-1';await submitDeliveryOperation('collection');assert.equal(pending.length,count);allowed=false;await submitDeliveryOperation('retry');assert.equal(pending.length,count);
""")


def test_details_history_text_content_and_update_schedule_iso_payload():
    run_js(r"""
const attack='<script>evil()</script>';let request=openDeliveryOperations(7);reply(0,{id:7,delivery_number:attack,last_attempt:{outcome:'failed',reason:attack},cod:{status:'pending',expected_amount:'20.00'}});reply(1,{items:[{actor_name:attack,note:attack,action:'attempt'}]});await request;
assert.ok(el('delivery-operations-details').textContent.includes(attack));assert.ok(el('delivery-operations-history').children[0].textContent.includes(attack));assert.equal(el('delivery-operations-history').children[0].innerHTML,'');
request=openUpdateDeliveryModal(7);reply(2,{id:7,stage:'packaged',priority:'high',scheduled_at:'2026-11-06T10:00:00Z'});await request;
el('update-delivery-scheduled').value='2026-11-07T13:45';request=updateDelivery();assert.equal(JSON.parse(pending[3].init.body).scheduled_at,new Date('2026-11-07T13:45').toISOString());reply(3,{},400);await request;
identity='other';const n=pending.length;await updateDelivery();assert.equal(pending.length,n);
""")


def test_modal_handoff_does_not_show_after_identity_becomes_stale():
    helper = _function(SOURCE, "showModalAfterHiding")
    _run_node_script(r"""
const assert=require('node:assert/strict');
let callback, valid=true, shown=0;
const target={}, previous={addEventListener(name, fn){callback=fn;}};
const document={getElementById(){return target;},querySelectorAll(){return [previous];}};
const bootstrap={Modal:{getOrCreateInstance(element){return {show(){shown++;},hide(){}};}}};
""" + helper + r"""
showModalAfterHiding('deliveryOperationsModal',()=>valid);
assert.equal(shown,0);valid=false;callback();assert.equal(shown,0);
showModalAfterHiding('deliveryOperationsModal',()=>valid);
valid=true;callback();assert.equal(shown,1);
console.log(JSON.stringify({passed:true}));
""")
