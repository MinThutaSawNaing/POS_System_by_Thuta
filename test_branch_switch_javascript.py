"""Execute the dashboard branch-switch/draft code in Node (no browser libraries).

The DOM and network are small controlled doubles; the functions under test are
extracted verbatim, following the existing offline/cache JavaScript tests.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from test_offline_sync_javascript import _function, _run_node_script
from test_section_cache_javascript import _cache_source


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
SOURCE = (Path(__file__).parent / "templates/dashboard.html").read_text(encoding="utf-8")
DRAFTS = SOURCE.split("// ===== BRANCH UNSAVED WORK =====", 1)[1].split(
    "// ===== END BRANCH UNSAVED WORK =====", 1
)[0]


HARNESS = r"""
const assert = require('node:assert/strict');
const fields = {}, listeners = {}, notices = [], calls = [], toasts = [], cacheWrites = [];
let currentBranch = {id: 1, name: 'Original'}, cart = [], cachedCategories = [];
let pendingReceiptLogoFile = null, pendingReceiptLogoObjectUrl = null;
let pendingMMQRFile = null, pendingMMQRRemoval = false, currentLanguage = 'en';
let savedAgentAutonomy = false, isSaleProcessing = false, isSyncingPendingSales = false;
const APP_SETTINGS = {currencyCode:'USD', receiptPaperSize:'THERMAL_80MM',
 receiptCustomization: {brand_name:'Store',email:'',phone:'',address:'',footer_message:'Thanks'},
 labelGeometry:{label_width_mm:32,label_height_mm:19,label_gap_mm:3,label_columns:3}};
function element() { return {children:[], events:{}, setAttribute(){},
 appendChild(child){this.children.push(child)},
 addEventListener(name, fn){this.events[name]=fn}}; }
const document = {getElementById:id=>fields[id] || null,
 querySelectorAll:()=>[], createElement:element,
 addEventListener(name, fn){(listeners[name] ||= []).push(fn)},
 body:{prepend(el){notices.push(el)}}};
let reloads = 0, answer = {success:true,branch:{id:2,name:'Other'},message:'Switched'};
let responseOK = true, rejectNetwork = false, releaseNetwork = null;
let network = async (input, init) => {
 calls.push({input:String(input),init});
 if (releaseNetwork) await new Promise(resolve => releaseNetwork = resolve);
 if (rejectNetwork) throw Error('Network unavailable');
 return new Response(JSON.stringify(answer), {status:responseOK ? 200:400,
 headers:{'content-type':'application/json'}});
};
const window = {location:{origin:'http://localhost:5000',reload(){reloads++}},
 fetch:(input,init)=>network(input,init)};
let dialog = null;
function showConfirmDialog(title,message,onConfirm){dialog={title,message,onConfirm};}
function requireCapability(){return true;}
function hasCapability(){return true;}
function showToast(message,kind){toasts.push({message,kind});}
function offlineCacheSet(key,data){cacheWrites.push([key,data]);}
function clearPendingMMQR(){pendingMMQRFile=null;pendingMMQRRemoval=false;}
function updatePendingSalesBadge(){}
function setConnectionStatus(){}
function bindCurrentBranchUI(){throw Error('Must not repaint workflows before reload');}
const invalidatedBranches = [];
function invalidateProductsCache(){invalidatedBranches.push(currentBranch.id);}
const storage = {};
const localStorage = {getItem:k=>storage[k] || null,setItem:(k,v)=>storage[k]=String(v)};
const sessionStorage = {setItem:(k,v)=>storage[k]=String(v)};
const CURRENT_BRANCH_CACHE_KEY = 'current', BRANCH_SWITCH_RETURN_SECTION_KEY='return',
 ACTIVE_SECTION_STORAGE_KEY='section', CACHE_USER_ID=7, CACHE_USER_ROLE='manager';
function scopedOfflineCacheKey(k){return 'scoped:'+k;}
function field(id,value='',modal=null,type='text') {
 const f = {id,value,type,disabled:false,readOnly:false,isConnected:true,checked:false,
 matches:()=>true,closest:()=>modal,files:[]};
 fields[id]=f; return f;
}
function edit(f,value){listeners.focusin.forEach(fn=>fn({target:f}));f.value=value;}
function fire(name,target){(listeners[name] || []).forEach(fn=>fn({target}));}
field('branch-selector','2'); field('save-branch-selection-btn');
"""


def run_js(body, *, cache=False, extra=()):
    helpers = "\n".join(_function(SOURCE, name) for name in (
        "switchBranch", "saveSelectedBranch", "updateBranchSaveButton", "getPendingSales",
        "setPendingSales", "handleBranchStorageChange", *extra,
    ))
    if cache:
        # Real nativeFetch/smartFetch interception and cache read/write behaviour.
        cache_code = _cache_source()
        # Helpers above supply invalidation; avoid duplicate declarations in cache.
        prefix = HARNESS.replace(
            "function invalidateProductsCache(){invalidatedBranches.push(currentBranch.id);}", ""
        ) + "\nlet cachedProducts=[], productsCacheTimestamp=0, cachedProductsBranchId=null;\n" + r"""
function offlineCacheRemove(){}
function cachedProductsKey(){return 'products:'+currentBranch.id;}
function isBrowserOffline(){return false;}
"""
    else:
        prefix = HARNESS
        cache_code = "let dashboardBranchStale=false; const cacheStore=new Map(),cacheInflight=new Map();\n" + "\n".join(
            _function(SOURCE, name) for name in ("staleBranchResponse", "handleStaleBranch")
        )
    script = prefix + "\n" + DRAFTS + "\n" + cache_code + "\n" + helpers + "\n" + r"""
globalThis.fetch = """ + ("window.fetch" if cache else "(input,init)=>network(input,init)") + ";\n" + r"""
(async()=>{
""" + body + r"""
console.log(JSON.stringify({passed:true}));
})().catch(e=>{console.error(e);process.exit(1)});
"""
    try:
        assert _run_node_script(script)["passed"]
    except subprocess.CalledProcessError as error:
        pytest.fail(error.stdout + error.stderr)


def test_clean_same_branch_and_queued_only_do_not_nag_or_destroy_queue():
    run_js(r"""
setPendingSales([{transaction_id:'retained'}]);
const queued = JSON.stringify(storage);
await switchBranch(1);
assert.equal(calls.length,0); assert.equal(dialog,null);
await saveSelectedBranch();
await new Promise(resolve=>setTimeout(resolve,0));
assert.equal(calls.length,1); assert.equal(dialog,null); assert.equal(reloads,1);
assert.equal(currentBranch.id,2);
assert.deepEqual(invalidatedBranches,[1]); // old scope, not the destination snapshot
assert.equal(storage['pos_pending_sales_v2:7:1'],'[{"transaction_id":"retained"}]');
assert.deepEqual(getPendingSales(),[]); // new branch must not borrow the queue
assert.equal(storage.return,'settings'); assert.equal(storage.section,'settings');
assert.ok(queued.includes('retained'));
""")


@pytest.mark.parametrize("draft", [
    "cart=[{product_id:42,quantity:1}]",
    "edit(field('edit-name','Existing',{}),'Changed')",
    "field('settings-currency-code','MMK')",
    "field('settings-language','my')",
    "field('receipt-phone','123')",
    "field('receipt-brand-name','New brand')",
    "field('receipt-footer-message','New footer')",
    "field('label-columns','4')",
    "field('settings-ai-api-key','draft-secret')",
    "field('agent-autonomy-toggle').checked=true",
    "document.querySelectorAll=()=>[{value:'A4',checked:true,disabled:false}]",
    "pendingReceiptLogoFile={name:'logo.png'}",
    "pendingMMQRFile={name:'qr.png'}",
    "pendingMMQRRemoval=true",
])
def test_real_drafts_require_confirmation_before_any_side_effect(draft):
    run_js(draft + r""";
setPendingSales([{transaction_id:'retained'}]);
const before=JSON.stringify(storage), branchBefore=currentBranch;
await switchBranch('2');
assert.ok(dialog.title.includes('Discard unsaved work'));
assert.ok(dialog.message.includes('NOT be discarded'));
assert.ok(dialog.message.includes('original branch'));
assert.ok(!dialog.message.includes('draft-secret')); // never expose settings values
assert.equal(calls.length,0); assert.equal(reloads,0);
assert.equal(currentBranch,branchBefore); assert.equal(JSON.stringify(storage),before);
assert.equal(invalidatedBranches.length,0); assert.equal(cacheWrites.length,0);
// Cancel means no callback: all drafts remain. Explicit confirmation posts once.
const confirm=dialog.onConfirm;
await confirm();
assert.equal(calls.length,1); assert.equal(reloads,1);
assert.equal(storage['pos_pending_sales_v2:7:1'],'[{"transaction_id":"retained"}]');
""")


def test_unchanged_populated_forms_reverts_resets_and_saved_forms_are_clean():
    run_js(r"""
const root={contains:f=>f.closest()===root,querySelectorAll:()=>[name,check,select]};
const name=field('name','Existing',root),check=field('active','on',root,'checkbox');
const select=field('category','42',root,'select-one');
fields.editor=root;
fire('shown.bs.modal',root);
assert.deepEqual(getUnsavedBranchWork(),[]);
edit(name,'New'); assert.ok(getUnsavedBranchWork().includes('changed forms'));
name.value='Existing'; assert.deepEqual(getUnsavedBranchWork(),[]);
check.checked=true; assert.ok(getUnsavedBranchWork().includes('changed forms'));
check.checked=false; assert.deepEqual(getUnsavedBranchWork(),[]);
select.value='99'; assert.ok(getUnsavedBranchWork().includes('changed forms'));
markBranchFormSaved('editor'); assert.deepEqual(getUnsavedBranchWork(),[]);
edit(name,'Later'); fire('hidden.bs.modal',root);
assert.ok(getUnsavedBranchWork().includes('changed forms')); // closed is not saved
fire('shown.bs.modal',root);
assert.ok(getUnsavedBranchWork().includes('changed forms')); // draft remains on reopen
fire('hidden.bs.modal',root); name.value='Another loaded record';
fire('shown.bs.modal',root);
assert.deepEqual(getUnsavedBranchWork(),[]); // loader replaced the old draft
edit(name,'Another draft');
fire('reset',root); await Promise.resolve();
assert.deepEqual(getUnsavedBranchWork(),[]);
field('settings-currency-code','USD'); field('settings-language','en');
field('label-columns','3.0'); // equivalent numeric value
field('receipt-brand-name','Store'); field('agent-autonomy-toggle').checked=false;
assert.deepEqual(getUnsavedBranchWork(),[]);
await switchBranch(2); assert.equal(dialog,null); assert.equal(calls.length,1);
""")


@pytest.mark.parametrize("mode", ["network", "http", "application"])
def test_failed_switch_keeps_work_and_target_without_reloading_forms(mode):
    run_js({"network": "rejectNetwork=true;", "http": "responseOK=false;",
            "application": "answer={success:false,message:'Denied'};"}[mode] + r"""
cart=[{product_id:42}]; pendingMMQRFile={name:'draft.png'};
await switchBranch(2); await dialog.onConfirm();
assert.equal(currentBranch.id,1); assert.equal(reloads,0);
assert.equal(cart.length,1); assert.ok(pendingMMQRFile);
assert.equal(fields['branch-selector'].value,'2');
assert.equal(fields['save-branch-selection-btn'].disabled,false);
assert.equal(branchSwitchInFlight,false); assert.deepEqual(cacheWrites,[]);
assert.deepEqual(invalidatedBranches,[]);
""")


def test_inflight_switch_deduplicated_and_sale_activity_blocks_switch():
    run_js(r"""
isSaleProcessing=true; await switchBranch(2); assert.equal(calls.length,0);
isSaleProcessing=false; isSyncingPendingSales=true;
await switchBranch(2); assert.equal(calls.length,0);
isSyncingPendingSales=false;
releaseNetwork=true;
const pending=switchBranch(2); await switchBranch(2);
assert.equal(calls.length,1); assert.equal(fields['save-branch-selection-btn'].disabled,true);
releaseNetwork(); await pending; assert.equal(reloads,1);
""")


def test_file_checkbox_and_multiselect_drafts_compare_actual_values():
    run_js(r"""
const root={};
const photo=field('photo','',root,'file');
fire('focusin',photo); photo.files=[{name:'new.png',size:42,lastModified:1}];
assert.ok(getUnsavedBranchWork().includes('changed forms'));
photo.files=[]; assert.deepEqual(getUnsavedBranchWork(),[]);
const multi=field('multi','',root,'select-multiple');
multi.multiple=true;multi.options=[{value:'one',selected:true},{value:'two',selected:false}];
fire('focusin',multi); multi.options[1].selected=true;
assert.ok(getUnsavedBranchWork().includes('changed forms'));
multi.options[1].selected=false; assert.deepEqual(getUnsavedBranchWork(),[]);
""")


def test_successful_settings_save_values_are_not_sticky_drafts():
    run_js(r"""
field('settings-currency-code','MMK'); field('settings-language','my');
field('receipt-brand-name','New brand'); field('label-columns','4');
field('agent-autonomy-toggle').checked=true;
assert.ok(getUnsavedBranchWork().includes('unsaved settings'));
// Save callbacks update their persisted models independently; no generic dirty
// flag may continue nagging after all corresponding saves succeeded.
APP_SETTINGS.currencyCode='MMK'; currentLanguage='my';
APP_SETTINGS.receiptCustomization.brand_name='New brand';
APP_SETTINGS.labelGeometry.label_columns=4; savedAgentAutonomy=true;
assert.deepEqual(getUnsavedBranchWork(),[]);
await switchBranch(2);assert.equal(dialog,null);assert.equal(reloads,1);
""")


def test_storage_branch_change_preserves_drafts_until_explicit_reload():
    run_js(r"""
cart=[{product_id:42}]; setPendingSales([{transaction_id:'retained'}]);
const event={key:'scoped:current',newValue:JSON.stringify({data:{id:2}})};
handleBranchStorageChange({...event,key:'unrelated'});
handleBranchStorageChange({...event,newValue:'invalid JSON'});
assert.equal(dashboardBranchStale,false);
handleBranchStorageChange(event); handleBranchStorageChange(event);
assert.equal(reloads,0); assert.equal(notices.length,1); assert.equal(currentBranch.id,1);
assert.equal(cart.length,1); assert.equal(getPendingSales().length,1);
assert.ok(notices[0].children[0].textContent.includes('syncing are paused'));
await switchBranch(2); assert.equal(calls.length,0);
notices[0].children[1].events.click(); assert.equal(reloads,0);
assert.ok(dialog.message.includes('NOT be discarded'));
dialog.onConfirm(); assert.equal(reloads,1);
""")


def test_storage_branch_change_without_drafts_reloads_without_confirmation():
    run_js(r"""
setPendingSales([{transaction_id:'retained'}]);
handleBranchStorageChange({key:'scoped:current',newValue:JSON.stringify({data:{id:2}})});
assert.equal(reloads,1); assert.equal(dialog,null); assert.equal(notices.length,0);
assert.equal(getPendingSales().length,1);
""")


def test_cached_current_branch_cannot_relabel_an_existing_draft():
    run_js(r"""
cart=[{product_id:42}];
storage['scoped:current']=JSON.stringify({data:{id:2,name:'Other'}});
applyCachedCurrentBranch();
assert.equal(currentBranch.id,1); assert.equal(reloads,0);
assert.equal(dashboardBranchStale,true); assert.equal(notices.length,1);
""", extra=("applyCachedCurrentBranch", "offlineCacheGet"))


def test_api_branch_mismatch_blocks_cache_checkout_and_sync_without_dropping_queue():
    run_js(r"""
cart=[{product_id:42}]; setPendingSales([{transaction_id:'retained',saleData:{}}]);
network=async(input,init)=>{
 calls.push({input:String(input),init});
 return new Response(JSON.stringify({success:false,code:'stale_branch'}),{status:409,
 headers:{'content-type':'application/json','x-pos-branch-mismatch':'1'}});
};
// In-progress sync receiving 409 must retain even a non-split-payment sale.
await syncPendingSales();
assert.equal(getPendingSales().length,1); assert.equal(dashboardBranchStale,true);
assert.equal(reloads,0); assert.equal(notices.length,1);
const count=calls.length;
const response=await fetch('/api/products');
assert.equal(response.status,409); assert.equal((await response.json()).code,'stale_branch');
await fetch('/api/products',{method:'POST',body:'{}'});
await syncPendingSales(); completeSale();
assert.equal(queueOfflineSale({},null),false);
assert.equal(calls.length,count); assert.equal(cart.length,1);
assert.equal(getPendingSales().length,1);
""", cache=True, extra=("syncPendingSales", "queueOfflineSale", "completeSale"))