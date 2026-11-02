"""Execute the shared CAPTCHA renderer and chat expansion controls in Node."""

import subprocess
from pathlib import Path

from test_dashboard_money_javascript import NODE, _javascript_function, pytestmark


ROOT = Path(__file__).resolve().parent


def test_shared_captcha_renderer_and_template_wiring():
    renderer = (ROOT / 'public/captcha.js').read_text(encoding='utf-8')
    for name in ('login.html', 'dashboard.html'):
        template = (ROOT / 'templates' / name).read_text(encoding='utf-8')
        assert '/public/captcha.css' in template
        assert '/public/captcha.js' in template
        assert 'renderSecurityCaptcha(' in template
    script = renderer + r'''
const assert = require('node:assert/strict');
const document = {createElement: () => ({style: {}})};
const display = {style: {}, children: [],
  replaceChildren() { this.children = []; },
  appendChild(child) { this.children.push(child); }};
renderSecurityCaptcha(display, '1234');
assert.equal(display.children.map(child => child.textContent).join(''), '1234');
assert.equal(display.children.length, 4);
renderSecurityCaptcha(display, '9876');
assert.equal(display.children.map(child => child.textContent).join(''), '9876');
assert.equal(display.children.length, 4);
'''
    subprocess.run([NODE, '-e', script], check=True, capture_output=True, text=True)


def test_chat_expands_centers_and_restores_without_touching_messages():
    widget = (ROOT / 'templates/ai_agent_widget.html').read_text(encoding='utf-8')
    assert "$('loliExpand').addEventListener('click',toggleExpanded)" in widget
    assert 'aria-controls="loliPanel"' in widget
    assert '.loli-panel.is-expanded' in widget
    functions = '\n'.join(_javascript_function(widget, name)
                          for name in ('placePanel', 'toggleExpanded'))
    script = functions + r'''
const assert = require('node:assert/strict');
let isOpen = true, isMobile = false;
const mobile = () => isMobile, margin = 12;
const window = {innerWidth: 1440, innerHeight: 900};
const classes = new Set();
const panel = {
  classList: {contains: name => classes.has(name), toggle(name) {
    if (classes.has(name)) { classes.delete(name); return false; }
    classes.add(name); return true;
  }},
  get offsetWidth() { return classes.has('is-expanded') ? 960 : 390; },
  get offsetHeight() { return classes.has('is-expanded') ? 876 : 560; },
  dataset: {}, style: {setProperty(name, value) { this[name] = value; }}
};
const button = {setAttribute(name, value) { this[name] = value; }};
const $ = () => button;
const head = {getBoundingClientRect: () => ({left: 1360, right: 1420, top: 400, height: 60})};
const headSide = () => 'right';
placePanel();
const original = {left: panel.style.left, top: panel.style.top};
toggleExpanded();
assert.equal(button['aria-expanded'], 'true');
assert.equal(button['aria-label'], 'Collapse chat');
assert.equal(panel.style.left, '240px');
assert.equal(panel.style.top, '12px');
window.innerWidth = 1200;
placePanel();
assert.equal(panel.style.left, '120px');
window.innerWidth = 1440;
toggleExpanded();
assert.equal(button['aria-expanded'], 'false');
assert.equal(button['aria-label'], 'Expand chat');
assert.equal(panel.style.left, original.left);
assert.equal(panel.style.top, original.top);
isMobile = true;
panel.style.left = '0'; panel.style.top = '0';
placePanel();
assert.equal(panel.style.left, '0');
assert.equal(panel.style.top, '0');
'''
    subprocess.run([NODE, '-e', script], check=True, capture_output=True, text=True)