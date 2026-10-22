"""Isolated Node-only clothing UI contracts; never imports app or opens a DB."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="Node.js is required")


def run_node(body):
    script = "const clothing = require(" + json.dumps(str(ROOT / "public" / "clothing.js")) + ");\n" + body
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


DOM = r"""
const assert = require('node:assert/strict');
class Element {
  constructor(tag) { this.tag = tag; this.nodeType = 1; this.children = []; this.events = {}; this.value = ''; this.open = false; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this.value = ''; }
  setAttribute(key, value) { this[key] = value; }
  addEventListener(name, callback) { this.events[name] = callback; }
  closest() { return this.details || this; }
  async fire(name) { if (this.events[name]) return this.events[name]({preventDefault() {}}); }
}
const pos = new Element('div'), admin = new Element('div');
pos.details = new Element('details'); admin.details = new Element('details');
const doc = {createElement: tag => new Element(tag), createTextNode: text => ({textContent: text}),
  querySelector: selector => selector === '[data-clothing-picker]' ? pos : admin};
const flatten = node => [node, ...node.children.flatMap(child => child.children ? flatten(child) : [child])];
const find = (host, text) => flatten(host).find(node => node.textContent === text);
const selects = host => flatten(host).filter(node => node.tag === 'select');
const calls = [], added = [];
let context = {branch: 1, user: 5}, next = [], keyCount = 0, changed = 0;
const env = {document: doc, context: () => context, canWrite: () => true,
  fetch: async (url, init) => { calls.push({url, init, body: init.body ? JSON.parse(init.body) : null});
    const reply = next.shift(); if (reply instanceof Error) throw reply;
    if (typeof reply === 'function') return reply();
    return {ok: true, json: async () => reply}; },
  addProduct: p => added.push(p), changed: () => changed++, receive: () => {}, key: () => 'key-' + (++keyCount)};
const ui = clothing.createUI(env); ui.mount();
async function loadPos(variants) {
  next.push({success: true, styles: [{id: 9, name: '<img src=x onerror=1>', code: 'A'}]});
  pos.details.open = true; await pos.details.fire('toggle');
  selects(pos)[0].value = '9'; next.push({success: true, variants});
  await selects(pos)[0].fire('change');
}
const variants = [
  {id: 101, product_id: 101, variant_id: 33, style_id: 9, name: 'Tee', size: 'M', color: 'Red', stock: 2, price: '10.00'},
  {id: 102, product_id: 102, variant_id: 34, style_id: 9, name: 'Tee', size: 'M', color: 'Blue', stock: 0, price: '10.00'},
  {id: 103, product_id: 103, variant_id: 35, style_id: 9, name: 'Tee', size: 'L', color: 'Blue', stock: 5, archived: true, price: '10.00'}];
"""


def test_display_names_and_exact_ids_do_not_mutate_product_names():
    run_node(r"""
const assert = require('node:assert/strict');
const ordinary = {id: 4, name: 'Ordinary', size: 'XL'};
assert.equal(clothing.displayName(ordinary), 'Ordinary');
const variant = {id: 100, variant_id: 8, style_id: 2, name: 'Tee', size: 'M', color: 'Red', stock: 3};
assert.equal(clothing.displayName(variant), 'Tee — Red / M');
assert.equal(variant.name, 'Tee');
assert.equal(clothing.displayName({...variant, display_name: 'Saved — M / Red'}), 'Saved — M / Red');
assert.equal(clothing.exactProduct([variant], 8, 2), undefined);
assert.equal(clothing.exactProduct([variant], 100, 2), variant);
assert.equal(clothing.exactProduct([variant], 100, 3), undefined);
assert.equal(clothing.exactProduct([{...variant, archived: true}], 100, 2), undefined);
assert.equal(clothing.exactProduct([{...variant, stock: 0}], 100, 2), undefined);
""")


def test_picker_selects_exact_product_filters_and_disables_unavailable_stock():
    run_node(DOM + r"""
(async () => {
  await loadPos(variants);
  assert.equal(selects(pos)[0].children[1].textContent, '<img src=x onerror=1> · A');
  let adds = flatten(pos).filter(n => n.textContent === 'Add 1');
  assert.equal(adds.length, 3); assert.equal(adds[0].disabled, false);
  assert.equal(adds[1].disabled, true); assert.equal(adds[2].disabled, true);
  await adds[0].fire('click'); assert.deepEqual(added.map(p => p.id), [101]);
  selects(pos)[2].value = 'Red'; await selects(pos)[2].fire('change');
  assert.equal(flatten(pos).filter(n => n.textContent === 'Add 1').length, 1);
  assert.ok(calls.every(c => c.init.cache === 'no-store' && c.init.headers['x-pos-branch-id'] === '1'));
})().catch(e => {console.error(e); process.exitCode = 1;});
""")


def test_branch_change_and_cache_invalidation_reject_old_variant_buttons():
    run_node(DOM + r"""
(async () => {
  await loadPos(variants);
  const old = find(pos, 'Add 1'); context = {branch: 2, user: 5};
  await old.fire('click'); assert.equal(added.length, 0);
  await loadPos(variants); const beforeInvalidation = find(pos, 'Add 1');
  ui.invalidate(); await beforeInvalidation.fire('click'); assert.equal(added.length, 0);
})().catch(e => {console.error(e); process.exitCode = 1;});
""")


def test_late_branch_variant_response_is_never_rendered():
    run_node(DOM + r"""
(async () => {
  next.push({styles: [{id: 9, name: 'Tee'}]}); await find(pos, 'Reload clothing styles / stock').fire('click');
  selects(pos)[0].value = '9'; let resolve;
  next.push(() => new Promise(r => {resolve = r;}));
  const pending = selects(pos)[0].fire('change'); context = {branch: 2, user: 5};
  resolve({ok: true, json: async () => ({variants})}); await pending;
  assert.equal(find(pos, 'Add 1'), undefined);
})().catch(e => {console.error(e); process.exitCode = 1;});
""")


def test_csv_commit_uses_normalized_preview_token_and_same_retry_key():
    run_node(DOM + r"""
(async () => {
  const csv = flatten(admin).find(n => n.tag === 'textarea'); csv.value = 'untrusted original';
  const normalized = [{style_id: 9, size: 'M', color: '<script>', barcode: 'sku1', price: '10.00'}];
  next.push({valid: true, rows: normalized, preview_token: 'signed'});
  await find(admin, 'Preview CSV (no changes)').fire('click');
  const commit = find(admin, 'Commit reviewed preview'); assert.equal(commit.disabled, false);
  next.push(new Error('Connection lost')); await commit.fire('click'); assert.equal(commit.disabled, false);
  next.push({success: true}, {styles: []}); await commit.fire('click');
  const writes = calls.filter(c => c.url.endsWith('/commit'));
  assert.equal(writes.length, 2); assert.deepEqual(writes[0].body, writes[1].body);
  assert.deepEqual(writes[0].body.rows, normalized); assert.equal(writes[0].body.preview_token, 'signed');
  assert.equal(writes[0].body.idempotency_key, 'key-1'); assert.equal(changed, 1);
  assert.equal(commit.disabled, true);
})().catch(e => {console.error(e); process.exitCode = 1;});
""")


def test_csv_edits_invalidate_preview_and_late_preview_response():
    run_node(DOM + r"""
(async () => {
  const csv = flatten(admin).find(n => n.tag === 'textarea');
  let resolve; next.push(() => new Promise(r => {resolve = r;}));
  const pending = find(admin, 'Preview CSV (no changes)').fire('click');
  csv.value = 'edited'; await csv.fire('input');
  resolve({ok: true, json: async () => ({valid: true, rows: [], preview_token: 'old'})});
  await pending;
  assert.equal(find(admin, 'Commit reviewed preview').disabled, true);
  await find(admin, 'Commit reviewed preview').fire('click');
  assert.equal(calls.filter(c => c.url.endsWith('/commit')).length, 0);
})().catch(e => {console.error(e); process.exitCode = 1;});
""")


def test_template_additions_preserve_external_script_and_safe_cart_display():
    source = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
    assert '<script src="/public/clothing.js"></script>' in source
    assert "name: product.display_name || product.name" in source
    assert '${cartName}</h6>' in source
    assert 'return-item-disposition' in source
    assert 'idempotency_key: receivingKey' in source
    # Admin clothing controls live in Settings, not in the Products catalog tab.
    settings = source.index('id="settings-section"')
    clothing_group = source.index('id="settings-group-clothing"')
    admin_controls = source.index('data-clothing-admin')
    assert settings < clothing_group < admin_controls
    assert 'data-group="clothing"' in source
    assert 'window.ClothingUI.load(true)' in source
    assert 'data-clothing-admin' not in source[:settings]
    assert "Product.name" not in (ROOT / "public" / "clothing.js").read_text(encoding="utf-8")