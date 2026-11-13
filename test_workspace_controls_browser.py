"""Check the purchasing filters, catalog paging and avatar radios in real Chrome."""
import re
import subprocess
from pathlib import Path

import pytest

from test_delivery_actions_browser import BROWSER
from test_offline_sync_javascript import _function


ROOT = Path(__file__).parent


@pytest.mark.skipif(not BROWSER, reason="Local Chrome/Chromium/Edge required")
@pytest.mark.parametrize("width,height", [(1440, 900), (1024, 768), (390, 844)])
def test_workspace_controls_fit_and_remain_clickable(tmp_path, width, height):
    source = (ROOT / "templates/dashboard.html").read_text(encoding="utf-8")
    styles = re.search(r"<style>(.*?)</style>", source, re.S).group(1)
    purchases = source.split('id="purchases-section"', 1)[1]
    filters = purchases.split('        <!-- Filters -->', 1)[1].split('        <div class="card">', 1)[0]
    radios = source[source.index('            <fieldset class="mb-3 border-bottom pb-3"'):source.index('            <div class="mb-3">', source.index('id="settings-ai-avatar-help"'))]
    paging = re.search(r'loadMoreCell.innerHTML = `(.*?)`;', source, re.S).group(1)
    functions = "\n".join(_function(source, name) for name in (
        "makeRemoteInventorySelect", "aiAvatarPreferenceKey", "loadAIAvatarPreference", "saveAIAvatarPreference",
    ))
    script = r"""
const CACHE_USER_ID='browser-test', events=[];
function inventoryIdentity(){return 'browser-test';}
function requireCapability(){return true;}
function showToast(){}
async function fetch(){return {ok:true,json:async()=>[{id:1,name:'Supplier One'}]};}
function check(ok,message){if(!ok)throw Error(message);}
let clicks=0;
function loadMorePOSProducts(){clicks++;}
window.addEventListener('pos-ai-avatar-visibility',event=>events.push(event.detail.enabled));
(async()=>{
  makeRemoteInventorySelect(document.getElementById('purchase-orders-supplier-filter'),'/suppliers','Supplier filter');
  await new Promise(resolve=>setTimeout(resolve,100));
  const fields=[...document.querySelector('.purchase-filters').children].slice(0,5);
  const labels=fields.map(field=>field.querySelector('label').getBoundingClientRect());
  if(innerWidth>=1200) check(labels.every(rect=>Math.abs(rect.top-labels[0].top)<1),'Desktop labels must align');
  for(const field of fields) check(field.scrollWidth<=field.clientWidth,'Filter must not overflow');
  check(document.documentElement.scrollWidth<=innerWidth,'Page must fit viewport');
  const pane=document.querySelector('.pos-products-scroll'), grid=document.getElementById('product-grid');
  const button=grid.querySelector('button');
  for(let i=0;i<50;i++){
    const card=document.createElement('div');card.className='card product-card';card.style.height='160px';
    grid.insertBefore(card,grid.lastElementChild);
  }
  for(const scroll of [0,200,pane.scrollHeight]){
    pane.scrollTop=scroll;
    pane.scrollIntoView({block:'center',behavior:'instant'});
    const rect=button.getBoundingClientRect(), bounds=pane.getBoundingClientRect();
    check(rect.height>=48,'Load more needs a large touch target');
    check(rect.top>=bounds.top&&rect.bottom<=bounds.bottom,'Load more must stay in the product pane');
    const hit=document.elementFromPoint(rect.left+rect.width/2,rect.top+rect.height/2);
    check(hit===button,'Load more must not be covered: '+JSON.stringify({scroll,top:rect.top,bottom:rect.bottom,height:innerHeight,hit:hit?.outerHTML.slice(0,200)}));
    button.click();
  }
  check(clicks===3,'Load more must respond to clicks');
  localStorage.removeItem(aiAvatarPreferenceKey());loadAIAvatarPreference();
  const on=document.getElementById('settings-ai-avatar-on'),off=document.getElementById('settings-ai-avatar-off');
  check(on.checked&&!off.checked,'On should be the default');
  off.closest('label').click();check(off.checked&&!on.checked,'Radio choices must be exclusive');
  check(events.at(-1)===true,'Selection must wait for Save');
  document.getElementById('save-ai-avatar-btn').click();check(events.at(-1)===false,'Save must hide the avatar');
  on.click();loadAIAvatarPreference();check(off.checked&&!on.checked,'Saved choice must restore');
  localStorage.removeItem(aiAvatarPreferenceKey());
  document.body.dataset.testResult='passed';
})().catch(error=>{document.body.dataset.testResult='failed';document.body.append(error.stack);});
"""
    css = (ROOT / "public/vendor/bootstrap/bootstrap.min.css").read_text(encoding="utf-8")
    page = tmp_path / "controls.html"
    page.write_text(
        '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<style>{css}\n{styles}</style></head><body><main style="padding:16px">{filters}'
        '<div class="pos-products-scroll" style="max-height:240px"><div id="product-grid">'
        f'<div class="pos-load-more-cell">{paging}</div></div></div>{radios}</main>'
        f'<script>{functions}\n{script}</script></body></html>', encoding="utf-8",
    )
    result = subprocess.run([
        BROWSER, "--headless", "--disable-gpu", "--no-sandbox",
        f"--user-data-dir={tmp_path / 'profile'}", f"--window-size={width},{height}",
        "--virtual-time-budget=3000", "--dump-dom", page.as_uri(),
    ], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert 'data-test-result="passed"' in result.stdout, result.stdout[-6000:] + result.stderr[-1000:]