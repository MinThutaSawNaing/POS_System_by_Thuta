"""Execute production delivery extension JavaScript in Node (no application import)."""
from test_delivery_operations_javascript import run_js, pytestmark
from test_delivery_operations_javascript import SOURCE
from test_offline_sync_javascript import _run_node_script


def test_ai_delivery_refresh_maps_writes_not_reads_and_blocks_stale_identity():
    listener = SOURCE.split('window.addEventListener("ai-records-changed", (event) => {', 1)[1].split('\n      });', 1)[0]
    _run_node_script(r"""
const assert=require('node:assert/strict');let dashboardSessionStale=false,dashboardBranchStale=false,reads=0,stats=0,allowed=true;
const deliveryWorkbench={detail:null};function hasCapability(){return allowed;}function hasManagerOrBossAccess(){return false;}
function loadDeliveries(){reads++;}function loadDeliveryStats(){stats++;}function deliveryContextCurrent(){return false;}
function listener(event){
""" + listener + r"""
}
for(const tool of ['get_delivery_details','get_delivery_history','get_deliveries']) listener({detail:{tools:[tool]}});
assert.equal(reads,0);
for(const tool of ['update_delivery_carrier','record_delivery_attempt','retry_delivery','record_delivery_collection','record_delivery_remittance','delete_delivery_record']) listener({detail:{tools:[tool]}});
assert.equal(reads,6);assert.equal(stats,6);
dashboardSessionStale=true;listener({detail:{tools:['delete_delivery_record']}});assert.equal(reads,6);
dashboardSessionStale=false;allowed=false;listener({detail:{tools:['delete_delivery_record']}});assert.equal(reads,6);
console.log(JSON.stringify({passed:true}));
""")


def test_photo_multipart_duplicate_lock_and_stale_identity():
    run_js(r"""
const context={id:7, identity, loaded:true, applicable:{proofs:true,carrier:true}};
deliveryWorkbench.detail=context;
el('delivery-photo').files=[Object.assign(new Blob(['photo'],{type:'image/jpeg'}),{name:'camera.jpg'})];
let request=submitDeliveryProof('photo');
assert.equal(pending.length,1);assert.equal(pending[0].url,'/api/deliveries/7/proofs');
assert.ok(pending[0].init.body instanceof FormData);assert.equal(pending[0].init.headers,undefined);
assert.equal(pending[0].init.body.get('kind'),'photo');assert.equal(pending[0].init.body.get('file').name,'camera.jpg');
assert.equal(pending[0].init.body.get('file').type,'image/jpeg');assert.equal(pending[0].init.body.get('signer_name'),'');
await submitDeliveryProof('photo');await submitDeliveryOperation('carrier');assert.equal(pending.length,1);
identity='other';reply(0,{success:true});await request;assert.equal(pending.length,1);assert.equal(deliveryWorkbench.writes.size,0);
identity=context.identity;el('delivery-photo').files=[new Blob(['x'],{type:'image/svg+xml'})];await submitDeliveryProof('photo');assert.equal(pending.length,1);
el('delivery-photo').files=[new Blob([new Uint8Array(5*1024*1024+1)],{type:'image/png'})];await submitDeliveryProof('photo');assert.equal(pending.length,1);
dashboardSessionStale=true;await submitDeliveryProof('photo');assert.equal(pending.length,1);
""")


def test_signature_pointer_drawing_clear_empty_guard_and_async_conversion_identity():
    run_js(r"""
const context={id:7,identity,loaded:true,applicable:{proofs:true}};deliveryWorkbench.detail=context;
let strokes=0,convert;const canvas=el('delivery-signature');canvas.width=600;canvas.height=200;
canvas.getContext=()=>({clearRect(){},beginPath(){},moveTo(){},lineTo(){},stroke(){strokes++;}});
canvas.getBoundingClientRect=()=>({left:0,top:0,width:300,height:100});canvas.setPointerCapture=()=>{};
canvas.toBlob=callback=>{convert=callback;};setupDeliverySignature(context);el('delivery-signer').value=' Ada ';
await submitDeliveryProof('signature');assert.equal(pending.length,0);assert.match(el('delivery-operations-error').textContent,/Draw/);
const event=(x,y)=>({pointerId:1,button:0,clientX:x,clientY:y,preventDefault(){}});
canvas.onpointerdown(event(5,5));canvas.onpointermove(event(25,25));canvas.onpointerup(event(25,25));assert.equal(strokes,1);assert.equal(context.signatureDrawn,true);
clearDeliverySignature();await submitDeliveryProof('signature');assert.equal(pending.length,0);
canvas.onpointerdown(event(5,5));canvas.onpointermove(event(25,25));
let request=submitDeliveryProof('signature');assert.equal(deliveryWorkbench.writes.has('delivery:7'),true);
identity='changed';convert(new Blob(['png'],{type:'image/png'}));await request;assert.equal(pending.length,0);assert.equal(deliveryWorkbench.writes.size,0);
identity=context.identity;request=submitDeliveryProof('signature');convert(new Blob(['png'],{type:'image/png'}));await tick();
assert.equal(pending.length,1);assert.equal(pending[0].init.body.get('kind'),'signature');assert.equal(pending[0].init.body.get('signer_name'),'Ada');assert.equal(pending[0].init.body.get('file').name,'signature.png');
reply(0,{message:'Rejected'},400);await request;
""")


def test_deletion_explicit_confirmation_capability_role_scope_and_terminal_guard():
    run_js(r"""
global.CURRENT_USER_ROLE='manager';
const context={id:7,identity,loaded:true,stage:'delivered',cod:{status:'remitted'},applicable:{delete:true}};deliveryWorkbench.detail=context;
el('delivery-delete-reason').value=' Duplicate record ';
await submitDeliveryOperation('delete');assert.equal(pending.length,0);
el('delivery-delete-confirm').checked=true;
for (const role of ['cashier','staff']) {global.CURRENT_USER_ROLE=role;await submitDeliveryOperation('delete');}assert.equal(pending.length,0);
global.CURRENT_USER_ROLE='boss';allowed=false;await submitDeliveryOperation('delete');allowed=true;
identity='wrong-branch';await submitDeliveryOperation('delete');identity=context.identity;
context.stage='delivering';await submitDeliveryOperation('delete');context.stage='delivered';context.cod.status='collected';await submitDeliveryOperation('delete');assert.equal(pending.length,0);
context.cod.status='pending';await submitDeliveryOperation('delete');assert.equal(pending.length,0);
context.cod.status='remitted';let request=submitDeliveryOperation('delete');
assert.equal(pending[0].url,'/api/deliveries/7');assert.equal(pending[0].init.method,'DELETE');assert.deepEqual(JSON.parse(pending[0].init.body),{confirm:true,reason:'Duplicate record'});
await submitDeliveryOperation('delete');assert.equal(pending.length,1);reply(0,{message:'Rejected'},400);await request;
""")


def test_carrier_manual_https_payload_and_no_live_booking_claims():
    run_js(r"""
deliveryWorkbench.detail={id:7,identity,loaded:true,applicable:{carrier:true}};
el('delivery-carrier-name').value=' DHL ';el('delivery-tracking-code').value=' ABC ';el('delivery-carrier-status').value=' Reported dispatched ';el('delivery-carrier-note').value=' Manual report ';
for (const url of ['javascript:alert(1)','http://carrier.example/track','https://user:pass@carrier.example/']) {el('delivery-tracking-url').value=url;await submitDeliveryOperation('carrier');}assert.equal(pending.length,0);
el('delivery-tracking-url').value='https://carrier.example/track';let request=submitDeliveryOperation('carrier');
assert.equal(pending[0].url,'/api/deliveries/7/carrier');assert.deepEqual(JSON.parse(pending[0].init.body),{carrier_name:'DHL',tracking_code:'ABC',tracking_url:'https://carrier.example/track',status:'Reported dispatched',note:'Manual report'});
reply(0,{message:'Rejected'},400);await request;
""")


def test_proof_urls_are_authenticated_same_origin_and_safe_dom():
    run_js(r"""
global.window={location:{origin:'https://pos.example'}};
const create=document.createElement;document.createElement=()=>{const element=create();element.style={};return element;};
const context={id:7,identity,loaded:true};deliveryWorkbench.detail=context;
await renderDeliveryProofs(context,[{url:'https://evil.example/api/deliveries/7/proofs/1',kind:'<script>x</script>'},{url:'/api/deliveries/8/proofs/1'}]);assert.equal(pending.length,0);
assert.equal(el('delivery-proof-previews').children[0].children[0].innerHTML,'');
let request=renderDeliveryProofs(context,[{url:'/api/deliveries/7/proofs/1',kind:'photo'}]);
assert.equal(pending[0].init.credentials,'same-origin');assert.equal(pending[0].init.cache,'no-store');
pending[0].resolve({ok:true,blob:async()=>new Blob(['png'],{type:'image/png'})});await request;
const image=el('delivery-proof-previews').children[2].children[1];assert.match(image.src,/^blob:/);assert.equal(image.innerHTML,'');clearDeliveryProofPreviews(context);
""")