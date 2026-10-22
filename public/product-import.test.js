const test = require('node:test');
const assert = require('node:assert/strict');
const {createImporter} = require('./product-import.js');

function harness() {
  const h = {file: {name: 'products.xlsx'}, scope: 'manager:branch-1',
    mapping: {}, mode: 'skip', createCategories: false, requests: [], renders: [], confirmations: [], saved: 0};
  h.importer = createImporter({
    file: () => h.file, scope: () => h.scope, mapping: () => h.mapping,
    duplicateMode: () => h.mode,
    createCategories: () => h.createCategories,
    confirm: text => { h.confirmations.push(text); return h.confirmed !== false; },
    render: state => h.renders.push(state), committed: () => { h.saved++; },
    request: async (path, payload) => {
      h.requests.push({path, payload});
      if (h.reply) return h.reply(path, payload);
      return path.endsWith('/preview') ? {success: true, preview_token: 'signed-preview',
        columns: ['Product Name', 'Price'], mapping: {name: 'Product Name', price: 'Price'},
        preview: {rows: [{row: 2, name: 'Coffee', action: 'create'}],
          summary: {create: 1, update: 0, skip: 0, error: 0}, warnings: []}}
        : {success: true, summary: {create: 1, update: 0, skip: 0, error: 0}};
    },
  });
  return h;
}

test('a valid preview never saves until explicit confirmation', async () => {
  const h = harness();
  await h.importer.preview();
  assert.equal(h.requests.length, 1);
  assert.equal(h.saved, 0);
  assert.equal(h.importer.state().canCommit, true);
  h.confirmed = false;
  await h.importer.commit();
  assert.equal(h.requests.length, 1);
  h.confirmed = true;
  await h.importer.commit();
  assert.equal(h.requests[1].path, '/api/products/import/commit');
  assert.equal(h.requests[1].payload.preview_token, 'signed-preview');
  assert.match(h.confirmations[1], /1 new/);
  assert.equal(h.saved, 1);
  assert.equal(h.importer.state().canCommit, false);
});

test('changing file, mapping, duplicate mode or branch invalidates confirmation', async () => {
  for (const change of [h => { h.file = {name: 'other.xlsx'}; },
    h => { h.mapping = {price: 'Cost'}; }, h => { h.mode = 'update'; },
    h => { h.scope = 'manager:branch-2'; }, h => { h.createCategories = true; }]) {
    const h = harness(); await h.importer.preview(); change(h);
    await h.importer.commit();
    assert.equal(h.requests.length, 1);
    assert.equal(h.importer.state().canCommit, false);
  }
});

test('invalid rows and all-skipped files cannot be confirmed', async () => {
  for (const summary of [{create: 1, update: 0, skip: 0, error: 1},
    {create: 0, update: 0, skip: 2, error: 0}]) {
    const h = harness(); h.reply = async () => ({success: true, preview_token: 'token',
      preview: {rows: [], summary, warnings: []}});
    await h.importer.preview(); await h.importer.commit();
    assert.equal(h.importer.state().canCommit, false);
    assert.equal(h.requests.length, 1);
  }
});

test('late preview responses cannot revive discarded or different-branch imports', async () => {
  const h = harness(); let resolve;
  h.reply = () => new Promise(done => { resolve = done; });
  const pending = h.importer.preview();
  assert.equal(h.importer.state().busy, true);
  h.importer.invalidate();
  resolve({success: true, preview_token: 'old', preview: {
    rows: [], summary: {create: 1, update: 0, skip: 0, error: 0}}});
  await pending;
  assert.equal(h.importer.state().preview, null);
  assert.equal(h.importer.state().busy, false);
  assert.equal(h.importer.state().canCommit, false);
});

test('double clicks issue one commit and a failure requires a fresh preview', async () => {
  const h = harness(); await h.importer.preview(); let reject;
  h.reply = () => new Promise((resolve, fail) => { reject = fail; });
  const saving = h.importer.commit(); await h.importer.commit();
  assert.equal(h.requests.length, 2);
  reject(new Error('The preview is stale.'));
  await saving;
  assert.match(h.importer.state().error, /Preview again/);
  assert.equal(h.importer.state().canCommit, false);
  assert.equal(h.saved, 0);
});

test('successful completion is no longer busy when the modal dismisses', async () => {
  let importer, completionBusy;
  const h = harness();
  importer = createImporter({file: () => h.file, scope: () => h.scope,
    mapping: () => ({}), duplicateMode: () => 'skip', confirm: () => true,
    render() {}, committed() { completionBusy = importer.state().busy; },
    request: async path => path.endsWith('/preview') ? {success: true,
      preview_token: 'token', preview: {rows: [], summary: {create: 1, update: 0, skip: 0, error: 0}}}
      : {success: true, summary: {create: 1, update: 0, skip: 0, error: 0}},
  });
  await importer.preview(); await importer.commit();
  assert.equal(completionBusy, false);
});