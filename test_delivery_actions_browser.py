"""Exercise the delivery workspace in a real local browser, without POS data."""
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest
from jinja2 import Environment, FileSystemLoader

from test_offline_sync_javascript import _function


ROOT = Path(__file__).parent
BROWSER = next((str(path) for path in (
    Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
) if path.is_file()), None) or shutil.which("chromium") or shutil.which("google-chrome")


@pytest.mark.skipif(not BROWSER, reason="Local Chrome/Chromium/Edge required")
@pytest.mark.parametrize("width,height", [(1280, 1000), (390, 844)])
def test_delivery_workspace_real_dom_and_responsive_layout(tmp_path, width, height):
    source = (ROOT / "templates/dashboard.html").read_text(encoding="utf-8")
    rendered = Environment(loader=FileSystemLoader(ROOT / "templates")).from_string(source).render(
        session={"role": "manager", "username": "Operator"},
        pos_name="Parrot POS", currency_code="MMK", currency_suffix="MMK",
    )
    styles = re.search(r"<style>(.*?)</style>", rendered, re.S).group(1)
    modal = rendered[rendered.index('    <div class="modal fade" id="deliveryOperationsModal"'):rendered.index("    <!-- Add Product Modal -->")]
    workbench = source[source.index("      const deliveryWorkbench ="):source.index("      function openCreateDeliveryModal()")]
    update = source[source.index("      async function openUpdateDeliveryModal("):source.index("      function printDeliverySlip(")]
    script = r"""
let allowed=true, dashboardSessionStale=false, dashboardBranchStale=false;
const CURRENT_USER_ROLE='manager';
function inventoryIdentity(){return 'browser-test';}
function hasCapability(){return allowed;}function requireCapability(){return allowed;}
function formatCurrency(value){return value+' MMK';}
function showToast(message){throw new Error(message);}
let printed=0;function printDeliverySlip(){printed++;}
async function fetch(url){return {ok:true,json:async()=>url.endsWith('/history')?{items:[]}:{
 id:7,delivery_number:'D-0007',stage:'delivering',recipient_name:'Aye Aye',recipient_phone:'09 123 456',
 delivery_address:'42 Market Street',instructions:'Call on arrival',cod:{status:'pending',expected_amount:'25000'},proofs:[]
}};}
function check(condition,message){if(!condition)throw new Error(message);}
""" + _function(source, "showModalAfterHiding") + workbench + update + r"""
(async()=>{
 const shown=new Promise(resolve=>document.getElementById('deliveryOperationsModal').addEventListener('shown.bs.modal',resolve,{once:true}));
 await openDeliveryOperations(7);
 await shown;
 const modal=document.getElementById('deliveryOperationsModal');
 const picker=document.getElementById('delivery-action');
 const panels=()=>[...modal.querySelectorAll('[data-delivery-panel]')].filter(p=>!p.hidden);
 check(picker.value==='attempts','Delivery attempt should be suggested');
 check(panels().length===1,'Exactly one task must be visible');
 const reason=document.getElementById('delivery-attempt-reason');reason.value='Keep draft';
 for(const action of ['photo','signature','carrier','edit','print','overview','attempts']){
  picker.value=action;picker.dispatchEvent(new Event('change',{bubbles:true}));
  check(panels().length===1&&panels()[0].dataset.deliveryPanel===action,'Wrong visible task: '+action);
 }
 check(reason.value==='Keep draft','Changing tasks must keep drafts');
 selectDeliveryAction('signature');
 const canvas=document.getElementById('delivery-signature');
 check(canvas.getBoundingClientRect().width>0,'Signature canvas should be usable');
 selectDeliveryAction('print');await runDeliveryAction('print');check(printed===1,'Printing should be reachable');
 selectDeliveryAction('attempts');
 allowed=false;syncDeliveryOperationForms(deliveryWorkbench.detail);
 check(picker.value==='overview','Read-only users should review details');
 check([...picker.options].map(o=>o.value).join(',')==='print,overview','Write actions must be absent');
 allowed=true;selectDeliveryAction('attempts');
 await new Promise(resolve=>setTimeout(resolve,400));
 check(document.documentElement.scrollWidth<=innerWidth,'Page must not overflow horizontally');
 check(modal.querySelector('.modal-content').getBoundingClientRect().width<=innerWidth,'Dialog must fit viewport');
 check(modal.querySelector('.modal-body').scrollWidth<=modal.querySelector('.modal-body').clientWidth,'Dialog body must not overflow horizontally');
 check(getComputedStyle(panels()[0]).display!=='none','Selected form must actually render');
 document.body.dataset.testResult='passed';
})().catch(error=>{document.body.dataset.testResult='failed';document.body.append(error.stack);});
"""
    css = (ROOT / "public/vendor/bootstrap/bootstrap.min.css").read_text(encoding="utf-8")
    bootstrap = (ROOT / "public/vendor/bootstrap/bootstrap.bundle.min.js").read_text(encoding="utf-8")
    page = tmp_path / "delivery.html"
    page.write_text(
        '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<style>{css}\n{styles}</style></head><body>{modal}<script>{bootstrap}</script>'
        f'<script>{script}</script></body></html>', encoding="utf-8",
    )
    result = subprocess.run([
        BROWSER, "--headless", "--disable-gpu", "--no-sandbox", "--no-first-run",
        "--no-default-browser-check", f"--user-data-dir={tmp_path / 'profile'}",
        f"--window-size={width},{height}", "--virtual-time-budget=3000", "--dump-dom", page.as_uri(),
    ], capture_output=True, text=True, encoding="utf-8", timeout=45)
    assert result.returncode == 0, result.stderr
    assert 'data-test-result="passed"' in result.stdout, result.stdout[-5000:] + result.stderr[-1000:]