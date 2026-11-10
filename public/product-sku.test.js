// Run with node --test public/product-sku.test.js (no server or database needed).
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../templates/dashboard.html'), 'utf8');

// Match the dashboard function-extraction convention of the Python/Node tests.
function fn(name) {
  const start = source.indexOf(`      function ${name}(`);
  assert.notEqual(start, -1, name);
  const end = source.indexOf('\n      }', start);
  return source.slice(start, end + '\n      }'.length);
}

function harness(sku) {
  const requests = [], messages = [];
  const elements = new Map();
  const context = vm.createContext({
    document: {getElementById(id) {
      if (!elements.has(id)) elements.set(id, {value: id.endsWith('sku') ? sku : '1', files: [], checked: false});
      return elements.get(id);
    }},
    requireCapability: () => true, currentProduct: {stock: 3},
    FormData, console, showToast: message => messages.push(message),
    fetch: (url, options) => {
      requests.push({url, options});
      return new Promise(() => {});
    },
  });
  return {context, requests, messages, elements};
}

test('create and edit submit trimmed optional SKU separately from barcode', () => {
  for (const name of ['saveProduct', 'updateProduct']) {
    for (const [input, expected] of [['  TSH-BLK-M-001  ', 'TSH-BLK-M-001'], ['', ''], ['   ', ''], ['x'.repeat(100), 'x'.repeat(100)]]) {
      const h = harness(input);
      vm.runInContext(`${fn(name)}; ${name}();`, h.context);
      assert.equal(h.requests.length, 1);
      assert.equal(h.requests[0].options.body.get('sku'), expected);
      assert.equal(h.requests[0].options.body.get('barcode'), '1');
    }
  }
});

test('create and edit reject overlength SKU before making requests', () => {
  for (const name of ['saveProduct', 'updateProduct']) {
    const h = harness('x'.repeat(101));
    vm.runInContext(`${fn(name)}; ${name}();`, h.context);
    assert.equal(h.requests.length, 0);
    assert.match(h.messages[0], /100 characters/);
  }
});

test('SKU fields have optional max length, linked labels and faded example', () => {
  for (const id of ['add-sku', 'edit-sku']) {
    const input = source.match(new RegExp(`<input[^>]*id="${id}"[^>]*>`))[0];
    assert.match(input, /maxlength="100"/);
    assert.match(input, /placeholder="TSH-BLK-M-001"/);
    assert.doesNotMatch(input, /required/);
    assert.match(source, new RegExp(`for="${id}"`));
  }
  assert.match(source, /product-sku-input::placeholder\s*\{[^}]*opacity: 0\.6/s);
  assert.match(fn('editProduct'), /getElementById\("edit-sku"\)\.value = product\.sku \|\| ""/);
});

test('management table renders escaped SKU and nullable fallback with aligned cells', () => {
  for (const sku of ['<img src=x onerror=alert(1)>', null, undefined]) {
    const h = harness('');
    const table = {innerHTML: '', rows: [], appendChild(row) { this.rows.push(row); }};
    h.elements.set('products-table', table);
    Object.assign(h.context, {
      parsePagedResponse: payload => payload, productSelection: new Map(),
      hasManagerOrBossAccess: () => false, hasCapability: () => false,
      escapeHtml: value => String(value).replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
      getProductPhotoHtml: () => '', syncProductSelectionUi() {}, renderPagination() {}, loadProducts() {},
    });
    h.context.document.createElement = () => ({dataset: {}});
    h.context.payload = {items: [{id: 1, name: 'Shirt', price: 1, stock: 2, tax_rate: 0, sku}]};
    vm.runInContext(`${fn('renderProductsResponse')}; renderProductsResponse(payload);`, h.context);
    const html = table.rows[0].innerHTML;
    assert.equal((html.match(/<td[ >]/g) || []).length, 11);
    assert.doesNotMatch(html, /<img|>null<|>undefined</);
    assert.ok(html.includes(sku ? '&lt;img' : '—'));
  }
});

test('offline SKU search finds a cached product without requiring a barcode', async () => {
  const h = harness('');
  h.elements.set('product-search', {value: 'tsh-blk'});
  const product = {id: 1, name: 'Shirt', sku: 'TSH-BLK-M-001', barcode: null};
  let matches;
  Object.assign(h.context, {
    currentBranch: {id: 1}, posCategoryFilterId: null,
    console: {error() {}},
    fetch: () => Promise.reject(new Error('offline')),
    posResponseJSON() {}, getCachedProducts: () => [product],
    renderProductsFromCacheForPOS: rows => { matches = rows; },
  });
  vm.runInContext(`${fn('searchProducts')}; searchProducts();`, h.context);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(matches, [product]);
});

test('import mapping and preview keep SKU distinct from barcode', () => {
  const importer = fs.readFileSync(path.join(__dirname, 'product-import.js'), 'utf8');
  assert.match(importer, /barcode: "Barcode", sku: "SKU \(optional\)"/);
  assert.match(importer, /row\.barcode, row\.sku, row\.action/);
  assert.match(source, /<th>Barcode<\/th><th>SKU<\/th><th>Action<\/th>/);
});