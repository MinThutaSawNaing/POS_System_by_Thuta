"""Real Chromium profiles -> one live central Flask/SQLite app; no browser library.

Run only through run_tests.py. A test-only WSGI wrapper serves the harness and
signed test sessions; all checkouts use the unmodified production /api/sales.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
from threading import Barrier, Event, Thread
from urllib.parse import parse_qs

import pytest
from werkzeug.serving import make_server

from test_checkout_recovery import backend, world
from test_offline_sync_javascript import _function


ROOT = Path(__file__).parent
BROWSER = next((str(path) for path in (
    Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Google/Chrome/Application/chrome.exe',
    Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe',
) if path.is_file()), None) or shutil.which('chromium') or shutil.which('google-chrome')


@pytest.mark.skipif(not BROWSER, reason='Local Chrome/Chromium/Edge required')
def test_four_real_browser_profiles_reload_lost_response_and_concurrent_sync(world, tmp_path):
    w, m = world, world.m
    source = (ROOT / 'templates/dashboard.html').read_text(encoding='utf-8')
    journal = (ROOT / 'public/offline-checkout.js').read_text(encoding='utf-8')
    functions = '\n'.join(_function(source, name) for name in ('syncPendingSales', 'checkoutScopeCurrent'))
    gate = Barrier(4)
    completed = {code: Event() for code in 'ABCD'}
    outcomes = {}

    def harness(environ, start_response):
        path = environ['PATH_INFO']
        if path == '/_checkout_result':
            value = json.loads(environ['wsgi.input'].read(int(environ['CONTENT_LENGTH'])))
            outcomes[value['code']] = value
            completed[value['code']].set()
            start_response('200 OK', [('Content-Type', 'text/plain')])
            return [b'ok']
        if path == '/_checkout_gate':
            gate.wait(timeout=35)
            start_response('200 OK', [('Content-Type', 'text/plain')])
            return [b'ready']
        if path != '/_checkout_test':
            return m.app(environ, start_response)
        query = parse_qs(environ.get('QUERY_STRING', ''))
        code = query['branch'][0]
        r = w.records[code]
        session = {'user_id': r.user, 'role': 'manager', 'username': 'recovery-' + code, 'branch_id': r.branch}
        cookie = m.app.session_interface.get_signing_serializer(m.app).dumps(session)
        config = json.dumps({'code': code, 'user': r.user, 'branch': r.branch, 'product': r.product})
        script = '''
const config=CONFIG, CACHE_USER_ID=config.user, CACHE_USER_ROLE='manager';
const currentBranch={id:config.branch};
let dashboardSessionStale=false,dashboardBranchStale=false,branchSwitchInFlight=false,isSyncingPendingSales=false;
let sequence=0,dropNext=false,posts=0;const nativeFetch=window.fetch.bind(window);
const offline=['C','D'].includes(config.code);
function check(value,message){if(!value)throw Error(message);}
function isBrowserOffline(){return false;}function setConnectionStatus(){}
function showToast(){}function updatePendingSalesBadge(){}function loadSales(){}
function loadDashboardStats(){}function invalidateProductsCache(){}
function checkoutJournal(){return OfflineCheckout.create({storage:localStorage,userId:CACHE_USER_ID,
 branchId:currentBranch.id,current:()=>true,locks:navigator.locks,id:()=>crypto.randomUUID(),
 fetch:async(url,opts)=>{
  posts++;opts.headers['X-POS-User-ID']=String(config.user);opts.headers['X-POS-Role']='manager';
  opts.headers['X-POS-Branch-ID']=String(config.branch);
  const response=await nativeFetch(url,opts);
  if(dropNext){dropNext=false;await response.text();throw Error('Simulated lost response AFTER central commit');}
  return response;
 }});}
''' .replace('CONFIG', config) + journal + '\n' + functions + '''
(async()=>{
 const recovery=checkoutJournal();
 if(!new URL(location.href).searchParams.has('resume')){
  for(let i=0;i<3;i++) recovery.prepare({items:[{product_id:config.product,quantity:1,price:100,tax_rate:5}],
   payment_method:'cash',cash_received:200},{total:105,items:[]},offline);
  check(recovery.list().length===3,'initial local outbox');
  location.replace(location.href+'&resume=1');return;
 }
 check(recovery.list().length===3,'reload must retain all original intents');
 await nativeFetch('/_checkout_gate');
 const first=recovery.list()[0];dropNext=true;
 await recovery.send(first.transaction_id,false,!offline);
 check(recovery.list().find(e=>e.transaction_id===first.transaction_id).status==='unknown','lost reply retained');
 check(posts===1,'one first transport');
 await recovery.send(first.transaction_id,false,!offline);check(posts===1,'unknown never auto retries');
 if(offline){await syncPendingSales();await syncPendingSales();}
 else {for(const e of recovery.list().filter(e=>e.status==='pending'))await recovery.send(e.transaction_id,false,true);}
 const originalNow=Date.now;Date.now=()=>originalNow()+61000;
 check((await recovery.recover(first.transaction_id)).success,'automatic same ID replay');
 Date.now=originalNow;
 check(recovery.list().every(e=>e.status==='synced'),'all records must settle');
 const before=posts;
 await Promise.all(Array.from({length:5},()=>recovery.send(first.transaction_id,true,false,'Browser-test manager')));
 check(posts===before,'acknowledged records never retransport');
 const rows=recovery.list();
 document.body.dataset.testResult='passed';
 document.getElementById('result').textContent=JSON.stringify({code:config.code,posts,
  ids:rows.map(e=>e.transaction_id),attempts:rows.map(e=>e.attempts)});
 await nativeFetch('/_checkout_result',{method:'POST',body:document.getElementById('result').textContent});
})().catch(error=>nativeFetch('/_checkout_result',{method:'POST',body:JSON.stringify({code:config.code,error:error.stack})}));
'''
        page = '<!doctype html><body><pre id="result"></pre><script>' + script + '</script></body>'
        start_response('200 OK', [('Content-Type', 'text/html; charset=utf-8'),
                                  ('Set-Cookie', 'session=' + cookie + '; Path=/; HttpOnly; SameSite=Lax'),
                                  ('Cache-Control', 'no-store')])
        return [page.encode('utf-8')]

    server = make_server('127.0.0.1', 0, harness, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def browser(code):
        log = open(tmp_path / (code + '.log'), 'w', encoding='utf-8')
        process = subprocess.Popen([
            BROWSER, '--headless', '--disable-gpu', '--no-sandbox', '--no-first-run',
            '--no-default-browser-check', f'--user-data-dir={tmp_path / code}',
            '--remote-debugging-port=0', '--disable-background-timer-throttling',
            f'http://127.0.0.1:{server.server_port}/_checkout_test?branch={code}',
        ], stdout=log, stderr=log)
        try:
            assert completed[code].wait(timeout=65), 'Browser did not finish: ' + code
            result = outcomes[code]
            assert not result.get('error'), result
            return result
        finally:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
            else:
                process.terminate()
            process.wait(timeout=10)
            log.close()

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(browser, 'ABCD'))
        assert len({txn for result in results for txn in result['ids']}) == 12
        for result in results:
            assert result['posts'] == 4
            assert sorted(result['attempts']) == [1, 1, 2]
        with m.app.app_context():
            assert m.Sale.query.count() == m.CheckoutRequest.query.count() == m.SaleItem.query.count() == 12
            assert m.Debt.query.count() == 0
            for code, record in w.records.items():
                assert m.db.session.get(m.Product, record.product).stock == 37
                assert m.Sale.query.filter_by(branch_id=record.branch).count() == 3
                assert m.CheckoutRequest.query.filter_by(branch_id=record.branch, offline=code in 'CD').count() == 3
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)