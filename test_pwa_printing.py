"""Regression coverage for hidden-iframe printing in browsers and installed PWAs."""

from datetime import datetime
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from werkzeug.serving import make_server

from test_checkout_recovery import backend, world
from test_offline_sync_javascript import _function, _run_node_script


DASHBOARD = Path('templates/dashboard.html').read_text(encoding='utf-8')
NODE = shutil.which('node')
BROWSER = next((str(path) for path in (
    Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Google/Chrome/Application/chrome.exe',
    Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe',
) if path.is_file()), None) or shutil.which('chromium') or shutil.which('google-chrome')


def test_print_frame_is_hidden_and_cleaned_up_after_printing():
    assert 'function createPrintFrame()' in DASHBOARD
    assert 'className = "pos-print-frame"' in DASHBOARD
    assert 'frame.setAttribute("aria-hidden", "true")' in DASHBOARD
    assert 'width: "1px"' in DASHBOARD
    assert 'opacity: "0"' in DASHBOARD
    assert 'printWindow.addEventListener("afterprint"' in DASHBOARD
    assert 'setTimeout(cleanup, 120000)' in DASHBOARD
    assert 'target.origin !== window.location.origin' in DASHBOARD


@pytest.mark.parametrize('name', [
    'openReceiptWindow', 'openReturnExchangeReceipt', 'printDeliverySlip',
    'printDebtReceipt', 'printPurchaseOrderDirect', 'printBranchInventory',
])
def test_document_print_actions_use_hidden_frames_not_popups(name):
    block = _function(DASHBOARD, name)
    assert 'loadUrlInPrintFrame' in block
    assert 'window.open' not in block


def test_labels_and_offline_receipts_use_the_same_hidden_frame_flow():
    labels = DASHBOARD.split('function generateBarcodeLabels()', 1)[1].split(
        '// ============================================', 1
    )[0]
    assert 'const printFrame = createPrintFrame();' in labels
    assert 'form.target = printFrame.name;' in labels
    assert 'navigatePrintFrame(printFrame, () => form.submit())' in labels
    assert '"_blank"' not in labels

    offline = _function(DASHBOARD, 'openOfflineReceiptWindow')
    assert 'const printFrame = createPrintFrame();' in offline
    assert 'printPrintFrame(printFrame)' in offline
    assert 'window.open' not in offline
    assert 'window.print()' not in offline


def test_parent_owns_autoprint_to_avoid_duplicate_dialogs():
    loader = _function(DASHBOARD, 'loadUrlInPrintFrame')
    assert 'target.searchParams.delete("autoprint")' in loader
    assert '?autoprint=1' not in DASHBOARD


@pytest.mark.parametrize('template', [
    'receipt.html', 'exchange_receipt.html', 'delivery_slip.html', 'barcode_labels.html',
])
def test_direct_document_print_buttons_ignore_repeated_clicks(template):
    source = (Path('templates') / template).read_text(encoding='utf-8')
    assert 'if (printing) return;' in source
    assert 'finally {' in source


@pytest.mark.skipif(NODE is None, reason='Node required')
def test_hidden_frame_ignores_blank_load_prints_once_and_cleans_up():
    helpers = '\n'.join(_function(DASHBOARD, name) for name in (
        'createPrintFrame', 'cleanupPrintFrame', 'waitForPrintLayout',
        'printPrintFrame', 'dashboardIdentityUrl', 'navigatePrintFrame',
        'loadUrlInPrintFrame',
    ))
    script = r'''
const realSetTimeout = setTimeout;
const timers = [];
globalThis.setTimeout = (callback, delay) => { timers.push({callback, delay}); return timers.length; };
globalThis.clearTimeout = () => {};
const printFrameCleanups = new WeakMap();
let activePrintFrame = null;
const CACHE_USER_ID = 7, CACHE_USER_ROLE = 'cashier';
const currentBranch = {id: 3};
let dashboardSessionStale = false, dashboardBranchStale = false;
let printCalls = 0, removed = 0, parentFocus = 0, afterPrint;
const handlers = {};
const printDocument = {fonts: {ready: Promise.resolve()}, images: [], body: {textContent: ''}};
const printWindow = {
  location: {href: 'about:blank'},
  document: printDocument,
  addEventListener(event, callback) { if (event === 'afterprint') afterPrint = callback; },
  focus() {},
  print() { printCalls++; afterPrint(); },
};
const frame = {
  style: {},
  setAttribute() {},
  addEventListener(event, callback) { handlers[event] = callback; },
  removeEventListener(event) { if (event === 'load') handlers.load = null; },
  remove() { removed++; },
  contentWindow: printWindow,
  contentDocument: printDocument,
  set src(url) {
    this.url = url;
    realSetTimeout(() => {
      printWindow.location.href = new URL(url, window.location.href).href;
      handlers.load();
    }, 0);
  },
};
const window = {
  location: {href: 'https://pos.test/', origin: 'https://pos.test'},
  focus() { parentFocus++; },
  requestAnimationFrame: callback => realSetTimeout(callback, 0),
};
const document = {body: {appendChild() {}}, createElement: () => frame};
function showToast() {}
function blockStaleDashboard() { dashboardSessionStale = true; }
''' + helpers + r'''
const opened = loadUrlInPrintFrame('/api/sales/sale/print?autoprint=1');
handlers.load();
const beforeDocumentLoad = printCalls;
realSetTimeout(() => console.log(JSON.stringify({
  opened, beforeDocumentLoad, printCalls, removed, parentFocus,
  url: frame.url,
  hasSafetyCleanup: timers.some(timer => timer.delay === 120000),
})), 50);
'''
    out = _run_node_script(script)
    assert out['opened'] is True
    assert out['beforeDocumentLoad'] == 0
    assert out['printCalls'] == 1
    assert out['removed'] == 1
    assert out['parentFocus'] == 1
    assert out['hasSafetyCleanup'] is True
    assert out['url'] == '/api/sales/sale/print?pos_user_id=7&pos_role=cashier&pos_branch_id=3'


_CDP_SCRIPT = r'''
const [port, loginUrl, rawConfig] = process.argv.slice(2);
const config = JSON.parse(rawConfig);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function main() {
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const target = targets.find(item => item.type === 'page');
  if (!target) throw new Error('Chromium did not expose a page target');
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  const pending = new Map();
  const network = [];
  const exceptions = [];
  let sequence = 0;
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const item = pending.get(message.id);
      if (!item) return;
      pending.delete(message.id);
      if (message.error) item.reject(new Error(JSON.stringify(message.error)));
      else item.resolve(message.result);
      return;
    }
    if (message.method === 'Network.responseReceived') {
      const response = message.params.response;
      network.push({url: response.url, status: response.status, mimeType: response.mimeType});
    }
    if (message.method === 'Runtime.exceptionThrown') {
      const details = message.params.exceptionDetails;
      exceptions.push({
        text: details.text,
        description: details.exception?.description || '',
        url: details.url || '',
        line: details.lineNumber,
      });
    }
  });
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, {once: true});
    socket.addEventListener('error', reject, {once: true});
  });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence;
    pending.set(id, {resolve, reject});
    socket.send(JSON.stringify({id, method, params}));
  });
  const evaluate = async (expression, awaitPromise = false) => {
    const result = await send('Runtime.evaluate', {
      expression, awaitPromise, returnByValue: true, userGesture: true,
    });
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.exception?.description
        || result.exceptionDetails.text || 'Browser evaluation failed');
    }
    return result.result.value;
  };

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Network.enable');
  await send('Page.addScriptToEvaluateOnNewDocument', {source: `
    (() => {
      window.__printRecords = [];
      window.__printErrors = [];
      window.__openedWindows = [];
      window.open = (...args) => { window.__openedWindows.push(args); return null; };
    })();
  `});
  await send('Page.navigate', {url: loginUrl});

  async function browserCheck(options) {
    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
    const records = window.__printRecords ||= [];
    const errors = window.__printErrors ||= [];
    const originalPrintPrintFrame = printPrintFrame;
    printPrintFrame = async frame => {
      const printWindow = frame.contentWindow;
      const record = () => {
        let ready = 'plugin';
        let text = '';
        try {
          ready = printWindow.document?.readyState || 'plugin';
          text = printWindow.document?.body?.innerText?.slice(0, 600) || '';
        } catch (error) {}
        records.push({
          href: String(printWindow.location.href), ready, text,
          hidden: getComputedStyle(frame).opacity === '0'
            && frame.getAttribute('aria-hidden') === 'true',
          frameCount: document.querySelectorAll('.pos-print-frame').length,
        });
      };
      try {
        const probe = () => { record(); printWindow.dispatchEvent(new Event('afterprint')); };
        Object.defineProperty(printWindow, 'print', {
          configurable: true, writable: true, value: probe,
        });
      } catch (error) { errors.push(String(error)); }
      return originalPrintPrintFrame(frame);
    };
    const waitFor = async (check, label, timeout = 20000) => {
      const deadline = Date.now() + timeout;
      while (Date.now() < deadline) {
        if (check()) return;
        await sleep(50);
      }
      throw new Error(`Timed out waiting for ${label}; records=${JSON.stringify(window.__printRecords)}; `
        + `errors=${JSON.stringify(window.__printErrors)}; frames=${JSON.stringify(
          [...document.querySelectorAll('.pos-print-frame')].map(frame => {
            let printWindow = null;
            try { printWindow = frame.contentWindow; } catch (error) {}
            return {
              src: frame.src,
              href: printWindow?.location?.href,
              ready: printWindow?.document?.readyState,
            };
          }))}`);
    };
    const actions = [];
    const runPrint = async (name, action) => {
      const before = window.__printRecords.length;
      const accepted = action();
      await waitFor(() => window.__printRecords.length === before + 1
        && !document.querySelector('.pos-print-frame'), name);
      actions.push({name, accepted, record: window.__printRecords[before]});
    };

    const firstAccepted = openReceiptWindow(options.saleId);
    const duplicateAccepted = openReturnExchangeReceipt(options.workflowId);
    await waitFor(() => window.__printRecords.length === 1
      && !document.querySelector('.pos-print-frame'), 'receipt and duplicate guard');
    actions.push({name: 'sale', accepted: firstAccepted, record: window.__printRecords[0]});

    await runPrint('delivery', () => printDeliverySlip(options.deliveryId));
    await runPrint('return', () => openReturnExchangeReceipt(options.workflowId));
    await runPrint('inventory', () => loadUrlInPrintFrame('/api/branch_inventory/print'));
    await runPrint('purchase-order', () => printPurchaseOrderDirect(options.poId));
    await runPrint('goods-receipt', () => loadUrlInPrintFrame(
      `/api/purchase_orders/${options.poId}/receipts/${options.receiptId}/print`));
    currentDebtId = options.debtId;
    await runPrint('debt', () => printDebtReceipt());

    const productId = options.productId;
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'product-checkbox';
    checkbox.dataset.id = String(productId);
    checkbox.checked = true;
    const quantity = document.createElement('input');
    quantity.className = 'barcode-qty';
    quantity.dataset.id = String(productId);
    quantity.value = '1';
    document.body.append(checkbox, quantity);
    await runPrint('labels', () => generateBarcodeLabels());
    checkbox.remove();
    quantity.remove();

    await runPrint('offline', () => openOfflineReceiptWindow(options.offlineReceipt));

    const toasts = [];
    const originalToast = showToast;
    showToast = (message, type) => { toasts.push({message, type}); return originalToast(message, type); };
    const beforeError = window.__printRecords.length;
    const errorAccepted = loadUrlInPrintFrame('/api/sales/not-a-real-sale/print');
    await waitFor(() => !document.querySelector('.pos-print-frame'), 'failed document cleanup');
    await sleep(100);
    showToast = originalToast;

    return {
      actions,
      duplicateAccepted,
      errorAccepted,
      errorToast: toasts.find(item => item.type === 'error') || null,
      errorPrintCount: window.__printRecords.length - beforeError,
      patchErrors: window.__printErrors,
      openedWindows: window.__openedWindows,
      remainingFrames: document.querySelectorAll('.pos-print-frame').length,
    };
  }

  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    try {
      const ready = await evaluate(`({ready: document.readyState, path: location.pathname,
        loaded: typeof openReceiptWindow === 'function',
        branch: typeof currentBranch === 'undefined' ? null : currentBranch?.id})`);
      if (ready.ready === 'complete' && ready.path === '/' && ready.loaded
          && ready.branch === config.branchId) break;
    } catch (error) {}
    await sleep(100);
  }
  const result = await evaluate(`(${browserCheck.toString()})(${JSON.stringify(config)})`, true);
  result.network = network;
  result.runtimeExceptions = exceptions;
  socket.close();
  process.stdout.write(JSON.stringify(result));
}

main().catch(error => {
  console.error(error.stack || error);
  process.exit(1);
});
'''


@pytest.mark.skipif(not BROWSER or NODE is None, reason='Chromium and Node required')
def test_real_browser_hidden_frame_print_flows(backend, world, tmp_path):
    m = backend
    shutil.copytree(Path('public'), Path(m.app.root_path) / 'public', dirs_exist_ok=True)
    record = world.records['A']
    with m.app.app_context():
        sale = m.Sale(transaction_id='browser-print-sale', total=200, tax=0,
                      payment_method='cash', cash_received=300, refund_amount=100,
                      user_id=record.user, branch_id=record.branch)
        m.db.session.add(sale)
        m.db.session.flush()
        sale_item = m.SaleItem(sale_id=sale.id, product_id=record.product,
                               quantity=2, price=100, tax=0)
        customer = m.Customer(name='Demo Customer', branch_id=record.branch)
        m.db.session.add_all([sale_item, customer])
        m.db.session.flush()
        delivery = m.Delivery(delivery_number='PRINT-DELIVERY-1', sale_id=sale.id,
                              customer_id=customer.id, branch_id=record.branch,
                              created_by=record.user, recipient_name='Demo Recipient',
                              recipient_phone='000', delivery_address='Demo address')
        debt = m.Debt(customer_id=customer.id, sale_id=sale.id, amount=200, balance=200,
                      status='pending', created_by=record.user, branch_id=record.branch)
        workflow = m.ReturnExchange(workflow_id='browser-print-return', mode='return',
                                    original_sale_id=sale.id, user_id=record.user,
                                    return_total=100, exchange_total=0, net_total=100,
                                    refund_amount=100, collected_amount=0,
                                    settlement_method='cash')
        m.db.session.add_all([delivery, debt, workflow])
        m.db.session.flush()
        m.db.session.add(m.ReturnExchangeItem(
            return_exchange_id=workflow.id, original_sale_item_id=sale_item.id,
            product_id=record.product, movement='return', quantity=1,
            unit_price=100, tax_rate=0, line_total=100, line_tax=0,
        ))
        supplier = m.Supplier(name='Demo Supplier', branch_id=record.branch)
        m.db.session.add(supplier)
        m.db.session.flush()
        purchase_order = m.PurchaseOrder(po_number='PRINT-PO-1', supplier_id=supplier.id,
                                         status='received', total_amount=120,
                                         created_by=record.user, branch_id=record.branch)
        m.db.session.add(purchase_order)
        m.db.session.flush()
        m.db.session.add(m.PurchaseOrderItem(
            purchase_order_id=purchase_order.id, product_id=record.product,
            ordered_qty=2, received_qty=2, unit_cost=60,
        ))
        m.db.session.add(m.PurchaseInvoice(
            purchase_order_id=purchase_order.id, supplier_id=supplier.id,
            supplier_reference='PRINT-INVOICE-1', invoice_date=datetime.utcnow().date(),
            total_minor=12000, currency='MMK', actor_id=record.user,
            created_at=datetime.utcnow(), snapshot={},
        ))
        goods_receipt = m.GoodsReceipt(
            number='PRINT-GRN-1', purchase_order_id=purchase_order.id,
            actor_id=record.user, created_at=datetime.utcnow(),
            snapshot={'po_number': purchase_order.po_number, 'supplier_name': supplier.name,
                      'items': [{'product_name': 'Demo Product', 'sku': '',
                                 'ordered_qty': 2, 'received_qty': 2}]},
        )
        m.db.session.add(goods_receipt)
        m.db.session.commit()
        config = {
            'saleId': sale.transaction_id, 'deliveryId': delivery.id,
            'workflowId': workflow.workflow_id, 'debtId': debt.id,
            'poId': purchase_order.id, 'receiptId': goods_receipt.id,
            'productId': record.product, 'userId': record.user,
            'productName': m.db.session.get(m.Product, record.product).name,
            'branchId': record.branch, 'role': 'manager',
            'offlineReceipt': {
                'transactionId': 'offline-browser-receipt', 'createdAt': '2026-01-02 10:00',
                'posName': 'Demo POS', 'currencySuffix': 'MMK', 'paperWidthMm': 58,
                'branchName': 'Demo Branch', 'branchAddress': '', 'branchPhone': '',
                'branchEmail': '', 'cashierName': 'Demo Cashier', 'paymentMethod': 'cash',
                'cashReceived': 200, 'change': 100,
                'items': [{'name': 'Demo Product', 'quantity': 2, 'unitPrice': 100,
                           'lineSubtotal': 200, 'taxRate': 0, 'taxAmount': 0}],
                'subtotal': 200, 'tax': 0, 'total': 200, 'checkoutOffline': True,
            },
        }

    signed = m.app.session_interface.get_signing_serializer(m.app).dumps({
        'user_id': config['userId'], 'username': 'print-browser-manager',
        'role': config['role'], 'branch_id': config['branchId'],
    })
    cookie = SimpleCookie()
    cookie['session'] = signed
    cookie['session']['path'] = '/'
    cookie['session']['httponly'] = True
    cookie['session']['samesite'] = 'Lax'

    def login_harness(environ, start_response):
        if environ['PATH_INFO'] == '/__print_browser_login':
            start_response('302 Found', [
                ('Location', '/'), ('Set-Cookie', cookie['session'].OutputString()),
                ('Cache-Control', 'no-store'),
            ])
            return [b'']
        return m.app(environ, start_response)

    server = make_server('127.0.0.1', 0, login_harness, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f'http://127.0.0.1:{server.server_port}'
    log = open(tmp_path / 'chromium.log', 'w', encoding='utf-8')
    process = subprocess.Popen([
        BROWSER, '--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run',
        '--no-default-browser-check', f'--user-data-dir={tmp_path / "profile"}',
        '--remote-debugging-port=0', '--disable-background-timer-throttling',
        '--disable-backgrounding-occluded-windows', '--disable-renderer-backgrounding',
        base_url + '/__print_browser_login',
    ], stdout=log, stderr=log)
    try:
        active_port = tmp_path / 'profile' / 'DevToolsActivePort'
        deadline = time.monotonic() + 20
        while not active_port.is_file():
            if process.poll() is not None or time.monotonic() >= deadline:
                log.flush()
                pytest.fail('Chromium did not start: '
                            + (tmp_path / 'chromium.log').read_text(encoding='utf-8')[-4000:])
            time.sleep(0.05)
        port = int(active_port.read_text(encoding='utf-8').splitlines()[0])
        script_path = tmp_path / 'print-cdp.js'
        script_path.write_text(_CDP_SCRIPT, encoding='utf-8')
        result = subprocess.run(
            [NODE, str(script_path), str(port), base_url + '/__print_browser_login',
             json.dumps(config)], check=False, capture_output=True, text=True,
            encoding='utf-8', timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        output = json.loads(result.stdout)
    finally:
        if process.poll() is None:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
            else:
                process.terminate()
        process.wait(timeout=10)
        log.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)

    expected = {'sale', 'delivery', 'return', 'inventory', 'purchase-order',
                'goods-receipt', 'debt', 'labels', 'offline'}
    assert {action['name'] for action in output['actions']} == expected
    assert output['duplicateAccepted'] is False
    assert output['errorAccepted'] is True
    assert output['errorPrintCount'] == 0
    assert output['errorToast'] is not None
    assert output['patchErrors'] == []
    assert output['openedWindows'] == []
    assert output['remainingFrames'] == 0
    assert output['runtimeExceptions'] == []
    assert all(action['record']['hidden'] for action in output['actions'])
    assert all(action['record']['frameCount'] == 1 for action in output['actions'])
    assert all(action['record']['ready'] in {'complete', 'interactive', 'plugin'}
               for action in output['actions'])

    by_name = {action['name']: action['record'] for action in output['actions']}
    assert config['productName'] in by_name['sale']['text']
    assert 'Demo Recipient' in by_name['delivery']['text']
    assert config['productName'] in by_name['return']['text']
    assert config['productName'] in by_name['labels']['text']
    assert 'OFFLINE SALE' in by_name['offline']['text']
    for name in ('inventory', 'purchase-order', 'goods-receipt', 'debt'):
        assert by_name[name]['ready'] == 'complete', by_name[name]
        assert by_name[name]['text'] == '', by_name[name]

    responses = {urlsplit(item['url']).path: item for item in output['network']
                 if '/print' in urlsplit(item['url']).path}
    for name, action in by_name.items():
        path = urlsplit(action['href']).path
        if name == 'offline':
            offline_url = urlsplit(action['href'])
            assert offline_url.path == '/'
            assert not offline_url.query
            continue
        query = parse_qs(urlsplit(action['href']).query)
        assert 'autoprint' not in query
        assert query['pos_user_id'] == [str(config['userId'])]
        assert query['pos_role'] == ['manager']
        assert query['pos_branch_id'] == [str(config['branchId'])]
        response = responses[path]
        assert response['status'] == 200, response
        expected_type = 'application/pdf' if name in {
            'inventory', 'purchase-order', 'goods-receipt', 'debt',
        } else 'text/html'
        assert response['mimeType'].startswith(expected_type), response
