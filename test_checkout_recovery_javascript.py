"""Execute production journal and dashboard functions in Node; no JS libraries."""
import json
from pathlib import Path
import shutil
import pytest
from test_offline_sync_javascript import _function, _run_node_script

ROOT = Path(__file__).parent
pytestmark = pytest.mark.skipif(shutil.which('node') is None, reason='Node required')


def run(body, dashboard=()):
    source = (ROOT / 'templates/dashboard.html').read_text(encoding='utf-8')
    return _run_node_script('''
const assert = require('assert');
// Node 22 exposes a getter-only global navigator; the browser harness owns it.
Object.defineProperty(globalThis, 'navigator', {value: {}, writable: true, configurable: true});
const raw = {};
const localStorage = {get length(){return Object.keys(raw).length}, key:i=>Object.keys(raw)[i],
 getItem:k=>raw[k] ?? null, setItem:(k,v)=>{raw[k]=String(v)}, removeItem:k=>delete raw[k]};
let seq=0, active=true, mode='success', posts=[], pendingResponse;
const options={storage:localStorage, userId:3, branchId:7, id:()=> 'id-'+(++seq), current:()=>active,
 fetch:async (url,opts)=>{
  posts.push(JSON.parse(opts.body));
  assert(Object.keys(raw).some(k=>k.endsWith(':start')), 'attempt durable before fetch');
  assert(Object.keys(raw).some(k=>k.includes(':intent:')), 'intent durable before fetch');
  if(mode==='network') throw Error('Response lost after commit');
  if(mode==='wait') return new Promise(resolve=>pendingResponse=resolve);
  return response(mode);
 }};
function response(kind){const status=kind==='success'?200:Number(kind)||200;
 return {ok:status>=200&&status<300,status,headers:{get:()=>kind==='html'?'text/html':'application/json'},
 json:async()=>({success:kind==='success',transaction_id:posts[posts.length-1]?.transaction_id,
 code:status===409?'checkout_conflict':null,message:'review required'})};}
const payload={items:[{product_id:9,price:10,quantity:2,tax_rate:5}],payment_method:'cash',cash_received:21};
const receipt={total:21,items:[{name:'Coffee'}]};
''' + (ROOT / 'public/offline-checkout.js').read_text(encoding='utf-8') + '\n' +
        '\n'.join(_function(source, name) for name in dashboard) +
        '\n(async()=>{const journal=OfflineCheckout.create(options);\n' + body +
        '\nconsole.log(JSON.stringify({ok:true}));})().catch(e=>{console.error(e);process.exit(1)});')


def test_lost_online_response_replays_exact_original_tender_context_and_id():
    run('''
const sale={...payload,payment_method:'credit_card',cash_received:undefined};
const entry=journal.prepare(sale,receipt,false);
sale.items[0].price=999; receipt.total=999; // caller cannot mutate stored intent
mode='network'; let result=await journal.send(entry.transaction_id,false,true);
assert.equal(result.status,'unknown');
assert.equal(journal.list()[0].status,'unknown');
assert.equal(journal.list()[0].receiptSnapshot.total,21);
assert.equal(journal.list()[0].receiptSnapshot.checkoutOffline,false);
assert.equal(posts[0].checkout_context.offline,false);
assert.equal(posts[0].payment_method,'credit_card');
assert.equal(posts[0].items[0].price,10);
await journal.send(entry.transaction_id,false); assert.equal(posts.length,1);
await assert.rejects(()=>journal.send(entry.transaction_id,true),/manager/i);
mode='success'; result=await journal.send(entry.transaction_id,true,false,'Manager A');
assert.equal(result.transaction_id,entry.transaction_id);
assert.deepEqual(posts[1],posts[0]);
assert.equal(journal.list()[0].status,'synced');
assert.equal(journal.list()[0].attempts,2);
assert.equal(journal.list()[0].events.filter(e=>e.type==='attempt')[1].manager_review,'Manager A');
assert(journal.exportData().records.some(r=>r.raw.includes('Response lost')));
''')


def test_automatic_recovery_waits_then_replays_original_checkout_without_manager():
    run('''
const entry=journal.prepare({...payload,payment_method:'credit_card'},receipt,false);
mode='network';await journal.send(entry.transaction_id,false,true);
assert.equal(await journal.recover(entry.transaction_id),null);
assert.equal(posts.length,1);
const originalNow=Date.now;Date.now=()=>originalNow()+61000;
mode='success';const result=await journal.recover(entry.transaction_id);
assert.equal(result.success,true);assert.deepEqual(posts[1],posts[0]);
assert.equal(journal.list()[0].status,'synced');
assert.equal(journal.list()[0].events.filter(e=>e.type==='attempt')[1].manual,false);
Date.now=originalNow;
''')


@pytest.mark.parametrize('kind', ['400', '409', '403'])
def test_automatic_recovery_never_retries_definite_rejection(kind):
    run('''
const entry=journal.prepare(payload,receipt,true);mode=KIND;
await journal.send(entry.transaction_id,false);
const originalNow=Date.now;Date.now=()=>originalNow()+3600000;
assert.equal(await journal.recover(entry.transaction_id),null);
assert.equal(posts.length,1);Date.now=originalNow;
'''.replace('KIND', json.dumps(kind)))


def test_health_probe_detects_server_outage_and_recovers_while_wifi_is_online():
    run('''
Object.assign(globalThis,{navigator:{onLine:true},checkoutConnectionCheck:null,
 checkoutServerUnavailable:false,setConnectionStatus:()=>{}});
globalThis.fetch=async()=>{throw Error('WAN down')};
assert.equal(await checkCheckoutConnection(),false);
assert.equal(checkoutServerUnavailable,true);
globalThis.fetch=async()=>({ok:true,redirected:false,json:async()=>({status:'ok'})});
assert.equal(await checkCheckoutConnection(),true);
assert.equal(checkoutServerUnavailable,false);
''', ('checkCheckoutConnection','isBrowserOffline'))


@pytest.mark.parametrize('kind', ['408', '429', '401'])
def test_temporary_http_failure_recovers_with_original_id(kind):
    run('''
const entry=journal.prepare(payload,receipt,true);mode=KIND;
await journal.send(entry.transaction_id,false);
const originalNow=Date.now;Date.now=()=>originalNow()+61000;mode='success';
assert((await journal.recover(entry.transaction_id)).success);
assert.deepEqual(posts[0],posts[1]);Date.now=originalNow;
'''.replace('KIND', json.dumps(kind)))


def test_changed_basket_during_connection_check_requires_fresh_confirmation():
    run('''
let resolveProbe, completed=0;const toasts=[];
Object.assign(globalThis,{checkoutPreflight:false,isSaleProcessing:false,CACHE_USER_ID:3,currentBranch:{id:7},
 cart:[{product_id:9,quantity:1}],appliedCoupon:null,document:{querySelectorAll:()=>[]},
 checkoutScopeCurrent:()=>true,setCompleteSaleButtonState:()=>{},showToast:s=>toasts.push(s),
 checkCheckoutConnection:()=>new Promise(resolve=>resolveProbe=resolve),completeSale:()=>completed++});
const pending=completeSaleAutomatically();cart[0].quantity=2;resolveProbe(true);await pending;
assert.equal(completed,0);assert(toasts.some(s=>s.includes('Checkout changed')));
assert.equal(checkoutPreflight,false);
''', ('completeSaleAutomatically',))


def test_automatic_retry_backs_off_and_cannot_bypass_prior_business_conflict():
    run('''
const entry=journal.prepare(payload,receipt,true);mode='network';
await journal.send(entry.transaction_id,false);
const originalNow=Date.now;Date.now=()=>originalNow()+61000;
await journal.recover(entry.transaction_id);assert.equal(posts.length,2);
assert.equal(await journal.recover(entry.transaction_id),null);
Date.now=()=>originalNow()+181000;mode='409';
await journal.recover(entry.transaction_id);assert.equal(posts.length,3);
mode='network';await journal.send(entry.transaction_id,true,false,'Manager');
Date.now=()=>originalNow()+3600000;
assert.equal(await journal.recover(entry.transaction_id),null);
assert.equal(posts.length,4);Date.now=originalNow;
''')


def test_automatic_recovery_after_reload_keeps_unfinished_original_attempt():
    run('''
const entry=journal.prepare(payload,receipt,true);mode='wait';
const sending=journal.send(entry.transaction_id,false);
const originalNow=Date.now;Date.now=()=>originalNow()+61000;
const reloaded=OfflineCheckout.create(options);mode='success';
assert((await reloaded.recover(entry.transaction_id)).success);
assert.deepEqual(posts[0],posts[1]);
pendingResponse(response('409'));await sending;
assert.equal(reloaded.list()[0].status,'synced');Date.now=originalNow;
''')


@pytest.mark.parametrize('kind,status', [('400','rejected'),('409','conflict'),('500','unknown'),('401','auth'),('403','auth'),('html','unknown'),('network','unknown')])
def test_all_errors_retained_no_automatic_permanent_retry(kind, status):
    run(f'''
const entry=journal.prepare(payload,receipt,true); mode={json.dumps(kind)};
const result=await journal.send(entry.transaction_id,false);
assert.equal(result.status,{json.dumps(status)});
for(let i=0;i<3;i++) await journal.send(entry.transaction_id,false);
assert.equal(posts.length,1); assert.equal(journal.list().length,1);
assert.equal(journal.list()[0].attempts,1);
assert.equal(journal.list()[0].saleData.payment_method,'cash');
assert.equal(journal.list()[0].receiptSnapshot.total,21);
''')


@pytest.mark.parametrize('extra', ["payment_method:'card'", "payment_method:'debt'", "payment_method:'mobile_payment'", "payment_method:'split_payment',payment_breakdown:{cash:1,card:20}", "coupon_code:'SAVE'", "delivery:{enabled:true}"])
def test_new_offline_restricts_to_plain_cash_without_creating_intent(extra):
    run('''
assert.throws(()=>journal.prepare({...payload,EXTRA},receipt,true),/cash only/);
assert.equal(journal.list().length,0);assert.equal(posts.length,0);
'''.replace('EXTRA', extra))


def test_storage_failure_before_intent_or_attempt_never_sends():
    run('''
const original=localStorage.setItem;
localStorage.setItem=()=>{throw Error('quota')};
assert.throws(()=>journal.prepare(payload,receipt,true),/quota/);
localStorage.setItem=original;
const entry=journal.prepare(payload,receipt,true);
localStorage.setItem=()=>{throw Error('quota')};
await assert.rejects(()=>journal.send(entry.transaction_id,false),/quota/);
assert.equal(posts.length,0); assert.equal(journal.list()[0].attempts,0);
''')


def test_crash_after_attempt_before_result_requires_manual_review():
    run('''
const entry=journal.prepare(payload,receipt,true); mode='wait';
const sending=journal.send(entry.transaction_id,false);
assert.equal(journal.list()[0].status,'unknown');
const tab=OfflineCheckout.create(options);
assert.equal(await tab.send(entry.transaction_id,false),null);
pendingResponse(response('success')); await sending;
assert.equal(tab.list()[0].status,'synced');
''')


def test_concurrent_tabs_distinct_intents_no_lost_sales_and_success_terminal():
    run('''
const tab=OfflineCheckout.create(options);
const a=journal.prepare(payload,receipt,true), b=tab.prepare(payload,receipt,true);
assert.equal(tab.list().length,2);
mode='wait';const first=journal.send(a.transaction_id,false);
await Promise.resolve();
mode='success';await tab.send(a.transaction_id,true,false,'Manager');
pendingResponse(response('409'));await first;
assert.equal(journal.list().find(e=>e.transaction_id===a.transaction_id).status,'synced');
assert.equal(journal.list().find(e=>e.transaction_id===b.transaction_id).status,'pending');
assert.equal(journal.list().find(e=>e.transaction_id===a.transaction_id).attempts,2);
''')


def test_stale_scope_other_user_branch_and_corruption_are_not_adopted():
    run('''
const entry=journal.prepare(payload,receipt,true);
const other=OfflineCheckout.create({...options,userId:4});
const branch=OfflineCheckout.create({...options,branchId:8});
assert.equal(other.list().length,0);assert.equal(branch.list().length,0);
active=false;await assert.rejects(()=>journal.send(entry.transaction_id,false),/changed/);
assert.throws(()=>journal.prepare(payload,receipt,true),/changed/); active=true;
const key=Object.keys(raw).find(k=>k.includes(':intent:'));raw[key]='broken';
assert.equal(journal.list()[0].status,'corrupt');
await assert.rejects(()=>journal.send(entry.transaction_id,true,false,'Manager'),/corrupt/);
assert.equal(raw[key],'broken');assert.equal(posts.length,0);
assert(journal.exportData().records.some(r=>r.raw==='broken'));
''')


def test_actual_dashboard_sync_never_sends_legacy_or_failed_queue():
    run('''
Object.assign(globalThis,{CACHE_USER_ID:3,CACHE_USER_ROLE:'manager',currentBranch:{id:7},
 dashboardSessionStale:false,dashboardBranchStale:false,branchSwitchInFlight:false,isSyncingPendingSales:false,
 isBrowserOffline:()=>false,checkoutJournal:()=>journal,setConnectionStatus:()=>{},showToast:()=>{},
  checkoutServerUnavailable:false,updatePendingSalesBadge:()=>{},loadSales:()=>{},loadDashboardStats:()=>{},invalidateProductsCache:()=>{}});
raw['pos_pending_sales_v2:3:7']='broken legacy cash/card queue';
const entry=journal.prepare(payload,receipt,true);mode='409';
await syncPendingSales(); await syncPendingSales();
assert.equal(posts.length,1);assert.equal(raw['pos_pending_sales_v2:3:7'],'broken legacy cash/card queue');
assert.equal(journal.list()[0].status,'conflict');
''', ('syncPendingSales','checkoutScopeCurrent'))


@pytest.mark.parametrize('tender', ['cash', 'credit_card', 'debt', 'split_payment'])
def test_actual_complete_sale_durable_before_online_send_and_unknown_not_requeued(tender):
    run('''
const fields={'payment-method':{value:TENDER},'pos-customer-select':{value:'5'},
 'cash-received':{value:'21'},'pos-enable-delivery':{checked:false},
 'split-method-one':{value:'cash'},'split-method-two':{value:'credit_card'},
 'split-amount-one':{value:'10'},'split-amount-two':{value:'11'}};
const toasts=[];let modal=0;
Object.assign(globalThis,{CACHE_USER_ID:3,CACHE_USER_ROLE:'cashier',currentBranch:{id:7},
 dashboardSessionStale:false,dashboardBranchStale:false,branchSwitchInFlight:false,isSaleProcessing:false,
 cart:[{product_id:9,name:'Coffee',price:10,quantity:2,tax_rate:5}],appliedCoupon:null,
 document:{getElementById:id=>fields[id]||null},navigator:{onLine:true},SALE_COOLDOWN_MS:0,
 checkoutJournal:()=>journal,buildOfflineReceiptSnapshot:()=>receipt,
  t:s=>s,requireCapability:()=>true,splitUsesMobilePayment:()=>false,getCurrentCartTotal:()=>21,setCompleteSaleButtonState:()=>{},
 updateCartDisplay:()=>{},updatePendingSalesBadge:()=>{},togglePosDeliveryFields:()=>{},updateCashRefundDisplay:()=>{},
 showToast:msg=>toasts.push(msg),showSaleSuccessModal:()=>modal++,
 resetPOSCustomer:()=>{},resetSplitPaymentFields:()=>{}});
mode='network';completeSale();await new Promise(resolve=>setTimeout(resolve,10));
assert.equal(posts.length,1);assert.equal(cart.length,0);assert.equal(modal,0);
const entry=journal.list()[0];assert.equal(entry.status,'unknown');
assert.equal(entry.saleData.payment_method,TENDER);assert.equal(entry.saleData.checkout_context.offline,false);
assert.equal(entry.attempts,1);assert(toasts.some(s=>s.includes('Do not take payment again')));
completeSale();await new Promise(resolve=>setTimeout(resolve,5));assert.equal(posts.length,1);
'''.replace('TENDER', json.dumps(tender)), ('completeSale','checkoutScopeCurrent','toCents','centsToNumber'))


def test_wrong_success_id_is_not_acknowledged_and_corrupt_legacy_does_not_mask_journal():
    run('''
const entry=journal.prepare(payload,receipt,true);
const other=OfflineCheckout.create({...options,fetch:async()=>({ok:true,status:200,
 headers:{get:()=> 'application/json'},json:async()=>({success:true,transaction_id:'wrong'})})});
assert.equal((await other.send(entry.transaction_id,false)).success,false);
Object.assign(globalThis,{CACHE_USER_ID:3,currentBranch:{id:7},checkoutJournal:()=>journal});
raw['pos_pending_sales_v2:3:7']='broken';
assert(getPendingSales().some(e=>e.transaction_id===entry.transaction_id));
assert.equal(raw['pos_pending_sales_v2:3:7'],'broken');
''', ('getPendingSales',))


@pytest.mark.parametrize('scenario', ['cash', 'quota', 'credit_card', 'debt', 'split_payment', 'coupon', 'delivery'])
def test_actual_offline_completion_preserves_cart_on_unsupported_or_failed_save(scenario):
    run('''
const scenario=SCENARIO, toasts=[];let modal=0, reset=0;
const originalCart=[{product_id:9,name:'Coffee',price:10,quantity:2,tax_rate:5}];
const fields={'cash-received':{value:'21'}};
Object.assign(globalThis,{CACHE_USER_ID:3,CACHE_USER_ROLE:'cashier',currentBranch:{id:7},
 dashboardSessionStale:false,dashboardBranchStale:false,branchSwitchInFlight:false,isSaleProcessing:false,
 cart:originalCart,document:{getElementById:id=>fields[id]||null},SALE_COOLDOWN_MS:0,
 checkoutJournal:()=>journal,buildOfflineReceiptSnapshot:()=>receipt,
 setCompleteSaleButtonState:()=>{},updateCartDisplay:()=>{},updatePendingSalesBadge:()=>{},
 setConnectionStatus:()=>{},showToast:msg=>toasts.push(msg),showSaleSuccessModal:()=>modal++,
 resetPOSCustomer:()=>reset++,resetSplitPaymentFields:()=>{}});
const sale={...payload};
if(['credit_card','debt','split_payment'].includes(scenario)) sale.payment_method=scenario;
if(scenario==='coupon') sale.coupon_code='SAVE';
if(scenario==='delivery') sale.delivery={enabled:true};
if(scenario==='quota') localStorage.setItem=()=>{throw Error('quota')};
finishOfflineSale(sale);await new Promise(resolve=>setTimeout(resolve,5));
assert.equal(posts.length,0);
if(scenario==='cash') {
 assert.equal(cart.length,0);assert.equal(modal,1);assert.equal(reset,1);
 const entry=journal.list()[0];assert.equal(entry.status,'pending');
 assert.equal(entry.receiptSnapshot.total,21);assert.equal(entry.saleData.checkout_context.offline,true);
 assert.equal(entry.transaction_id,sale.transaction_id);
} else {
 assert.strictEqual(cart,originalCart);assert.equal(modal,0);assert.equal(reset,0);
 assert.equal(journal.list().length,0);assert(toasts.some(s=>s.includes('NOT saved')));
 assert.equal(isSaleProcessing,false);
}
'''.replace('SCENARIO', json.dumps(scenario)), ('finishOfflineSale','queueOfflineSale'))


def test_result_storage_failure_keeps_unknown_evidence_and_retries_original_id():
    run('''
const entry=journal.prepare(payload,receipt,true), original=localStorage.setItem;
localStorage.setItem=(key,value)=>{if(key.endsWith(':result'))throw Error('quota');original(key,value)};
await assert.rejects(()=>journal.send(entry.transaction_id,false),/quota/);
assert.equal(posts.length,1);assert.equal(journal.list()[0].status,'unknown');
await journal.send(entry.transaction_id,false);assert.equal(posts.length,1);
localStorage.setItem=original;
assert((await journal.send(entry.transaction_id,true,false,'Manager')).success);
assert.deepEqual(posts[0],posts[1]);assert.equal(journal.list()[0].status,'synced');
''')


@pytest.mark.parametrize('tender', ['cash', 'mobile_payment', 'credit_card', 'debt', 'split_payment'])
def test_detected_server_outage_when_browser_still_reports_online(tender):
    run('''
const fields={'payment-method':{value:TENDER},'pos-customer-select':{value:''},
 'cash-received':{value:'21'},'pos-enable-delivery':{checked:false}};
let modal=0;
Object.assign(globalThis,{CACHE_USER_ID:3,CACHE_USER_ROLE:'cashier',currentBranch:{id:7},
 dashboardSessionStale:false,dashboardBranchStale:false,branchSwitchInFlight:false,isSaleProcessing:false,
 cart:[{product_id:9,name:'Coffee',price:10,quantity:2,tax_rate:5}],appliedCoupon:null,
  checkoutServerUnavailable:true,document:{getElementById:id=>fields[id]||null},navigator:{onLine:true},SALE_COOLDOWN_MS:0,
 checkoutJournal:()=>journal,buildOfflineReceiptSnapshot:()=>receipt,
 t:s=>s,requireCapability:()=>true,getCurrentCartTotal:()=>21,setCompleteSaleButtonState:()=>{},
 updateCartDisplay:()=>{},updatePendingSalesBadge:()=>{},setConnectionStatus:()=>{},
 showToast:()=>{},showSaleSuccessModal:()=>modal++,resetPOSCustomer:()=>{},resetSplitPaymentFields:()=>{}});
completeSale();await new Promise(resolve=>setTimeout(resolve,5));
assert.equal(posts.length,0);
if(TENDER==='cash'){
 assert.equal(cart.length,0);assert.equal(modal,1);
 assert.equal(journal.list()[0].status,'pending');assert.equal(journal.list()[0].saleData.checkout_context.offline,true);
}else {assert.equal(cart.length,1);assert.equal(modal,0);assert.equal(journal.list().length,0);}
'''.replace('TENDER', json.dumps(tender)), ('completeSale','finishOfflineSale','queueOfflineSale','toCents','centsToNumber'))


def test_branch_change_during_send_records_result_under_original_scope():
    run('''
const entry=journal.prepare(payload,receipt,true);mode='wait';
const sending=journal.send(entry.transaction_id,false);active=false;
const other=OfflineCheckout.create({...options,branchId:8});
pendingResponse(response('success'));await sending;
assert.equal(journal.list()[0].status,'synced');assert.equal(other.list().length,0);
assert.equal(posts.length,1);
''')


def test_saved_receipt_copy_distinguishes_unknown_online_from_acknowledged():
    run('''
let printed='';
Object.assign(globalThis,{window:{open:()=>({document:{write:s=>printed=s,close:()=>{}},close:()=>{}})},
 escapeHtml:s=>String(s),formatOfflineReceiptMoney:v=>String(v),formatOfflineReceiptNumber:s=>s,
 showToast:()=>{}});
const saved={...receipt,items:[],transactionId:'receipt-id',paperWidthMm:58,
 checkoutOffline:false,paymentMethod:'credit_card'};
assert(openOfflineReceiptWindow(saved));
assert(printed.includes('verify central outcome; do not pay again'));
assert(!printed.includes('OFFLINE SALE'));
assert(openOfflineReceiptWindow({...saved,recoveryStatus:'synced'}));
assert(printed.includes('server acknowledged'));assert(!printed.includes('pending sync'));
assert(openOfflineReceiptWindow({...saved,checkoutOffline:true}));
assert(printed.includes('pending sync / reconciliation'));
''', ('openOfflineReceiptWindow',))


@pytest.mark.parametrize('stage', ['headers', 'body'])
def test_hung_transport_times_out_retains_identity_and_releases_lock(stage):
    run('''
const entry=journal.prepare(payload,receipt,true);let signal;
const stalled=OfflineCheckout.create({...options,timeoutMs:5,fetch:async(url,opts)=>{
 posts.push(JSON.parse(opts.body));signal=opts.signal;
 if(STAGE==='headers') return new Promise(()=>{});
 return {ok:true,status:200,headers:{get:()=> 'application/json'},json:()=>new Promise(()=>{})};
}});
const result=await stalled.send(entry.transaction_id,false);
assert.equal(result.status,'unknown');assert(signal.aborted);
assert.equal(journal.list()[0].status,'unknown');
assert((await journal.send(entry.transaction_id,true,false,'Manager')).success);
assert.deepEqual(posts[0],posts[1]);
'''.replace('STAGE', json.dumps(stage)))


@pytest.mark.parametrize('value', ['null', '[]', '{}', "{success:true,transaction_id:'other'}"])
def test_malformed_200_response_is_unknown_and_never_auto_retried(value):
    run('''
const entry=journal.prepare(payload,receipt,true);
const broken=OfflineCheckout.create({...options,fetch:async()=>({ok:true,status:200,
 headers:{get:()=> 'application/json'},json:async()=>(VALUE)})});
assert.equal((await broken.send(entry.transaction_id,false)).status,'unknown');
assert.equal(await journal.send(entry.transaction_id,false),null);
assert.equal(journal.list()[0].attempts,1);
'''.replace('VALUE', value))


def test_new_unfinished_attempt_supersedes_old_rejection_even_with_clock_rollback():
    run('''
const entry=journal.prepare(payload,receipt,true);mode='400';
await journal.send(entry.transaction_id,false);assert.equal(journal.list()[0].status,'rejected');
const key='pos_checkout_v3:3:7:event:'+entry.transaction_id+':crash:start';
raw[key]=JSON.stringify({type:'attempt',attempt_id:'crash',at:'2020-01-01T00:00:00Z'});
assert.equal(journal.list()[0].status,'unknown');
await journal.send(entry.transaction_id,false);assert.equal(posts.length,1);
''')


def test_corrupt_or_orphaned_attempts_are_visible_and_not_sent():
    run('''
const entry=journal.prepare(payload,receipt,true);
raw['pos_checkout_v3:3:7:event:'+entry.transaction_id+':bad:result']=JSON.stringify({
 type:'result',attempt_id:'bad',at:'2026-01-01T00:00:00Z',status:'synced',transaction_id:'wrong'});
assert.equal(journal.list()[0].status,'corrupt');
await assert.rejects(()=>journal.send(entry.transaction_id,true,false,'Manager'),/corrupt/);
delete raw['pos_checkout_v3:3:7:intent:'+entry.transaction_id];
assert(journal.list().some(e=>e.status==='corrupt'&&e.error.includes('Orphaned')));
assert(journal.exportData().records.length>0);assert.equal(posts.length,0);
''')


@pytest.mark.parametrize('failure', ['badge', 'modal', 'cart'])
def test_offline_saved_basket_not_resold_after_optional_ui_failure(failure):
    run('''
Object.assign(globalThis,{CACHE_USER_ID:3,CACHE_USER_ROLE:'cashier',currentBranch:{id:7},
 dashboardSessionStale:false,dashboardBranchStale:false,branchSwitchInFlight:false,isSaleProcessing:false,
 cart:[{product_id:9}],document:{getElementById:()=>null},SALE_COOLDOWN_MS:0,
 checkoutJournal:()=>journal,buildOfflineReceiptSnapshot:()=>receipt,setCompleteSaleButtonState:()=>{},
 updateCartDisplay:()=>{if(FAILURE==='cart')throw Error('display');},
 updatePendingSalesBadge:()=>{if(FAILURE==='badge')throw Error('display');},
 setConnectionStatus:()=>{},showToast:()=>{},showSaleSuccessModal:()=>{if(FAILURE==='modal')throw Error('display');}});
finishOfflineSale({...payload});await new Promise(resolve=>setTimeout(resolve,5));
assert.equal(journal.list().length,1);assert.equal(cart.length,0);assert.equal(isSaleProcessing,false);
assert.equal(posts.length,0);
'''.replace('FAILURE', json.dumps(failure)), ('finishOfflineSale','queueOfflineSale'))


def test_online_display_exception_does_not_prevent_transport_or_release_of_checkout():
    run('''
const fields={'payment-method':{value:'cash'},'pos-customer-select':{value:''},
 'cash-received':{value:'21'},'pos-enable-delivery':{checked:false}};
Object.assign(globalThis,{CACHE_USER_ID:3,CACHE_USER_ROLE:'cashier',currentBranch:{id:7},
 dashboardSessionStale:false,dashboardBranchStale:false,branchSwitchInFlight:false,isSaleProcessing:false,
 cart:[{product_id:9,price:10,quantity:2,tax_rate:5}],appliedCoupon:null,navigator:{onLine:true},
 document:{getElementById:id=>fields[id]||null},SALE_COOLDOWN_MS:0,
 checkoutJournal:()=>journal,buildOfflineReceiptSnapshot:()=>receipt,t:s=>s,requireCapability:()=>true,
 getCurrentCartTotal:()=>21,setCompleteSaleButtonState:()=>{},updateCartDisplay:()=>{throw Error('display');},
 updatePendingSalesBadge:()=>{throw Error('badge');},showToast:()=>{},showSaleSuccessModal:()=>{},
 loadSales:()=>{},loadDashboardStats:()=>{},invalidateProductsCache:()=>{}});
completeSale();await new Promise(resolve=>setTimeout(resolve,10));
assert.equal(posts.length,1);assert.equal(journal.list()[0].status,'synced');
assert.equal(cart.length,0);assert.equal(isSaleProcessing,false);
''', ('completeSale','checkoutScopeCurrent','toCents','centsToNumber'))