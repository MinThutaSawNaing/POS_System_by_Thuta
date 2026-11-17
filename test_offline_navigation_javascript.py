"""Exercise the actual offline-only reload guard with browser event doubles."""
import shutil
from pathlib import Path

import pytest

from test_offline_sync_javascript import _function, _run_node_script

ROOT = Path(__file__).parent
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node required")


def test_reload_protection_only_applies_during_browser_or_server_outages():
    source = (ROOT / "templates/dashboard.html").read_text(encoding="utf-8")
    helpers = "\n".join(_function(source, name) for name in (
        "isOfflineNavigationProtected", "initOfflineNavigationProtection",
    ))
    _run_node_script(r"""
const assert = require('node:assert/strict');
let offline = false, checkoutServerUnavailable = false;
function isBrowserOffline() { return offline; }
const listeners = {}, warnings = [];
const window = {addEventListener(type, callback) { listeners[type] = callback; }};
function showToast(message, kind) { warnings.push({message, kind}); }
""" + helpers + r"""
initOfflineNavigationProtection();
function event(key, modifiers = {}) {
  return {key, prevented: false, stopped: false, ...modifiers,
    preventDefault() { this.prevented = true; },
    stopImmediatePropagation() { this.stopped = true; }};
}
for (const state of ['online', 'browser-offline', 'server-offline', 'reconnected']) {
  offline = state === 'browser-offline';
  checkoutServerUnavailable = state === 'server-offline';
  const protectedMode = offline || checkoutServerUnavailable;
  for (const [key, modifiers] of [['F5', {}], ['F5', {ctrlKey:true}],
      ['r', {ctrlKey:true}], ['R', {ctrlKey:true, shiftKey:true}],
      ['r', {metaKey:true}], ['R', {metaKey:true, shiftKey:true}]]) {
    const e = event(key, modifiers);
    listeners.keydown(e);
    assert.equal(e.prevented, protectedMode, state + ':' + key);
    assert.equal(e.stopped, protectedMode);
  }
  for (const key of ['r', 'Enter', 'F2', 'Escape']) {
    const e = event(key); listeners.keydown(e);
    assert.equal(e.prevented, false, 'ordinary keys stay usable');
  }
  const unload = event();
  listeners.beforeunload(unload);
  assert.equal(unload.prevented, protectedMode, state + ':unload');
  assert.equal(unload.returnValue, protectedMode ? true : undefined);
}
assert.equal(warnings.length, 12);
assert.ok(warnings.every(w => w.kind === 'warning' && /offline/i.test(w.message)));
console.log(JSON.stringify({passed:true}));
""")
    assert "initOfflineNavigationProtection();" in source