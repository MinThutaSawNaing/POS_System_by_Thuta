"""Execute the real bulk-download helpers with Node and durable-storage mocks."""
from pathlib import Path

from test_offline_sync_javascript import _function, _run_node_script, pytestmark


SOURCE = (Path(__file__).parent / 'templates' / 'dashboard.html').read_text(encoding='utf-8')


def run_download(body):
    helpers = '\n'.join(_function(SOURCE, name) for name in (
        'scopedOfflineCacheKey', 'offlineCacheGet', 'cachedProductsKey',
        'saveCachedProducts', 'getCachedProducts', 'renderOfflineProductButton',
        'fetchOfflineCatalogVersion', 'checkOfflineProductCatalog',
        'loadAllProductsForOffline', 'posResponseJSON',
        'rememberPOSProducts',
    ))
    return _run_node_script('''
const assert = require('node:assert/strict');
const storage = {};
let failStorage = false;
const localStorage = {getItem: k => storage[k] || null,
  setItem(k, value) { if (failStorage) throw new Error('quota'); storage[k] = value; }};
const CACHE_USER_ID = 3, CACHE_USER_ROLE = 'cashier', PRODUCT_CACHE_KEY = 'products';
let currentBranch = {id: 7}, cachedProducts = [], cachedProductsBranchId = null, productsCacheTimestamp = 0;
let offlineProductSync = null, offlineCatalogCheck = null, offlineCatalogVersion = null;
let offline = false, version = 'v1', count = 101, pages = 0, failure = false, mutate = false;
const button = {style: {}, setAttribute() {}}, status = {};
const document = {getElementById: id => id === 'pos-offline-load' ? button : status};
function posCartScopeCurrent(id) { return currentBranch.id === id; }
function posIsOffline() { return offline; }
const requests = [];
async function fetch(url, options) {
  assert.equal(options.cache, 'no-store'); requests.push(url);
  if (failure) throw new Error('network');
  const params = new URL(url, 'https://pos.test').searchParams;
  let payload;
  if (params.get('view') === 'offline-version') payload = {version, count, branch_id: currentBranch.id};
  else {
    pages++;
    const last = !!params.get('cursor');
    payload = {items: Array.from({length: last ? 1 : 100}, (_, i) =>
      ({id: last ? 101 : i + 1, name: 'Product', price: 10, stock: 4})),
      has_more: !last, next_cursor: last ? null : 'next'};
    if (mutate && last) version = 'v2';
  }
  return {ok: true, status: 200, json: async () => payload};
}
''' + helpers + '\n(async () => {\n' + body + "\nconsole.log(JSON.stringify({ok: true}));\n})().catch(e => {console.error(e); process.exit(1);});")


def test_bulk_download_persists_all_pages_disables_and_detects_new_products():
    assert run_download('''
const pending = loadAllProductsForOffline();
assert.equal(button.disabled, true);
assert.match(button.innerHTML, /spinner/);
await loadAllProductsForOffline();
await pending;
assert.equal(pages, 2);
assert.equal(getCachedProducts().length, 101);
assert.equal(button.disabled, true);
assert.equal(button.style.opacity, '0.5');
version = 'v2'; count = 102;
await checkOfflineProductCatalog();
assert.equal(button.disabled, false);
assert.equal(getCachedProducts().length, 101);
currentBranch = {id: 8}; renderOfflineProductButton();
assert.equal(button.disabled, false);
assert.equal(getCachedProducts().length, 0);
''')['ok']


def test_failure_and_quota_preserve_previous_snapshot_and_allow_retry():
    assert run_download('''
saveCachedProducts([{id: 999, price: 1, stock: 2}]);
for (const kind of ['network', 'quota', 'mutation']) {
  failure = kind === 'network'; failStorage = kind === 'quota'; mutate = kind === 'mutation';
  await loadAllProductsForOffline();
  assert.equal(button.disabled, false);
  assert.deepEqual(getCachedProducts().map(p => p.id), [999]);
  assert.match(status.textContent, /not completed/);
}
offline = true; const calls = requests.length;
await loadAllProductsForOffline();
assert.equal(requests.length, calls);
''')['ok']


def test_browsing_never_truncates_complete_catalog_even_after_updates():
    assert run_download('''
await loadAllProductsForOffline();
rememberPOSProducts([{id: 1, price: 20, stock: 4}], 7);
rememberPOSProducts([{id: 2, price: 30, stock: 4}], 7);
assert.equal(getCachedProducts().length, 101);
assert.equal(getCachedProducts().find(p => p.id === 1).price, 20);
renderOfflineProductButton(); assert.equal(button.disabled, false);
failStorage = true;
rememberPOSProducts([{id: 1, price: 99, stock: 1}], 7);
assert.equal(getCachedProducts().find(p => p.id === 1).price, 99);
assert.equal(getCachedProducts().length, 101);
''')['ok']


def test_scope_change_mid_download_cannot_write_into_new_branch():
    assert run_download('''
const originalFetch = fetch;
fetch = async (...args) => {
  const response = await originalFetch(...args);
  if (args[0].includes('view=offline&')) currentBranch = {id: 8};
  return response;
};
await loadAllProductsForOffline();
assert.equal(offlineCacheGet('products_7'), null);
assert.equal(offlineCacheGet('products_8'), null);
assert.equal(button.disabled, false);
''')['ok']