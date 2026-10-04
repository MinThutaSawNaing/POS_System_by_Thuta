"""Stale rendered tabs must not act as the account in a replacement cookie."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import pytest

from test_cashier_permissions import backend, world, login, checkout, state, CHILD
from test_section_cache_javascript import _cache_source, PROLOGUE
from test_offline_sync_javascript import _function, _run_node_script


@pytest.mark.skipif(CHILD, reason='Parent launcher only')
def test_isolated_identity_backend():
    with tempfile.TemporaryDirectory(prefix='pos-stale-identity-') as directory:
        env = dict(os.environ, POS_PERMISSION_CHILD='1', POS_PERMISSION_INSTANCE=directory)
        result = subprocess.run([sys.executable, '-m', 'pytest', str(Path(__file__).resolve()),
                                 '-q', '--tb=short', '-k', 'backend'], env=env,
                                capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('role', ['manager', 'cashier', 'boss'])
def test_backend_old_identity_denied_without_mutation(world, role):
    w = world
    old_user = w['users'][1].id
    login(w, role=role)
    before = state(w)
    headers = {'X-POS-User-ID': str(old_user), 'X-POS-Role': 'cashier'}
    for path, method in [('/api/sales', 'POST'), ('/api/products', 'POST'),
                         ('/api/agent/chat', 'POST'), ('/api/products', 'GET')]:
        response = w['client'].open(path, method=method, headers=headers, json=checkout(w))
        assert response.status_code == 409
        assert response.json['code'] == 'stale_session'
        assert response.headers['X-POS-Session-Mismatch'] == '1'
        assert response.headers['Cache-Control'] == 'no-store'
        assert state(w) == before


def test_backend_role_missing_session_and_compatibility(world):
    w = world
    user = w['users'][0].id
    for headers in [ {'X-POS-User-ID': str(user), 'X-POS-Role': 'manager'},
                     {'X-POS-User-ID': str(user)}, {'X-POS-Role': 'cashier'} ]:
        assert w['client'].get('/api/products', headers=headers).status_code == 409
    matching = {'X-POS-User-ID': str(user), 'X-POS-Role': 'cashier'}
    assert w['client'].get('/api/products', headers=matching).status_code == 200
    assert w['client'].get('/api/products').status_code == 200
    with w['client'].session_transaction() as session:
        session.clear()
    assert w['client'].post('/api/sales', headers=matching, json=checkout(w)).status_code == 409


@pytest.mark.skipif(shutil.which('node') is None, reason='Node required')
def test_fetch_identity_headers_and_stale_queue():
    source = (Path(__file__).parent / 'templates/dashboard.html').read_text(encoding='utf-8')
    helpers = _function(source, 'syncPendingSales') + '\n' + '\n'.join(
        _function(source, name) for name in ('queueOfflineSale', 'completeSale'))
    out = _run_node_script(PROLOGUE + '''
const CACHE_USER_ID = 1, CACHE_USER_ROLE = 'manager';
let overlay;
const child = {inert: false};
const document = {body: {children: [child], appendChild: el => overlay = el},
 getElementById: id => id === 'stale-session-overlay' ? overlay : null,
 createElement: () => ({setAttribute() {}, style: {}, focus() {}})};
let calls = [], mismatch = false;
network = async (input, init) => {
 calls.push({url: typeof input === 'string' ? input : input.url,
  headers: Object.fromEntries(new Headers(init && init.headers))});
 return new Response(JSON.stringify({success: !mismatch}), {status: mismatch ? 409 : 200,
  headers: {'content-type': 'application/json', ...(mismatch ? {'x-pos-session-mismatch': '1'} : {})}});
};
''' + _cache_source() + '\n' + helpers + '''
globalThis.fetch = window.fetch;
(async () => {
 await window.fetch('/api/agent/chat', {method:'POST', headers:{'content-type':'application/json'}});
 const req = new Request('http://localhost:5000/api/agent/chat', {
  method: 'POST', headers: {'x-request':'kept', 'x-pos-user-id':'spoof'}, body:'{}'});
 await window.fetch(req, {headers: {'x-init':'kept', 'x-pos-role':'spoof'}});
 await window.fetch('https://external.test/api/chat', {headers:{'x-only':'yes'}});
 await window.fetch('/api/products');
 cacheStore.clear();
 let isSyncingPendingSales = false;
 // The real sync function closes over these same script-level bindings below.
 mismatch = true;
 await syncPendingSales();
 const callsAtBlock = calls.length;
 const response = await window.fetch('/api/products');
 await syncPendingSales();
 const queued = queueOfflineSale({}, null);
 completeSale();
 console.log(JSON.stringify({calls, callsAtBlock, stale: dashboardSessionStale,
  status: response.status, pending, writes, queued, overlay: overlay.innerHTML, inert: child.inert}));
})().catch(e => {console.error(e);process.exit(1)});
let isSyncingPendingSales = false;
const pending = [{transaction_id:'a-sale', saleData:{transaction_id:'a-sale',payment_method:'cash'}}];
let writes = 0;
function getPendingSales() { return pending; }
function setPendingSales() { writes++; return true; }
function setConnectionStatus() {}
function updatePendingSalesBadge() {}
function showToast() {}
''')
    assert out['calls'][0]['headers'] == {
        'content-type': 'application/json', 'x-pos-user-id': '1', 'x-pos-role': 'manager'}
    assert out['calls'][1]['headers'] == {
        'content-type': 'text/plain;charset=UTF-8', 'x-request': 'kept',
        'x-init': 'kept', 'x-pos-user-id': '1', 'x-pos-role': 'manager'}
    assert out['calls'][2]['headers'] == {'x-only': 'yes'}
    assert out['calls'][3]['headers']['x-pos-user-id'] == '1'
    assert out['stale'] and out['inert']
    assert out['status'] == 409
    assert len(out['calls']) == out['callsAtBlock'] == 5
    assert out['writes'] == 0 and out['queued'] is False
    assert len(out['pending']) == 1
    assert 'Sign-in changed' in out['overlay']
