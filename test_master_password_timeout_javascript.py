"""Behavioural tests for the Users-tab master-password session.

The unlock must not survive an idle tab or a move to another tab. These tests
execute the real dashboard helpers in Node with fake timers and a mocked API.
"""

import shutil
from pathlib import Path

import pytest

from test_offline_sync_javascript import _function, _run_node_script

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node required")

SOURCE = (Path(__file__).parent / 'templates' / 'dashboard.html').read_text(encoding='utf-8')


def run_timeout():
    helpers = "\n".join(_function(SOURCE, name) for name in (
        'renderAccountBarrierState', 'clearAccountBarrierIdleTimer',
        'scheduleAccountBarrierIdleTimer', 'lockAccountBarrier',
        'noteAccountBarrierActivity', 'syncAccountBarrierFromServer',
    ))
    return _run_node_script(r"""
const assert = require('node:assert/strict');
let currentSection = 'users';
let accountBarrierUnlocked = false;
let accountBarrierIdleTimer = null;
let accountBarrierLastTouchAt = 0;
let accountBarrierStateGeneration = 0;
const ACCOUNT_BARRIER_IDLE_MS = 10 * 60 * 1000;
const ACCOUNT_BARRIER_TOUCH_MIN_MS = 30 * 1000;
const badge = {className: '', innerHTML: ''};
const document = {getElementById: id => id === 'users-master-lock-status' ? badge : null};
const notices = [], requests = [];
function showToast(message, kind) { notices.push({message, kind}); }
function hasManagerOrBossAccess() { return true; }
const activeTimers = new Map();
let nextTimerId = 1;
const setTimeout = (fn, ms) => { const id = nextTimerId++; activeTimers.set(id, {fn, ms}); return id; };
const clearTimeout = (id) => activeTimers.delete(id);
let statusResponse = {success: true, unlocked: true, expires_in: 120};
let touchResponse = {success: true, unlocked: true, expires_in: 600};
async function fetch(url) {
  requests.push(url);
  const body = url.includes('/status') ? statusResponse
    : url.includes('/touch') ? touchResponse : {success: true, unlocked: false};
  return {ok: true, status: 200, json: async () => body};
}
const settle = async () => { for (let i = 0; i < 4; i++) await new Promise(r => setImmediate(r)); };
""" + helpers + r"""
(async () => {
  // Locking while already unlocked releases the server-side session too.
  accountBarrierUnlocked = true;
  lockAccountBarrier('locked by test');
  assert.equal(accountBarrierUnlocked, false);
  assert.ok(requests.includes('/api/account_barrier/lock'));
  assert.match(badge.innerHTML, /locked/i);
  assert.equal(notices.at(-1).kind, 'warning');

  // The idle timer locks locally and warns the manager.
  accountBarrierUnlocked = true;
  scheduleAccountBarrierIdleTimer(1500);
  assert.equal([...activeTimers.values()][0].ms, 1500);
  const timer = [...activeTimers.values()][0];
  activeTimers.clear();
  timer.fn();
  assert.equal(accountBarrierUnlocked, false);
  assert.match(notices.at(-1).message, /expired after inactivity/);

  // Entering Users adopts the server's remaining window (not a fresh full one).
  statusResponse = {success: true, unlocked: true, expires_in: 90};
  assert.equal(await syncAccountBarrierFromServer(), true);
  assert.equal(accountBarrierUnlocked, true);
  assert.equal([...activeTimers.values()][0].ms, 90000);
  assert.match(badge.innerHTML, /unlocked/i);

  // An expired server session clears the local unlock instead of trusting it.
  statusResponse = {success: true, unlocked: false, expires_in: 0};
  assert.equal(await syncAccountBarrierFromServer(), false);
  assert.equal(accountBarrierUnlocked, false);
  assert.equal(activeTimers.size, 0);

  // Activity in Users slides the window and touches the server.
  accountBarrierUnlocked = true;
  accountBarrierLastTouchAt = 0;
  noteAccountBarrierActivity();
  await settle();
  assert.ok(requests.includes('/api/account_barrier/touch'));
  assert.ok([...activeTimers.values()].some(t => t.ms === ACCOUNT_BARRIER_IDLE_MS));

  // An activity reply that arrives after leaving Users cannot resurrect it.
  accountBarrierUnlocked = true;
  accountBarrierLastTouchAt = 0;
  currentSection = 'products';
  noteAccountBarrierActivity();
  await settle();
  assert.equal(accountBarrierUnlocked, true);  // untouched here...
  currentSection = 'users';
  statusResponse = {success: true, unlocked: true, expires_in: 60};
  const pending = syncAccountBarrierFromServer();
  currentSection = 'products';
  assert.equal(await pending, false);
  assert.equal(accountBarrierUnlocked, true, 'a stale reply must not change state');
  console.log(JSON.stringify({ok: true}));
})().catch(e => {console.error(e); process.exit(1);});
""")


def test_idle_timeout_and_server_sync_lock_the_master_password():
    assert run_timeout()['ok']
