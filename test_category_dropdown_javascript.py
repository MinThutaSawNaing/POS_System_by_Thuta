"""Category dropdowns are searchable, and duplicate leaf names ask first."""

import re
import shutil
from pathlib import Path

import pytest

from test_offline_sync_javascript import _function, _run_node_script


SOURCE = (Path(__file__).parent / 'templates' / 'dashboard.html').read_text(encoding='utf-8')

CATEGORY_SELECT_IDS = (
    'supplier-category-filter',
    'logs-category-filter',
    'add-category',
    'edit-product-category',
    'add-category-parent',
    'edit-category-parent',
    'add-supplier-category',
    'edit-supplier-category',
)


def test_every_category_dropdown_uses_the_searchable_select():
    for select_id in CATEGORY_SELECT_IDS:
        match = re.search(rf'<select[^>]*id="{re.escape(select_id)}"[^>]*>', SOURCE)
        assert match, f'{select_id} select is missing'
        tag = match.group(0)
        class_attr = re.search(r'class="([^"]*)"', tag)
        assert class_attr, f'{select_id} has no class attribute'
        assert 'searchable-select' in class_attr.group(1).split(), f'{select_id} is not searchable'
        assert 'data-search-placeholder=' in tag, f'{select_id} has no search hint'
    # The wrapper is built once on page load for every searchable select.
    assert 'initSearchableSelects();' in SOURCE


def test_category_search_supports_filtering_and_keyboard_picks():
    # Typing narrows the list, and the highlighted row stays visible.
    assert 'searchable-select-input' in SOURCE
    assert 'button.searchable-select-option' in SOURCE
    assert 'searchable-select-list .list-group-item.active' in SOURCE
    # Arrow keys walk the filtered list and Enter takes the highlighted option,
    # which matters for long shelves such as Electronics > IOS > ... > 256GB.
    assert 'e.key === "ArrowDown"' in SOURCE
    assert 'e.key === "ArrowUp"' in SOURCE
    assert 'visibleOptions[activeIndex >= 0 ? activeIndex : 0]' in SOURCE


def test_long_category_paths_are_not_clipped_in_the_dropdown():
    # A deep shelf such as Electronics/IOS/Iphones/18Series/Purple/256GB used to
    # be cut down to "Electronic/IOS/Iphones/18" by nowrap + ellipsis on every row.
    option_rule = re.search(
        r"\.searchable-select-list \.list-group-item\s*\{([^}]*)\}", SOURCE
    )
    assert option_rule is not None, "searchable select option CSS rule is missing"
    body = option_rule.group(1)
    assert "white-space: normal" in body
    assert "overflow-wrap: anywhere" in body
    assert "text-overflow" not in body
    assert "white-space: nowrap" not in body
    # The popup may grow past the input so a deep path still fits on one line.
    list_rule = re.search(r"\.searchable-select-list\s*\{([^}]*)\}", SOURCE)
    assert list_rule is not None, "searchable select list CSS rule is missing"
    assert "width: max-content" in list_rule.group(1)
    assert "min-width: 100%" in list_rule.group(1)
    # The input itself clips too, so the picked path is also a hover tooltip.
    assert "input.title = input.value;" in SOURCE


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js is required')
def test_duplicate_subcategory_is_confirmed_before_the_retry_saves_it():
    script = r"""
const assert = require('node:assert/strict');
const requests = [];
const toasts = [];
const dialogs = [];
let modalHidden = 0;
const saveButton = {disabled: false, innerHTML: 'Save'};
const form = {reset() {}};
const colorInput = {value: '#6c757d'};
const colorPreview = {textContent: ''};
const document = {
  querySelector(selector) {
    return selector === '#addCategoryModal .btn-primary' ? saveButton : null;
  },
  getElementById(id) {
    return {'add-category-form': form, 'add-category-color': colorInput,
      'add-category-color-preview': colorPreview, 'addCategoryModal': {}}[id];
  },
};
const bootstrap = {Modal: {getInstance() { return {hide() { modalHidden += 1; }}; }}};
function showToast(message, kind) { toasts.push({message, kind}); }
function showConfirmDialog(title, message, onConfirm) { dialogs.push({title, message, onConfirm}); }
function markBranchFormSaved() {}
function loadCategories() {}
function loadCategoryDropdowns() {}
let cachedCategories = [];
async function fetch(url, options) {
  const body = JSON.parse(options.body);
  requests.push({url, body});
  if (!body.confirm_duplicate) {
    return {json: async () => ({success: false, duplicate: true,
      message: 'Subcategory "256GB" already exists under (Electronics / IOS / Iphone / Iphone18series). Are you willing to save it?'})};
  }
  return {json: async () => ({success: true, category: {id: 9}})};
}
""" + _function(SOURCE, 'postCategoryForm') + r"""
(async () => {
  await postCategoryForm({name: '256GB', parent_id: 5}, false);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/api/categories');
  assert.equal(requests[0].body.confirm_duplicate, false);
  assert.equal(dialogs.length, 1);
  assert.equal(dialogs[0].title, 'Duplicate subcategory');
  assert.ok(dialogs[0].message.includes('Are you willing to save it?'));
  // The first attempt must not touch the table until the manager answers.
  assert.equal(toasts.length, 0);
  assert.equal(saveButton.disabled, false);
  await dialogs[0].onConfirm();
  assert.equal(requests.length, 2);
  assert.equal(requests[1].body.confirm_duplicate, true);
  assert.equal(toasts[toasts.length - 1].kind, 'success');
  assert.equal(modalHidden, 1);
  console.log(JSON.stringify({passed: true, requests: requests.length}));
})().catch(error => { console.error(error); process.exit(1); });
"""
    result = _run_node_script(script)
    assert result['passed'] is True
    assert result['requests'] == 2
