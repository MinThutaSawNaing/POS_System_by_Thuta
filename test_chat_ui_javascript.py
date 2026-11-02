"""Runtime regressions for chat keyboard geometry and opening focus policy."""

import re
import subprocess
from pathlib import Path

from test_dashboard_money_javascript import NODE, _javascript_function, pytestmark


SOURCE = (Path(__file__).parent / 'templates/ai_agent_widget.html').read_text(encoding='utf-8')


def run_ui_script(body):
    functions = '\n'.join(_javascript_function(SOURCE, name) for name in (
        'syncMobileViewport', 'handleViewportResize', 'watchViewport', 'setOpen',
    ))
    harness = r'''
const assert = require('node:assert/strict');
let isOpen = false, small = true, placements = 0, restores = 0, headPlacements = 0;
const mobile = () => small;
function element() {
  const classes = new Set();
  return {style: {setProperty(k,v) { this[k] = v; }, removeProperty(k) { delete this[k]; }},
    classList: {contains: k => classes.has(k), add: k => classes.add(k),
      remove: k => classes.delete(k), toggle(k, value) { value ? classes.add(k) : classes.delete(k); }},
    setAttribute(k,v) { this[k] = v; }, focusCalls: 0, focus() { this.focusCalls++; },
    blurCalls: 0, blur() { this.blurCalls++; }};
}
const workspace = element(), panel = element(), head = element(), input = element(), unread = element();
const messages = {scrollTop: 240, scrollHeight: 1500};
panel.contains = node => node === input;
const document = {activeElement: input};
const timers = [];
const setTimeout = callback => timers.push(callback);
const flush = () => { while (timers.length) timers.shift()(); };
const listeners = {}, viewportListeners = {};
const window = {innerWidth: 390, innerHeight: 844,
  addEventListener(name, callback) { listeners[name] = callback; },
  visualViewport: {height: 844, width: 390, offsetTop: 0, offsetLeft: 0,
    addEventListener(name, callback) { viewportListeners[name] = callback; }}};
function placePanel() { placements++; panel.style.top = '12px'; }
function restorePosition() { restores++; }
function placeHead() { headPlacements++; }
function currentPosition() { return {x: 300, y: 700}; }
function hideMenu() {}
'''
    result = subprocess.run([NODE, '-e', harness + functions + body], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_mobile_keyboard_resize_scroll_offsets_and_close_cleanup():
    run_ui_script(r'''
watchViewport();
assert.equal(listeners.resize, handleViewportResize);
assert.equal(viewportListeners.resize, syncMobileViewport);
assert.equal(viewportListeners.scroll, syncMobileViewport);
setOpen(true); flush();
assert.equal(input.focusCalls, 0);
assert.equal(panel.style.top, '0px');
assert.equal(workspace.style['--loli-viewport-height'], '844px');
window.visualViewport.height = 390;
viewportListeners.resize();
assert.equal(workspace.style['--loli-viewport-height'], '390px');
window.visualViewport.offsetTop = 95;
window.visualViewport.offsetLeft = 4;
viewportListeners.scroll();
assert.equal(panel.style.top, '95px');
assert.equal(panel.style.left, '4px');
assert.equal(messages.scrollTop, 240, 'Do not interrupt reading old messages');
assert.equal(placements, 0);
window.visualViewport.height = 844;
window.visualViewport.offsetTop = 0;
viewportListeners.resize();
assert.equal(panel.style.top, '0px');
setOpen(false); flush();
assert.equal(input.blurCalls, 1);
assert.equal(head.focusCalls, 1);
assert.equal(workspace.classList.contains('is-mobile-open'), false);
assert.equal(workspace.style['--loli-viewport-height'], undefined);
viewportListeners.scroll();
assert.equal(workspace.style['--loli-viewport-width'], undefined);
''')


def test_fallback_breakpoint_changes_and_desktop_focus_races():
    run_ui_script(r'''
window.visualViewport = undefined;
watchViewport();
setOpen(true); flush();
assert.equal(workspace.style['--loli-viewport-height'], '844px');
assert.equal(workspace.style['--loli-viewport-width'], '390px');
window.innerHeight = 350;
listeners.resize();
assert.equal(workspace.style['--loli-viewport-height'], '350px');
small = false;
listeners.resize();
assert.equal(restores, 1);
assert.equal(placements, 1);
assert.equal(workspace.style['--loli-viewport-height'], undefined);
setOpen(false); flush();
setOpen(true); flush();
assert.equal(input.focusCalls, 1);
setOpen(false); setOpen(true); setOpen(false); flush();
assert.equal(input.focusCalls, 1, 'Closed panel must not steal focus');
setOpen(true); small = true; listeners.resize(); flush();
assert.equal(input.focusCalls, 1, 'Mobile breakpoint must cancel pending autofocus');
assert.equal(workspace.classList.contains('is-mobile-open'), true);
''')


def test_typography_safe_areas_and_independent_message_scrolling():
    def rule(selector):
        return re.search(re.escape(selector) + r'\s*\{([^}]+)\}', SOURCE).group(1)

    assert 'font-size:16px' in rule('.loli-message__bubble')
    assert 'font-size:16px' in rule('.loli-input-row textarea')
    assert 'font-size:16px' in rule('.loli-memory__form textarea')
    # An ancestor with touch-action:none disables native message scrolling too.
    assert 'touch-action:auto' in rule('.loli-workspace')
    assert 'touch-action:none' in rule('.loli-chat-head')
    scroll = rule('.loli-messages')
    for declaration in ('min-height:0', 'flex:1', 'overflow-y:auto', 'overscroll-behavior:contain'):
        assert declaration in scroll
    assert '.loli-header,.loli-composer,.loli-typing,.loli-error { flex-shrink:0; }' in SOURCE
    for side in ('top', 'bottom', 'left', 'right'):
        assert f'env(safe-area-inset-{side})' in SOURCE
    assert 'height:var(--loli-viewport-height,100dvh)' in SOURCE
    assert 'watchViewport();' in SOURCE