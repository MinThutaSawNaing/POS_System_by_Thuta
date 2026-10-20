"""Node behavioral coverage for the Reports statement, without application imports."""
import json
import re

import pytest

from test_reports_dashboard_javascript import DASHBOARD, NODE, _function, _run_node_script

pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is required")


def run_report(body):
    source = DASHBOARD.read_text(encoding="utf-8")
    reports = source[source.index('      // Reports functions'):source.index('      // User management functions')]
    helpers = '\n'.join(_function(source, name) for name in (
        'toCents', 'centsToNumber', 'buildReportQuery', 'exportReport'))
    return _run_node_script('''
const fields = {};
for (const id of ['start-date','end-date','report-scope','report-branch-selector',
                  'report-results','generate-report-btn','report-context','salesChart','paymentChart']) {
 fields[id] = {value:'', innerHTML:'', textContent:'', disabled:false, attrs:{},
   setAttribute(key,value){this.attrs[key]=value}, getContext(){return {}}};
}
fields['start-date'].value = '2026-09-01'; fields['end-date'].value = '2026-09-30';
fields['report-scope'].value = 'current';
const document = {getElementById:id=>fields[id]};
let CURRENT_USER_ROLE = 'manager', currentBranch = {id:3,name:'Alpha <branch>'};
let dashboardSessionStale = false, dashboardBranchStale = false;
let salesChart = null, paymentChart = null;
const charts = [], requests = [], downloads = [], toasts = [];
function hasManagerOrBossAccess(){return ['manager','boss'].includes(CURRENT_USER_ROLE)}
function showToast(message,kind){toasts.push({message,kind})}
function formatCurrency(value){return 'MMK ' + value.toFixed(2)}
function openReportDownload(url){downloads.push(url);return true}
function Chart(ctx,config){charts.push(config);this.destroy=()=>{}}
let rows = [], ok = true;
let fetch = async url => {requests.push(url);return {ok,json:async()=>rows}};
''' + helpers + reports + '\n(async()=>{\n' + body + '\n})().catch(e=>{console.error(e);process.exit(1)});')


@pytest.mark.parametrize('start,end', [('', ''), ('2026-09-30', '2026-09-01')])
def test_generate_and_export_reject_invalid_dates(start, end):
    out = run_report(f'''
fields['start-date'].value={json.dumps(start)};fields['end-date'].value={json.dumps(end)};
await generateReport(); const exported=exportReport();
console.log(JSON.stringify({{requests,downloads,toasts,exported,disabled:fields['generate-report-btn'].disabled}}));
''')
    assert out['requests'] == out['downloads'] == []
    assert len(out['toasts']) == 2
    assert out['exported'] is False
    assert out['disabled'] is False


def test_export_preserves_scopes_and_is_manager_boss_only():
    out = run_report('''
fields['report-scope'].value='branch';fields['report-branch-selector'].value='9';
exportReport();CURRENT_USER_ROLE='cashier';exportReport();
CURRENT_USER_ROLE='boss';fields['report-scope'].value='all';exportReport();
console.log(JSON.stringify({downloads}));
''')
    assert out['downloads'] == [
        '/api/reports/sales/export?start=2026-09-01&end=2026-09-30&scope=branch&report_branch_id=9',
        '/api/reports/sales/export?start=2026-09-01&end=2026-09-30&scope=all',
    ]


def test_currency_escaping_split_allocation_and_charts_share_one_fetch():
    out = run_report('''
rows=[{transaction_id:'<img onerror="bad">',date:'2026-09-10T12:00:00',total:'0.30',tax:'0.01',
 refund_amount:'0.10',payment_method:'<script>',payment_breakdown:{cash:'0.10','<card>':'0.20'}},
 {transaction_id:'next',date:'2026-09-11T12:00:00',total:'0.10',tax:'0.02',payment_method:'cash'}];
await generateReport();
console.log(JSON.stringify({html:fields['report-results'].innerHTML,requests,charts,
 totals:reportPaymentTotals(rows),busy:fields['report-results'].attrs['aria-busy'],
 disabled:fields['generate-report-btn'].disabled,context:fields['report-context'].textContent}));
''')
    assert len(out['requests']) == 1
    assert 'MMK 0.40' in out['html'] and 'MMK 0.03' in out['html']
    assert '$' not in out['html']
    assert '<img' not in out['html'] and '<script>' not in out['html']
    assert '&lt;img onerror=&quot;bad&quot;&gt;' in out['html']
    assert '&lt;card&gt;' in out['html'] and '&lt;script&gt;' in out['html']
    assert out['totals'] == {'cash': {'cents': 20, 'count': 2}, '<card>': {'cents': 20, 'count': 1}}
    assert out['charts'][0]['data']['datasets'][0]['data'] == [0.3, 0.1]
    assert out['charts'][1]['data']['datasets'][0]['data'] == [0.2, 0.2]
    assert out['busy'] == 'false' and out['disabled'] is False
    assert 'Alpha <branch>' in out['context']
    assert 'Change given' in out['html'] and 'not refunds' in out['html']


@pytest.mark.parametrize('failure', ['empty', 'http', 'network', 'shape'])
def test_empty_and_error_states(failure):
    setup = {
        'empty': '', 'http': 'ok=false;',
        'network': "fetch=async()=>{throw new Error('<unsafe>')};",
        'shape': 'rows={error:"bad"};',
    }[failure]
    out = run_report(setup + '''
await generateReport();console.log(JSON.stringify({html:fields['report-results'].innerHTML,
 charts,busy:fields['report-results'].attrs['aria-busy'],disabled:fields['generate-report-btn'].disabled}));
''')
    assert ('No transactions found' if failure == 'empty' else 'Failed to generate report') in out['html']
    assert '<unsafe>' not in out['html']
    assert len(out['charts']) == (2 if failure == 'empty' else 0)
    assert out['busy'] == 'false' and out['disabled'] is False



@pytest.mark.parametrize('change', ['filter', 'branch', 'scope', 'identity', 'overlap', 'invalid'])
def test_stale_generate_responses_are_ignored(change):
    changes = {
        'filter': "fields['end-date'].value='2026-09-20';invalidateSalesReport();",
        'branch': 'currentBranch={id:4,name:"Beta"};',
        'scope': "fields['report-scope'].value='all';invalidateSalesReport();",
        'identity': 'dashboardSessionStale=true;',
        'overlap': "const newer=generateReport(); pending[1]({ok:true,json:async()=>[]});await newer;",
        'invalid': "fields['start-date'].value='';await generateReport();",
    }
    out = run_report('''
const pending=[];fetch=url=>{requests.push(url);return new Promise(resolve=>pending.push(resolve))};
const old=generateReport();const busy=fields['generate-report-btn'].disabled;
''' + changes[change] + '''
const before=fields['report-results'].innerHTML;
pending[0]({ok:true,json:async()=>[{transaction_id:'OLD',date:'2026-09-02',total:999}]});await old;
console.log(JSON.stringify({busy,before,after:fields['report-results'].innerHTML,charts}));
''')
    assert out['busy'] is True
    assert out['before'] == out['after']
    assert 'OLD' not in out['after']
    assert len(out['charts']) == (2 if change == 'overlap' else 0)


@pytest.mark.parametrize('chart', ['initSalesChart', 'initPaymentChart'])
@pytest.mark.parametrize('change', ['branch', 'overlap', 'filter'])
def test_standalone_chart_context_and_overlap_guards(chart, change):
    action = {
        'branch': 'currentBranch={id:8};',
        'filter': "fields['end-date'].value='2026-09-20';",
        'overlap': f'const newer={chart}([]);await newer;',
    }[change]
    out = run_report(f'''
let resolve;fetch=()=>new Promise(r=>resolve=r);
const old={chart}();{action}
resolve({{ok:true,json:async()=>[{{date:'2026-09-02',total:999,payment_method:'cash'}}]}});await old;
console.log(JSON.stringify({{charts}}));
''')
    assert len(out['charts']) == (1 if change == 'overlap' else 0)
    if out['charts']:
        assert out['charts'][0]['data']['datasets'][0]['data'] == []


def test_latest_transactions_limit_and_order():
    out = run_report('''
rows=Array.from({length:7},(_,i)=>({transaction_id:'TX'+i,date:'2026-09-0'+(i+1),total:1,payment_method:'cash'}));
await generateReport();console.log(JSON.stringify({html:fields['report-results'].innerHTML}));
''')
    assert 'Showing latest 5 of 7' in out['html']
    assert 'TX0' not in out['html'] and 'TX1' not in out['html']
    assert out['html'].index('TX6') < out['html'].index('TX2')


def test_report_markup_and_full_dashboard_javascript_syntax():
    source = DASHBOARD.read_text(encoding='utf-8')
    section = source[source.index('<!-- Reports Section -->'):source.index('<!-- Promotions Section -->')]
    for field in ('start-date', 'end-date', 'report-scope', 'report-branch-selector'):
        assert f'for="{field}"' in section
    assert 'aria-live="polite"' in section
    assert 'Generate report' in section and 'Download Excel' in section
    assert '{% if session.role in ["manager", "boss"] %}' in section
    css = section.split('<style>')[1].split('</style>')[0]
    assert all(rule.strip().startswith('#reports-section') for rule in css.split('}') if rule.strip())
    scripts = re.findall(r'<script\b[^>]*>(.*?)</script>', source, re.S)
    scripts = [re.sub(r'\{\{.*?\}\}', 'null', re.sub(r'\{%.*?%\}', '', script, flags=re.S), flags=re.S)
               for script in scripts]
    # Parse every inline script without executing browser code.
    out = _run_node_script('const scripts=' + json.dumps(scripts) + ';for(const s of scripts)new Function(s);console.log(JSON.stringify(true));')
    assert out is True
