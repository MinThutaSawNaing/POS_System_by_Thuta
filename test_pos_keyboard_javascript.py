"""Keyboard-only cashier navigation and checkout safety."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_offline_sync_javascript import _function


SOURCE = (Path(__file__).parent / "templates" / "dashboard.html").read_text(encoding="utf-8")
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js required")


def run_keys(body):
    helpers = "\n".join(_function(SOURCE, name) for name in ("initPOSQuickAccessKeys",))
    script = """
const assert = require('node:assert/strict');
let currentSection = 'pos', actions = [], handler;
const section = {contains: node => node && node.inPOS !== false};
const cart = {contains: node => node && node.inCart === true};
const nodes = Object.fromEntries(['product-search','payment-method','cash-received','split-amount-one',
  'split-amount-two','pos-price-check-query','pos-coupon-code','pos-hold-sale','pos-held-sales-toggle',
  'pos-price-check-toggle','pos-coupon-toggle','complete-sale-btn'].map(id => [id, {
    id, tagName: id === 'payment-method' ? 'SELECT' : id.includes('toggle') || id.includes('-sale') ? 'BUTTON' : 'INPUT',
    inCart: ['payment-method','cash-received','split-amount-one','split-amount-two','pos-coupon-code','complete-sale-btn'].includes(id),
    disabled:false, click(){actions.push(id)}, focus(){actions.push('focus:' + id)}, getAttribute(){return null}, value:''
  }]));
const document = {activeElement:nodes['product-search'], getElementById:id => id === 'pos-section' ? section : id === 'pos-cart-column' ? cart : nodes[id],
  querySelector:()=>null, addEventListener:(name,fn)=>{if(name==='keydown') handler=fn}};
function focusPOSSearchInput(){ actions.push('search'); }
function clearPOSSearchAndFilter(){ actions.push('reset'); }
let posCategoryFilterId = null;
""" + helpers + "\n" + """
function press(key, id='product-search', options={}) {
  document.activeElement=nodes[id] || {id,tagName:'INPUT',inPOS:options.inPOS !== false,inCart:options.inCart === true,getAttribute:()=>null};
  const event={key,target:document.activeElement,defaultPrevented:false,repeat:false,isComposing:false,
    ctrlKey:false,altKey:false,metaKey:false,preventDefault(){this.defaultPrevented=true},...options};
  handler(event); return event.defaultPrevented;
}
initPOSQuickAccessKeys();
""" + body + "\nconsole.log(JSON.stringify(actions));"
    result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_shortcuts_reach_operational_controls_without_mouse():
    actions = run_keys("""
press('F2'); press('F3'); press('F4'); press('F6'); press('F7'); press('F8'); press('F9');
""")
    assert actions == ['search', 'pos-price-check-toggle', 'focus:payment-method',
                       'pos-hold-sale', 'pos-held-sales-toggle', 'pos-coupon-toggle', 'complete-sale-btn']


def test_enter_checks_out_only_from_tender_fields_or_ctrl_enter_in_cart():
    actions = run_keys("""
assert.equal(press('Enter','cash-received'),true);
assert.equal(press('Enter','split-amount-one'),true);
assert.equal(press('Enter','split-amount-two'),true);
assert.equal(press('Enter','pos-coupon-code'),false);
assert.equal(press('Enter','pos-price-check-query'),false);
assert.equal(press('Enter','product-search'),false);
assert.equal(press('Enter','payment-method'),false);
assert.equal(press('Enter','pos-hold-sale'),false);
assert.equal(press('Enter','cash-received',{repeat:true}),false);
assert.equal(press('Enter','cash-received',{isComposing:true}),false);
assert.equal(press('Enter','payment-method',{ctrlKey:true}),false);
assert.equal(press('Enter','pos-coupon-code',{ctrlKey:true}),false);
assert.equal(press('Enter','other',{ctrlKey:true,inCart:true}),true);
""")
    assert actions == ['complete-sale-btn'] * 4


def test_shortcuts_ignore_dialog_other_section_other_fields_and_disabled_hold():
    actions = run_keys("""
currentSection='sales'; press('F9'); currentSection='pos';
press('F9','other',{inPOS:false});
nodes['pos-hold-sale'].disabled=true; press('F6');
press('F9','cash-received',{ctrlKey:true});
press('F9','cash-received',{altKey:true});
press('F9','cash-received',{defaultPrevented:true});
document.querySelector=()=>({}); press('F9');
""")
    assert actions == []


def test_product_cards_and_cart_buttons_are_keyboard_accessible():
    assert SOURCE.count('role="button" tabindex="0" aria-label="Add ${escapeHtml(product.name)} to sale"') == 3
    assert 'aria-label="Decrease quantity of ${escapeHtml(item.name)}"' in SOURCE
    assert 'aria-label="Increase quantity of ${escapeHtml(item.name)}"' in SOURCE
    assert 'aria-label="Remove ${escapeHtml(item.name)} from sale"' in SOURCE
    assert 'onkeydown="if (event.key === \'Enter\' && !isPOSScannerEnter()) { event.preventDefault(); searchProducts(); }"' in SOURCE
    assert 'function isPOSScannerEnter()' in SOURCE