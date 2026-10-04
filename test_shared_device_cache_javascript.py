"""Execute actual cache helpers and service worker in Node."""
import json
import shutil
from pathlib import Path
import pytest
from test_offline_sync_javascript import _function, _run_node_script

ROOT = Path(__file__).parent
pytestmark = pytest.mark.skipif(shutil.which('node') is None, reason='Node required')


def run_dashboard(body):
    source = (ROOT / 'templates/dashboard.html').read_text(encoding='utf-8')
    helpers = '\n'.join(_function(source, name) for name in (
        'scopedOfflineCacheKey', 'offlineCacheSet', 'offlineCacheGet', 'offlineCacheRemove',
        'cachedProductsKey', 'getCachedProducts', 'saveCachedProducts',
        'saveProductsCache', 'getProductsCache', 'getPendingSales', 'setPendingSales',
        'updatePendingSalesBadge', 'syncPendingSales'))
    return _run_node_script('''
let CACHE_USER_ID = 1, CACHE_USER_ROLE = 'manager', currentBranch = {id: 7};
const PRODUCT_CACHE_KEY = 'pos_products_all_cache';
const storage = {}, requests = [];
const localStorage = {getItem: k => storage[k] ?? null,
 setItem: (k,v) => storage[k] = v, removeItem: k => delete storage[k]};
const badge = {style: {}, textContent: ''};
const document = {getElementById: () => badge};
let isSyncingPendingSales = false;
async function fetch(...args) { requests.push(args); throw Error('offline'); }
''' + helpers + '\n(async () => {' + body + '})().catch(e => {console.error(e);process.exit(1)});')


def test_snapshot_user_role_branch_isolation():
    out = run_dashboard('''
storage.pos_settings_cache = JSON.stringify({data: 'legacy'});
storage.pos_products_cache = JSON.stringify({data: 'legacy catalog'});
offlineCacheSet('pos_settings_cache', 'manager');
saveCachedProducts([{id: 99}]); saveProductsCache({items: [{id: 99}]});
CACHE_USER_ROLE = 'cashier';
const cashier = [offlineCacheGet('pos_settings_cache'), getCachedProducts(), getProductsCache()];
offlineCacheRemove('pos_settings_cache');
CACHE_USER_ROLE = 'manager'; CACHE_USER_ID = 2;
const other = offlineCacheGet('pos_settings_cache');
CACHE_USER_ID = 1;
const original = offlineCacheGet('pos_settings_cache').data;
currentBranch = {id: 8};
const branch = [getCachedProducts(), getProductsCache()];
offlineCacheSet(PRODUCT_CACHE_KEY, [{id: 123}]);
const generic = getCachedProducts();
currentBranch = null;
const unknown = [getCachedProducts(), getProductsCache()];
CACHE_USER_ID = null; offlineCacheSet('missing', 1);
console.log(JSON.stringify({cashier, other, original, branch, generic, unknown, storage}));
''')
    assert out['cashier'] == [None, [], None]
    assert out['other'] is None
    assert out['original'] == 'manager'
    assert out['branch'] == out['unknown'] == [[], None]
    assert out['generic'] == []
    assert not any('missing' in key for key in out['storage'])


@pytest.mark.parametrize('legacy', ['[{"transaction_id":"old","customer":"secret"}]', 'broken'])
def test_legacy_queue_retained_warned_never_synced(legacy):
    out = run_dashboard('''
storage.pos_pending_sales = LEGACY;
setPendingSales([{transaction_id: 'owned'}]);
CACHE_USER_ID = 2;
const other = getPendingSales();
await syncPendingSales(); updatePendingSalesBadge();
const warning = badge.textContent;
CACHE_USER_ID = 1; currentBranch = {id: 8};
const branch = getPendingSales(); await syncPendingSales();
currentBranch = {id: 7};
const owned = getPendingSales();
currentBranch = null;
const missingBranch = setPendingSales([]);
console.log(JSON.stringify({other, branch, owned, requests, warning, missingBranch, legacy: storage.pos_pending_sales}));
'''.replace('LEGACY', json.dumps(legacy)))
    assert out['other'] == out['branch'] == out['requests'] == []
    assert out['owned'] == [{'transaction_id': 'owned'}]
    assert out['missingBranch'] is False
    assert out['legacy'] == legacy
    assert 'manager reconciliation' in out['warning']
    assert 'secret' not in out['warning']



def test_worker_network_only_sensitive_paths_and_upgrade_cleanup():
    source = (ROOT / 'public/sw.js').read_text(encoding='utf-8')
    out = _run_node_script('''
const handlers = {}, writes = [], deletes = [], network = [];
let offline = false;
const self = {location: {origin: 'https://pos.test'},
 addEventListener: (type, fn) => handlers[type] = fn,
 clients: {claim: async () => {}}, skipWaiting: async () => {}};
const cache = {match: async () => ({old: true}), put: async (...args) => writes.push(args)};
const caches = {open: async () => cache, keys: async () => ['parrot-pos-1.1.1', 'parrot-pos-1.2.0'],
 delete: async k => deletes.push(k)};
async function fetch(req, opts) {
 network.push([req.url, opts]);
 if (offline) throw Error('offline');
 return {ok: true};
}
''' + source + '''
(async () => {
let activation; handlers.activate({waitUntil: p => activation = p}); await activation;
const failures = [];
for (const path of ['/', '/api/products', '/api/sales', '/api/logs', '/receipt/1', '/reports/sales', '/login']) {
 for (const mode of ['navigate', 'cors']) {
  offline = false;
  let reply; handlers.fetch({request: {url: 'https://pos.test' + path, method: 'GET', mode}, respondWith: p => reply = p});
  await reply;
  offline = true;
  handlers.fetch({request: {url: 'https://pos.test' + path, method: 'GET', mode}, respondWith: p => reply = p});
  try { await reply; failures.push(false); } catch { failures.push(true); }
 }
}
let asset;
handlers.fetch({request: {url: 'https://pos.test/public/vendor/bootstrap/bootstrap.min.css', method: 'GET', mode: 'cors'}, respondWith: p => asset = p});
console.log(JSON.stringify({deletes, writes, failures, network, asset: await asset}));
})().catch(e => {console.error(e); process.exit(1)});
''')
    assert out['deletes'] == ['parrot-pos-1.1.1']
    assert out['writes'] == []
    assert all(out['failures'])
    assert all(options == {'cache': 'no-store'} for _, options in out['network'])
    assert out['asset'] == {'old': True}
