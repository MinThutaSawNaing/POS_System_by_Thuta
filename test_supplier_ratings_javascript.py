"""Supplier rating presentation and save workflow regression tests."""
import json
import re

import pytest

from test_returns_dashboard_javascript import DASHBOARD, NODE, _function, _run_node_script

pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is required")


@pytest.mark.parametrize("quality,delivery,expected", [
    (0, 0, None), (4, 0, 4), (0, 3, 3), (4, 3, 3.5), ("4", "5", 4.5),
])
def test_overall_rating_ignores_unrated_categories(quality, delivery, expected):
    helper = _function(DASHBOARD.read_text(encoding="utf-8"), "supplierRatingSummary")
    result = _run_node_script(helper + "\nconsole.log(JSON.stringify(supplierRatingSummary(" +
                              json.dumps({"quality_rating": quality, "delivery_rating": delivery}) + ")));" )
    assert result["average"] == expected
    if expected is None:
        assert result["label"] == "Not rated"
    else:
        assert result["label"] == f"{expected:.1f}/5"


@pytest.mark.parametrize("success", [True, False])
def test_save_ratings_refreshes_only_on_success(success):
    helper = _function(DASHBOARD.read_text(encoding="utf-8"), "saveSupplierRatings")
    script = """
let supplierRatingRequest = 0;
const fields = {
  'rating-supplier-id': {value: '12'}, 'rating-quality': {value: '4'},
  'rating-delivery': {value: '0'}, 'save-supplier-ratings': {disabled: false},
};
let sent, hidden = 0, loaded = 0; const toasts = [];
const document = {getElementById: id => fields[id] || {}};
const bootstrap = {Modal: {getInstance: () => ({hide: () => hidden++})}};
function loadSuppliers() {loaded++;}
function showToast(message, kind) {toasts.push({message, kind});}
async function fetch(url, options) {
  sent = {url, body: JSON.parse(options.body)};
  return {ok: __SUCCESS__, json: async () => ({success: __SUCCESS__, message: 'Rejected'})};
}
__HELPER__
(async () => {await saveSupplierRatings();
console.log(JSON.stringify({sent, hidden, loaded, toasts, disabled: fields['save-supplier-ratings'].disabled}));})();
""".replace("__HELPER__", helper).replace("__SUCCESS__", json.dumps(success))
    result = _run_node_script(script)
    assert result["sent"] == {"url": "/api/suppliers/12/ratings",
                              "body": {"quality_rating": 4, "delivery_rating": 0}}
    assert result["hidden"] == int(success)
    assert result["loaded"] == int(success)
    assert result["disabled"] is False
    assert result["toasts"][0]["kind"] == ("success" if success else "error")


def test_rating_controls_are_connected_in_list_and_details():
    source = DASHBOARD.read_text(encoding="utf-8")
    for element_id in ('supplier-avg-quality', 'supplier-avg-delivery', 'po-total', 'po-monthly-amount'):
        assert len(re.findall(rf'id="{element_id}"', source)) == 1
    assert '>Supplier Rating</th>' in source
    for handler in ('loadSuppliers', 'viewSupplierDetails'):
        body = _function(source, handler)
        assert 'supplierRatingSummary(supplier)' in body
        assert 'showSupplierRatingsModal(${supplier.id})' in body
    assert 'id="supplierRatingsModal"' in source
    assert 'onsubmit="event.preventDefault(); saveSupplierRatings();"' in source


def test_open_rating_dialog_populates_scores_and_uses_safe_handoff():
    helper = _function(DASHBOARD.read_text(encoding="utf-8"), 'showSupplierRatingsModal')
    result = _run_node_script("""
let supplierRatingRequest = 0;
const fields = {}; const opened = [];
const document = {getElementById: id => fields[id] ||= {}};
function showModalAfterHiding(id) {opened.push(id);}
function showToast() {throw new Error('unexpected error');}
async function fetch() {return {ok: true, json: async () => ({
  id: 12, name: 'Supplier <name>', quality_rating: 4.5, delivery_rating: 0,
})};}
__HELPER__
(async () => {await showSupplierRatingsModal(12);
console.log(JSON.stringify({fields, opened}));})();
""".replace('__HELPER__', helper))
    assert result['opened'] == ['supplierRatingsModal']
    assert result['fields']['rating-quality']['value'] == 4.5
    assert result['fields']['rating-delivery']['value'] == 0
    assert result['fields']['rating-supplier-name']['textContent'] == 'Supplier <name>'


def test_stale_supplier_fetch_cannot_overwrite_latest_selection():
    helper = _function(DASHBOARD.read_text(encoding="utf-8"), 'showSupplierRatingsModal')
    result = _run_node_script("""
let supplierRatingRequest = 0;
const fields = {}, pending = {}, opened = [];
const document = {getElementById: id => fields[id] ||= {}};
function showModalAfterHiding(id) {opened.push(id);}
function showToast() {}
function fetch(url) {return new Promise(resolve => pending[url] = resolve);}
__HELPER__
(async () => {
 const a = showSupplierRatingsModal(1), b = showSupplierRatingsModal(2);
 pending['/api/suppliers/2']({ok:true, json:async()=>({id:2,name:'B'})}); await b;
 pending['/api/suppliers/1']({ok:true, json:async()=>({id:1,name:'A'})}); await a;
 console.log(JSON.stringify({fields, opened}));
})();
""".replace('__HELPER__', helper))
    assert result['fields']['rating-supplier-id']['value'] == 2
    assert result['opened'] == ['supplierRatingsModal']