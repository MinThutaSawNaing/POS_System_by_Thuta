"""The Difference Monitor query builder: branch selection, filter and paging."""

from pathlib import Path
import shutil
import subprocess

import pytest

from test_offline_sync_javascript import _function, _run_node_script


SOURCE = (Path(__file__).parent / 'public' / 'product-differences.js').read_text(encoding='utf-8')
DASHBOARD = (Path(__file__).parent / 'templates' / 'dashboard.html').read_text(encoding='utf-8')
IMPORT_MODAL_MARKER = '<!-- Product import writes only after a validated preview and confirmation. -->'


def test_monitor_offers_filters_and_both_downloads():
    modal = DASHBOARD.split('id="productDifferencesModal"', 1)[1].split(IMPORT_MODAL_MARKER, 1)[0]
    assert 'data-difference-filter="all"' in modal
    assert 'data-difference-filter="missing_from_target"' in modal
    assert 'data-difference-filter="needs_review"' in modal
    assert 'id="product-differences-filter-branches"' in modal
    # The filter bar sits inside the results block, before the table it narrows.
    assert (modal.index('id="product-differences-filter-bar"')
            < modal.index('id="product-differences-rows"'))
    assert modal.index('id="product-differences-export-xlsx"') > modal.index('id="product-differences-rows"')
    assert modal.index('id="product-differences-export-pdf"') > modal.index('id="product-differences-rows"')
    assert '/public/product-differences.js?v=' in DASHBOARD


def run_script(script):
    try:
        return _run_node_script(script)
    except subprocess.CalledProcessError as error:
        pytest.fail(error.stdout + error.stderr)


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js is required')
def test_difference_query_carries_page_branches_and_filter():
    helper = _function(SOURCE, 'differenceQuery')
    result = run_script(r"""
const assert = require('node:assert/strict');
const target = {value: '2'};
const checks = [{value: '1', checked: true}, {value: '3', checked: true}, {value: '4', checked: false}];
function selected() { return checks.filter(input => input.checked).map(input => input.value); }
let filter = {mode: 'all', branch: ''};
""" + helper + r"""
const all = differenceQuery();
assert.equal(all.get('page'), '1');
assert.equal(all.get('target_branch'), '2');
assert.equal(all.get('filter'), 'all');
assert.deepEqual(all.getAll('compare_branch'), ['1', '3']);
assert.equal(all.has('filter_branch'), false);

const branch = differenceQuery({page: 4, filter: {mode: 'missing_from_branch', branch: '3'}});
assert.equal(branch.get('page'), '4');
assert.equal(branch.get('filter'), 'missing_from_branch');
assert.equal(branch.get('filter_branch'), '3');
assert.deepEqual(branch.getAll('compare_branch'), ['1', '3']);

const targetView = differenceQuery({filter: {mode: 'missing_from_target', branch: ''}});
assert.equal(targetView.get('filter'), 'missing_from_target');
assert.equal(targetView.has('filter_branch'), false);

const review = differenceQuery({filter: {mode: 'needs_review', branch: ''}});
assert.equal(review.get('filter'), 'needs_review');
console.log(JSON.stringify({passed: true}));
""")


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js is required')
def test_difference_export_reuses_the_visible_filter_without_the_page():
    helpers = '\n'.join(_function(SOURCE, name) for name in ('differenceQuery', 'exportDifferences'))
    result = run_script(r"""
const assert = require('node:assert/strict');
const downloads = [];
const target = {value: '2'};
const checks = [{value: '1', checked: true}, {value: '3', checked: false}];
function selected() { return checks.filter(input => input.checked).map(input => input.value); }
let filter = {mode: 'missing_from_branch', branch: '1'};
function ready() { return Boolean(target.value) && selected().length > 0; }
function requireCapability() { return true; }
const status = {textContent: ''};
function openReportDownload(url) { downloads.push(url); return true; }
""" + helpers + r"""
assert.equal(exportDifferences('xlsx'), true);
const url = new URL(downloads[0], 'https://pos.test');
assert.equal(url.pathname, '/api/products/differences/export');
assert.equal(url.searchParams.get('format'), 'xlsx');
assert.equal(url.searchParams.get('filter'), 'missing_from_branch');
assert.equal(url.searchParams.get('filter_branch'), '1');
assert.equal(url.searchParams.has('page'), false);
assert.deepEqual(url.searchParams.getAll('compare_branch'), ['1']);
exportDifferences('anything');
assert.equal(new URL(downloads[1], 'https://pos.test').searchParams.get('format'), 'pdf');
console.log(JSON.stringify({passed: true}));
""")
    assert result['passed']


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js is required')
def test_difference_filter_bar_marks_the_active_view_and_offers_each_branch():
    helpers = '\n'.join(_function(SOURCE, name) for name in ('filterDescription', 'renderFilterState'))
    result = run_script(r"""
const assert = require('node:assert/strict');
const classes = () => {
  const names = new Set();
  return {add: name => names.add(name), toggle: (name, on) => on ? names.add(name) : names.delete(name),
    contains: name => names.has(name)};
};
const filterButtons = ['all', 'missing_from_target', 'needs_review'].map(mode => ({
  dataset: {differenceFilter: mode}, classList: classes(), attrs: {},
  setAttribute(name, value) { this.attrs[name] = value; }}));
const branchMenu = {items: [], replaceChildren(...items) { this.items = items; }};
const branchFilter = {disabled: null, classList: classes()};
const filterState = {textContent: ''};
const branches = [{id: 1, name: 'Main', code: 'M'}, {id: 2, name: 'Cool Room', code: 'C'}];
const target = {value: '1'};
function selected() { return ['2']; }
function branchName(id) { return branches.find(branch => String(branch.id) === String(id))?.name || 'this branch'; }
let countsByBranch = {};
let filter = {mode: 'missing_from_branch', branch: '2'};
const document = {createElement: () => ({dataset: {}, classList: classes(), textContent: ''})};
""" + helpers + r"""
renderFilterState();
assert.deepEqual(filterButtons.map(button => button.attrs['aria-pressed']), ['false', 'false', 'false']);
assert.equal(branchFilter.classList.contains('active'), true);
assert.equal(branchFilter.disabled, false);
assert.deepEqual(branchMenu.items.map(item => item.textContent),
  ['Show only missing in Main', 'Show only missing in Cool Room']);
assert.deepEqual(branchMenu.items.map(item => item.dataset.branchId), ['1', '2']);
assert.equal(branchMenu.items[1].classList.contains('active'), true);
assert.match(filterState.textContent, /Cool Room/);

countsByBranch = {'1': 0, '2': 7};
renderFilterState();
assert.deepEqual(branchMenu.items.map(item => item.textContent),
  ['Show only missing in Main (0)', 'Show only missing in Cool Room (7)']);

filter = {mode: 'missing_from_target', branch: ''};
renderFilterState();
assert.deepEqual(filterButtons.map(button => button.attrs['aria-pressed']), ['false', 'true', 'false']);
assert.equal(branchFilter.classList.contains('active'), false);
assert.match(filterState.textContent, /Main/);
console.log(JSON.stringify({passed: true}));
""")
    assert result['passed']

    assert result['passed']
