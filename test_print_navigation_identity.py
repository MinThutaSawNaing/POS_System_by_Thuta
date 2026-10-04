"""Navigation identity is a consistency check, not a permission source."""
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile

import pytest
from test_cashier_permissions import backend, world, login, CHILD
from test_offline_sync_javascript import _function, _run_node_script


@pytest.mark.skipif(CHILD, reason='Parent launcher only')
def test_isolated_navigation_backend():
    with tempfile.TemporaryDirectory(prefix='pos-navigation-') as directory:
        env = dict(os.environ, POS_PERMISSION_CHILD='1', POS_PERMISSION_INSTANCE=directory)
        result = subprocess.run([sys.executable, '-m', 'pytest', str(Path(__file__).resolve()),
                                 '-q', '--tb=short', '-k', 'backend'], env=env,
                                capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, result.stdout + result.stderr


def test_backend_navigation_identity_and_no_escalation(world):
    w = world
    client = w['client']
    user = w['users'][0].id
    matching = {'pos_user_id': str(user), 'pos_role': 'cashier'}
    paths = ['/delivery-report', '/api/sales/permission-sale-0/print',
             f"/api/deliveries/{w['deliveries'][0].id}/print",
             '/api/returns_exchanges/permission-workflow-0/print', '/api/deliveries/report']
    for path in paths:
        for query in [dict(matching, pos_user_id=str(w['users'][1].id)),
                      dict(matching, pos_role='manager'), {'pos_user_id': str(user)},
                      {'pos_role': 'cashier'}]:
            response = client.get(path, query_string=query)
            assert response.status_code == 409, (path, response.status_code)
            assert response.json['code'] == 'stale_session'
            assert response.headers['X-POS-Session-Mismatch'] == '1'
            assert 'no-store' in response.headers['Cache-Control']
        response = client.get(path, query_string=matching)
        assert response.status_code == 200, (path, response.status_code)
        assert 'no-store' in response.headers['Cache-Control']
    for path in ['/api/deliveries/export', '/api/debts/1/print', '/api/warehouse/export']:
        assert client.get(path, query_string=matching).status_code == 403
        assert client.get(path, query_string=dict(matching, pos_role='manager')).status_code == 409
    headers = {'X-POS-User-ID': str(user), 'X-POS-Role': 'cashier'}
    assert client.get('/delivery-report', headers=headers,
                      query_string=dict(matching, pos_role='boss')).status_code == 409
    assert client.get('/api/deliveries/report', query_string=matching,
                      headers=dict(headers, **{'X-POS-Role': 'boss'})).status_code == 409
    assert 'no-store' in client.get('/').headers['Cache-Control']
    with client.session_transaction() as session:
        session.clear()
    assert client.get('/delivery-report', query_string=matching).status_code == 409


def test_backend_rendered_report_uses_trusted_session(world):
    w = world
    login(w, role='manager')
    user = w['users'][2].id
    response = w['client'].get('/delivery-report', query_string={
        'pos_user_id': str(user), 'pos_role': 'manager'})
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert f'const REPORT_USER_ID = {user};' in html
    assert 'const REPORT_USER_ROLE = "manager";' in html
    assert 'headers: identityHeaders' in html
    assert '"X-POS-User-ID": String(REPORT_USER_ID)' in html
    assert '"X-POS-Role": String(REPORT_USER_ROLE)' in html
    assert 'params.set("pos_user_id", String(REPORT_USER_ID))' in html
    assert 'params.set("pos_role", String(REPORT_USER_ROLE))' in html


@pytest.mark.skipif(shutil.which('node') is None, reason='Node required')
def test_dashboard_navigation_real_helpers():
    source = (Path(__file__).parent / 'templates/dashboard.html').read_text(encoding='utf-8')
    names = ['dashboardIdentityUrl', 'loadUrlInPwaPrintFrame', 'openReportDownload',
             'openReceiptWindow', 'openReturnExchangeReceipt', 'printDeliverySlip',
             'openDeliveryReportWindow', 'printDebtReceipt', 'printPurchaseOrder',
             'printPurchaseOrderDirect']
    helpers = '\n'.join(_function(source, name) for name in names)
    out = _run_node_script('''
const CACHE_USER_ID = 7, CACHE_USER_ROLE = 'cashier';
let dashboardSessionStale = false, standalone = false;
let calls = [];
const window = {location: {href:'https://pos.test/', origin:'https://pos.test'},
 open: (url) => { calls.push(url); return {location:{replace: url => calls.push(url)}, opener:null}; }};
let frameLoad, removed = false;
const frame = {addEventListener: (event, handler) => frameLoad = handler,
 contentDocument: {body: {textContent: '{}'}}, remove: () => removed = true,
 set src(url) {calls.push(url)}};
function createPwaPrintFrame() { return frame; }
function blockStaleDashboard() {dashboardSessionStale = true;}
function isStandalonePWA() {return standalone;}
function showToast() {}
let currentDebtId = 1, currentPOId = 2;
''' + helpers + '''
const external = dashboardIdentityUrl('https://outside.test/api/export');
const override = dashboardIdentityUrl('/api/export?pos_user_id=999&pos_role=boss&format=xlsx#top');
openReceiptWindow('sale', true);
openReturnExchangeReceipt('return');
printDeliverySlip(3);
openDeliveryReportWindow();
openReportDownload('/api/warehouse/export?format=xlsx');
printDebtReceipt(); printPurchaseOrder(); printPurchaseOrderDirect(4);
standalone = true;
openReceiptWindow('sale', true); openReturnExchangeReceipt('return'); printDeliverySlip(3);
const count = calls.length;
frame.contentDocument.body.textContent = '{"code":"stale_session"}';
frameLoad();
openReceiptWindow('sale'); openReturnExchangeReceipt('return'); printDeliverySlip(3);
openDeliveryReportWindow(); openReportDownload('/api/warehouse/export');
loadUrlInPwaPrintFrame('/api/sales/sale/print');
console.log(JSON.stringify({external, override, calls, count, removed, stale: dashboardSessionStale}));
''')
    assert out['removed'] and out['stale']
    assert out['external'] is None
    assert out['override'] == '/api/export?pos_user_id=7&pos_role=cashier&format=xlsx#top'
    assert len(out['calls']) == out['count']
    navigations = [url for url in out['calls'] if url != 'about:blank']
    assert len(navigations) == 11
    assert all('pos_user_id=7' in url and 'pos_role=cashier' in url for url in navigations)


@pytest.mark.skipif(shutil.which('node') is None, reason='Node required')
def test_standalone_report_refresh_export_and_block():
    source = (Path(__file__).parent / 'templates/delivery_report.html').read_text(encoding='utf-8')
    helpers = '\n'.join(_function(source, name) for name in
                        ['loadReport', 'exportReport', 'blockStaleReport'])
    out = _run_node_script('''
const REPORT_USER_ID = 7, REPORT_USER_ROLE = 'manager';
const identityHeaders = {'X-POS-User-ID': String(REPORT_USER_ID), 'X-POS-Role': String(REPORT_USER_ROLE)};
let sessionStale = false, mismatch = false, calls = [], opened = [], renders = 0;
const root = {innerHTML: '', insertAdjacentHTML() {}};
const buttons = {};
const document = {getElementById: id => buttons[id] ||= {disabled: false}};
const window = {open: url => {opened.push(url);return {};}};
function ensureDefaults() {}
function dateParams() {return new URLSearchParams({date_from:'2026-01-01'});}
function escapeHtml(s) {return s;}
function render() {renders++;}
async function fetch(url, init) {
 calls.push({url, init});
 return {status: mismatch ? 409 : 200, ok: !mismatch,
 headers: {get: () => mismatch ? '1' : null}, json: async () => ({kpis:{}})};
}
''' + helpers + '''
(async () => {
 await loadReport(); await loadReport(); exportReport('xlsx');
 mismatch = true; await loadReport();
 await loadReport(); exportReport('pdf');
 console.log(JSON.stringify({calls, opened, renders, buttons, stale: sessionStale}));
})().catch(e => {console.error(e);process.exit(1)});
''')
    assert len(out['calls']) == 3
    assert all(call['init']['headers'] == {'X-POS-User-ID': '7', 'X-POS-Role': 'manager'}
               for call in out['calls'])
    assert out['opened'] == ['/api/deliveries/export?date_from=2026-01-01&pos_user_id=7&pos_role=manager&format=xlsx']
    assert out['renders'] == 2 and out['stale']
    assert all(button['disabled'] for button in out['buttons'].values())

