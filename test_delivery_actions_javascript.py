"""Single-entry delivery actions use the production workbench in Node."""
from test_delivery_operations_javascript import run_js, pytestmark


def test_delivery_rows_have_one_accessible_action_for_each_permission_level():
    run_js(r"""
let request=loadDeliveries();reply(0,page([{id:7,delivery_number:'D7',stage:'packaged'}]));await request;
let html=el('deliveries-table').children[0].innerHTML;
assert.equal((html.match(/<button/g)||[]).length,1);
assert.match(html,/aria-label="Manage delivery D7"/);
assert.ok(!html.includes('printDeliverySlip('));assert.ok(!html.includes('openUpdateDeliveryModal('));
assert.match(html,/<td class="d-none d-md-table-cell">/);
allowed=false;request=loadDeliveries();reply(1,page([{id:7,delivery_number:'D7'}]));await request;
html=el('deliveries-table').children[0].innerHTML;
assert.equal((html.match(/<button/g)||[]).length,1);assert.match(html,/View delivery/);
""")


def test_suggested_action_follows_stage_failed_attempt_and_cod_state():
    run_js(r"""
global.CURRENT_USER_ROLE='manager';
for(const [stage,cod,failed,expected] of [
 ['to_deliver','pending',false,'progress'],['packaged','pending',false,'progress'],
 ['delivering','pending',false,'attempts'],['delivering','pending',true,'retry'],
 ['delivered','pending',false,'collection'],['delivered','collected',false,'remittance'],
 ['delivered','remitted',false,'overview'],['cancelled','pending',false,'overview']]){
 const n=pending.length;const request=openDeliveryOperations(7);
 reply(n,{id:7,stage,cod:{status:cod},has_failed_attempt:failed});reply(n+1,{items:[]});await request;
 assert.equal(el('delivery-action').value,expected);
 const actions=deliveryActions(deliveryWorkbench.detail).map(a=>a.id);
 assert.ok(actions.includes('print'));assert.ok(actions.includes('edit'));
 assert.ok(actions.includes('photo'));assert.ok(actions.includes('signature'));assert.ok(actions.includes('carrier'));
 assert.equal(actions.includes('delete'),stage==='cancelled'||(stage==='delivered'&&cod==='remitted'));
}
""")


def test_picker_shows_one_panel_and_preserves_form_values_when_switching():
    run_js(r"""
const context={id:7,identity,loaded:true,stage:'delivering',applicable:{attempts:true,proofs:true,carrier:true}};
deliveryWorkbench.detail=context;
const panels=['attempts','photo','signature','carrier','edit','print','overview','delete'].map(id=>{
 const panel=new Element();panel.dataset.deliveryPanel=id;
 if(['attempts','photo','signature','carrier','delete'].includes(id))panel.dataset.deliveryOperation=['photo','signature'].includes(id)?'proofs':id;
 return panel;
});
el('deliveryOperationsModal').querySelectorAll=selector=>selector==='[data-delivery-panel]'?panels:panels.filter(p=>p.dataset.deliveryOperation);
syncDeliveryOperationForms(context);assert.equal(context.action,'attempts');
for(const action of ['signature','photo','carrier','edit','print','overview','attempts']){
 el('delivery-attempt-reason').value='Keep this draft';selectDeliveryAction(action);
 assert.deepEqual(panels.filter(p=>!p.hidden).map(p=>p.dataset.deliveryPanel),[action]);
 assert.equal(el('delivery-attempt-reason').value,'Keep this draft');
}
selectDeliveryAction('delete');assert.equal(context.action,'attempts');
deliveryWorkbench.writes.add('delivery:7');syncDeliveryOperationForms(context);
assert.equal(el('delivery-action').disabled,true);selectDeliveryAction('print');assert.equal(context.action,'attempts');
deliveryWorkbench.writes.clear();allowed=false;syncDeliveryOperationForms(context);
assert.equal(context.action,'overview');assert.deepEqual(deliveryActions(context).map(a=>a.id),['print','overview']);
assert.deepEqual(panels.filter(p=>!p.hidden).map(p=>p.dataset.deliveryPanel),['overview']);
identity='other';syncDeliveryOperationForms(context);assert.equal(el('delivery-action').disabled,true);
assert.equal(panels.filter(p=>!p.hidden).length,0);
""")


def test_progress_requires_explicit_submit_and_blocks_duplicate_and_stale_requests():
    run_js(r"""
const context={id:7,identity,loaded:true,stage:'to_deliver',nextStage:'packaged',applicable:{progress:true}};
deliveryWorkbench.detail=context;syncDeliveryOperationForms(context);
assert.equal(pending.length,0);assert.equal(context.action,'progress');
let request=runDeliveryAction('progress');assert.equal(pending[0].url,'/api/deliveries/7');
assert.equal(pending[0].init.method,'PUT');assert.deepEqual(JSON.parse(pending[0].init.body),{stage:'packaged'});
assert.equal(el('delivery-action').disabled,true);
await runDeliveryAction('progress');assert.equal(pending.length,1);
reply(0,{message:'Rejected'},400);await request;assert.equal(el('delivery-action').disabled,false);
identity='other';await runDeliveryAction('progress');assert.equal(pending.length,1);
identity=context.identity;context.nextStage='delivered';await runDeliveryAction('progress');assert.equal(pending.length,1);
allowed=false;await runDeliveryAction('progress');assert.equal(pending.length,1);
""")


def test_readonly_deleted_and_failed_loading_never_expose_write_actions():
    run_js(r"""
global.CURRENT_USER_ROLE='manager';allowed=false;
let request=openDeliveryOperations(7);reply(0,{id:7,stage:'delivering'});reply(1,{items:[]});await request;
assert.equal(el('delivery-action').value,'overview');assert.deepEqual(deliveryActions(deliveryWorkbench.detail).map(a=>a.id),['print','overview']);
allowed=true;request=openDeliveryOperations(7);reply(2,{id:7,stage:'delivered',is_deleted:true});reply(3,{items:[]});await request;
assert.deepEqual(deliveryActions(deliveryWorkbench.detail).map(a=>a.id),['print','overview']);
request=openDeliveryOperations(7);reply(4,{message:'Unavailable'},500);reply(5,{items:[]});await request;
assert.equal(el('delivery-action').disabled,true);assert.equal(el('delivery-action').children.length,0);
assert.match(el('delivery-operations-error').textContent,/Unavailable/);
""")


def test_delayed_edit_cannot_reopen_a_closed_or_replaced_workspace():
    run_js(r"""
for(const replacement of [null,{id:8,identity,loaded:true}]){
 const context={id:7,identity,loaded:true,stage:'packaged',action:'edit',applicable:{}};
 deliveryWorkbench.detail=context;
 const n=pending.length,request=runDeliveryAction('edit');
 assert.equal(pending[n].url,'/api/deliveries/7');
 deliveryWorkbench.detail=replacement;
 reply(n,{id:7,stage:'packaged'});await request;
 assert.equal(shown.length,0);assert.equal(deliveryWorkbench.update,null);
}
""")