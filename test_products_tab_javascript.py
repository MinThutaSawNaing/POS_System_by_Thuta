"""Regression coverage for the Products management table and page requests."""

from pathlib import Path
from html.parser import HTMLParser
import re
import shutil
import subprocess

import pytest

from test_offline_sync_javascript import _function, _run_node_script


SOURCE = (Path(__file__).parent / 'templates' / 'dashboard.html').read_text(encoding='utf-8')


def run_products_script(script):
    try:
        return _run_node_script(script)
    except subprocess.CalledProcessError as error:
        pytest.fail(error.stdout + error.stderr)


def test_product_table_is_an_accessible_independent_scroll_region():
    class Regions(HTMLParser):
        def __init__(self):
            super().__init__()
            self.divs = []
            self.elements = {}

        def handle_starttag(self, tag, attributes):
            attrs = dict(attributes)
            if attrs.get('id'):
                self.elements[attrs['id']] = (attrs, list(self.divs))
            if tag == 'div':
                self.divs.append(attrs.get('id'))

        def handle_endtag(self, tag):
            if tag == 'div':
                self.divs.pop()

    regions = Regions()
    # Only parse the Products section: unrelated template branches contain
    # mutually exclusive markup that is not a single rendered DOM tree.
    section = SOURCE.split('<!-- Products Section -->', 1)[1].split('<!-- Categories Section -->', 1)[0]
    regions.feed(section)
    attrs, _ = regions.elements['products-table-scroll']
    assert 'products-table-scroll' in attrs['class'].split()
    assert attrs['role'] == 'region'
    assert attrs['aria-label'] == 'Product list'
    assert attrs['tabindex'] == '0'
    assert 'products-table-scroll' in regions.elements['products-table'][1]
    assert 'products-table-scroll' not in regions.elements['products-search'][1]
    assert 'products-table-scroll' not in regions.elements['products-pagination'][1]
    scroll_css = re.search(r'\.products-table-scroll\s*\{([^}]+)\}', SOURCE).group(1)
    assert 'max-height:' in scroll_css
    assert 'overflow: auto;' in scroll_css
    assert 'overscroll-behavior: contain;' in scroll_css
    assert 'scrollbar-gutter: stable;' in scroll_css
    header_css = re.search(r'\.products-table-scroll thead th\s*\{([^}]+)\}', SOURCE).group(1)
    assert 'position: sticky;' in header_css
    assert 'top: 0;' in header_css


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js is required')
def test_products_requests_thirty_rows_and_renders_each_server_page():
    state = re.search(r'const pageState = \{.*?\};', SOURCE, re.S).group()
    sizes = re.search(r'const pageSize = \{.*?\};', SOURCE, re.S).group()
    functions = '\n'.join(_function(SOURCE, name) for name in (
        'parsePagedResponse', 'renderProductsResponse', 'loadProducts',
    ))
    result = run_products_script(r"""
const assert = require('node:assert/strict');
const requests = [], paginations = [];
const table = {rows: [], set innerHTML(value) { this.rows = []; },
  appendChild(row) { this.rows.push(row); }};
const scroll = {scrollTop: 300};
const search = {value: '  Widget  '};
const document = {
  getElementById(id) { return {'products-table': table, 'products-search': search,
    'products-table-scroll': scroll}[id]; },
  createElement() { return {innerHTML: ''}; },
};
function hasManagerOrBossAccess() { return true; }
function hasCapability() { return true; }
function escapeHtml(value) { return String(value); }
function getProductPhotoHtml() { return ''; }
function saveProductsCache() {}
function getProductsCache() { return null; }
function showToast(message) { throw Error(message); }
function renderPagination(key, id, total, reload) { paginations.push({key, id, total}); }
async function fetch(url) {
  const params = new URL(url, 'https://pos.test').searchParams;
  requests.push(Object.fromEntries(params));
  const page = Number(params.get('page'));
  return {ok: true, json: async () => ({
    items: Array.from({length: page === 1 ? 30 : 1}, (_, index) => ({
      id: page === 1 ? index + 1 : 31, name: `Widget ${page}-${index}`,
      price: 10, stock: 5, tax_rate: 0,
    })), total: 31, total_pages: 2, page, per_page: 30,
  })};
}
""" + state + '\n' + sizes + '\n' + functions + r"""
(async () => {
  loadProducts();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(requests[0], {page: '1', per_page: '30', q: 'Widget'});
  assert.equal(table.rows.length, 30);
  assert.equal(scroll.scrollTop, 0);
  assert.equal(paginations[0].total, 2);
  pageState.products = 2;
  scroll.scrollTop = 500;
  loadProducts();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(requests[1], {page: '2', per_page: '30', q: 'Widget'});
  assert.equal(table.rows.length, 1);
  assert.equal(scroll.scrollTop, 0);
  assert.ok(table.rows[0].innerHTML.includes('Widget 2-0'));
  console.log(JSON.stringify({passed: true}));
})().catch(error => { console.error(error); process.exit(1); });
""")
    assert result['passed']