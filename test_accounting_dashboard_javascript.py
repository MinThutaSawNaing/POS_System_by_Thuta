import json
from pathlib import Path
import subprocess


SOURCE = Path(__file__).with_name('public') / 'accounting.js'


def _function(source, name):
    declaration = f'async function {name}('
    if declaration not in source:
        declaration = f'function {name}('
    start = source.index(declaration)
    opening = source.index('{', start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == '{':
            depth += 1
        elif source[index] == '}':
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f'Unclosed function: {name}')


def test_accounting_conflicts_retain_the_operation_key():
    source = _function(SOURCE.read_text(encoding='utf-8'), 'request')
    script = f"""
const API = '';
let status = 409;
function online() {{ return true; }}
function usable() {{ return true; }}
globalThis.fetch = async () => ({{ok: false, status, redirected: false,
  json: async () => ({{success: false, message: 'Busy'}})}});
{source}
(async () => {{
  const result = [];
  for (status of [409, 400, 503]) {{
    try {{ await request('/setup', {{}}); result.push(null); }}
    catch (error) {{ result.push(error.definite); }}
  }}
  console.log(JSON.stringify(result));
}})();
"""
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [False, True, False]


def test_uncertain_setup_remains_retryable_without_opening_preview():
    source = _function(SOURCE.read_text(encoding='utf-8'), 'syncControls')
    script = f"""
const controls = {{}};
function control(id) {{
  return controls[id] ||= {{disabled: false, querySelectorAll() {{ return []; }}}};
}}
const tabs = [];
const state = {{pending: false, loading: false, setup: {{uncertain: true}}, editor: null, opening: null}};
function online() {{ return true; }}
function usable() {{ return true; }}
function el(id) {{ return control(id); }}
const root = {{querySelectorAll() {{ return []; }}}};
{source}
syncControls();
const uncertain = {{activate: controls.activate.disabled, fields: controls['setup-fields'].disabled,
  refresh: controls.refresh.disabled}};
state.setup.uncertain = false;
syncControls();
const inactive = {{activate: controls.activate.disabled}};
console.log(JSON.stringify({{uncertain, inactive}}));
"""
    result = subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == {
        'uncertain': {'activate': False, 'fields': True, 'refresh': True},
        'inactive': {'activate': True},
    }


def test_opening_balances_use_reviewed_branches_before_activation():
    source = SOURCE.read_text(encoding='utf-8')
    helpers = '\n'.join(_function(source, name) for name in ('id', 'cents', 'fromCents', 'money', 'readOpening'))
    script = f"""
const state = {{status: {{locations: []}}, opening: {{locations: [
  {{branch_id: 1, name: 'Main'}}, {{branch_id: 2, name: 'Second'}}
]}}}};
const openingLocations = () => state.opening.locations;
const values = {{location: '2', account: 'CASH', debit: '100.00', credit: '0.00'}};
const row = {{querySelector(selector) {{
  const name = selector.match(/\\[name="(.*?)"\\]/)[1];
  return {{value: values[name]}};
}}}};
function el() {{ return {{children: [row]}}; }}
const value = (parent, name) => parent.querySelector(`[name="${{name}}"]`).value.trim();
{helpers}
console.log(JSON.stringify(readOpening()));
"""
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        {'branch_id': 2, 'account_code': 'CASH', 'debit': '100.00', 'credit': '0.00'},
    ]


def test_opening_balances_allow_central_cash_and_bank():
    source = SOURCE.read_text(encoding='utf-8')
    start = source.index('const openingLocations =')
    opening_locations = source[start:source.index(';', start) + 1]
    helpers = '\n'.join(_function(source, name) for name in ('id', 'cents', 'fromCents', 'money', 'readOpening'))
    script = f"""
const state = {{status: {{locations: []}}, opening: {{locations: [
  {{branch_id: 1, name: 'Main'}}
]}}}};
const values = {{location: 'null', account: 'BANK', debit: '0.00', credit: '25.00'}};
const row = {{querySelector(selector) {{
  const name = selector.match(/\\[name="(.*?)"\\]/)[1];
  return {{value: values[name]}};
}}}};
function el() {{ return {{children: [row]}}; }}
const value = (parent, name) => parent.querySelector(`[name="${{name}}"]`).value.trim();
{opening_locations}
{helpers}
console.log(JSON.stringify(readOpening()));
"""
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        {'branch_id': None, 'account_code': 'BANK', 'debit': '0.00', 'credit': '25.00'},
    ]


def test_central_opening_choice_and_equity_use_the_same_location_mapping():
    source = SOURCE.read_text(encoding='utf-8')
    start = source.index('const openingLocations =')
    opening_locations = source[start:source.index(';', start) + 1]
    helpers = '\n'.join(_function(source, name) for name in (
        'cents', 'fromCents', 'addOpeningLine', 'updateEquity'))
    script = f"""
const state = {{status: {{currency: 'USD'}}, opening: {{
  currency: 'USD', locations: [{{branch_id: 1, name: 'Main', stock_value: '75.00', receivables: '40.00'}}],
  warehouse: {{value: '60.00'}}, unbilled_receipts: '0.00'
}}}};
const row = values => ({{querySelector(selector) {{
  const name = selector.match(/\\[name="(.*?)"\\]/)[1];
  return {{value: values[name]}};
}}}});
const elements = {{
  'opening-lines': {{children: []}}, 'opening-payables': {{children: []}},
  'opening-equity': {{textContent: ''}}
}};
let locationChoices;
function el(id) {{ return elements[id]; }}
function removableLine() {{ return {{fields: {{dataset: {{}}}}, row: {{}}}}; }}
function field(parent, name, label, type, value, choices) {{
  if (name === 'location') locationChoices = choices;
}}
function accountOptions() {{ return []; }}
const value = (parent, name) => parent.querySelector(`[name="${{name}}"]`).value.trim();
{opening_locations}
{helpers}
addOpeningLine();
const choices = locationChoices;
elements['opening-lines'].children = [row({{location: 'null', debit: '0.00', credit: '25.00'}})];
elements['opening-payables'].children = [row({{amount: '10.00'}})];
updateEquity();
console.log(JSON.stringify({{choices, equity: elements['opening-equity'].textContent}}));
"""
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output['choices'] == [[1, 'Main'], [None, 'Central warehouse / head office']]
    assert output['equity'].startswith(
        'Computed opening equity: 140.00 USD. Main: 115.00; Central warehouse: 25.00.')


def test_settled_connection_status_reloads_live_only_accounting():
    source = _function(SOURCE.read_text(encoding='utf-8'), 'handleAccountingConnection')
    script = f"""
const calls = {{load: 0, clear: 0, sync: 0}};
const state = {{identity: 'manager:1', live: false, loading: false, pending: false, editor: null, setup: null}};
const notice = {{textContent: ''}};
function visible() {{ return true; }}
function loadAccounting() {{ calls.load++; state.loading = true; }}
function clearLiveAccounting() {{ calls.clear++; state.live = false; }}
function syncControls() {{ calls.sync++; }}
function el(id) {{ return notice; }}
function message(target, text) {{ target.textContent = text; }}
{source}
handleAccountingConnection({{detail: {{status: 'online'}}}});
handleAccountingConnection({{detail: {{status: 'synced'}}}});
state.loading = false;
state.editor = {{uncertain: true}};
handleAccountingConnection({{detail: {{status: 'online'}}}});
const uncertain = {{live: state.live, notice: notice.textContent}};
state.live = false;
state.editor = null;
handleAccountingConnection({{detail: {{status: 'offline'}}}});
state.identity = null;
handleAccountingConnection({{detail: {{status: 'online'}}}});
console.log(JSON.stringify({{calls, uncertain}}));
"""
    result = subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)
    output = json.loads(result.stdout)
    assert output['calls'] == {'load': 1, 'clear': 1, 'sync': 1}
    assert output['uncertain']['live'] is True
    assert 'retained request' in output['uncertain']['notice']


def test_dashboard_emits_connection_status_after_updating_indicator():
    dashboard = Path(__file__).with_name('templates') / 'dashboard.html'
    source = _function(dashboard.read_text(encoding='utf-8'), 'setConnectionStatus')
    accounting = SOURCE.read_text(encoding='utf-8')
    assert 'window.addEventListener("pos-connection-status", handleAccountingConnection);' in accounting
    script = f"""
const events = [];
const elements = {{
  'connection-status-indicator': {{className: ''}},
  'connection-status-text': {{textContent: ''}},
  'connection-status-icon': {{className: ''}}
}};
const CONNECTION_STATUS = {{
  online: {{text: 'Online mode detected', icon: 'bi-wifi'}},
  offline: {{text: 'Offline mode detected', icon: 'bi-wifi-off'}}
}};
let connectionStatusTimer = null;
const document = {{getElementById: id => elements[id]}};
const window = {{dispatchEvent: event => events.push({{type: event.type, status: event.detail.status,
  text: elements['connection-status-text'].textContent}})}};
class CustomEvent {{
  constructor(type, options) {{ this.type = type; this.detail = options.detail; }}
}}
{source}
setConnectionStatus('online');
setConnectionStatus('offline');
console.log(JSON.stringify(events));
"""
    result = subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == [
        {'type': 'pos-connection-status', 'status': 'online', 'text': 'Online mode detected'},
        {'type': 'pos-connection-status', 'status': 'offline', 'text': 'Offline mode detected'},
    ]
