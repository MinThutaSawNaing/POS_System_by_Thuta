"""Public sales builders/renderers and HTTP contract tests; use run_tests.py.

Assumptions: daily keys date/transactions; payment keys method/transactions.
refund_amount means change given. Split allocations represent sales amounts, not cash tendered.
HTTP uses existing start/end/q and scope/report_branch_id query parameters.
"""
import copy
import importlib.util
import io
import json
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import reports

NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
COLUMNS = ['transaction_id', 'date', 'username', 'branch_name',
           'payment_method', 'payment_breakdown', 'total', 'tax',
           'cash_received', 'change_given']
MONEY = ['total', 'tax', 'cash_received', 'change_given']


def records():
    return [
        dict(transaction_id='CASH-1', date='2026-09-21T00:00:00',
             total=100.25, tax=5.25, cash_received=120.25, refund_amount=20,
             payment_method='cash', payment_breakdown={'cash': 120.25},
             username='alice', branch_name='Alpha'),
        dict(transaction_id='SPLIT-2', date='2026-09-21T23:59:59.999999',
             total=80, tax=4, cash_received=100, refund_amount=20,
             payment_method='split_payment', payment_breakdown={'cash': 60, 'card': 20},
             username='bob', branch_name='Beta'),
        dict(transaction_id='CARD-3', date='2026-09-22T00:00:00',
             total=30.50, tax=1.50, cash_received=30.50, refund_amount=0,
             payment_method='card', payment_breakdown={'card': 30.50},
             username='alice', branch_name='Alpha'),
    ]


def sales_report(rows=None, **overrides):
    options = dict(brand={'name': 'Sales Test Brand', 'address': '1 Test Road',
                          'phone': '555-0100', 'email': 'sales@example.test'},
                   branch_name='Alpha', generated_by='report-manager',
                   filters_text='Payment: all', currency_suffix='MMK',
                   generated_at=datetime(2026, 9, 23, 10, 30),
                   period_text='2026-09-21 to 2026-09-22', scope='all')
    options.update(overrides)
    return reports.build_sales_report(records() if rows is None else rows, **options)


def sheets(report):
    return [reports.build_sales_daily_summary_sheet(report),
            reports.build_sales_payment_summary_sheet(report)]


class Workbook:
    """Read actual OOXML, including styles and worksheet relationships."""
    def __init__(self, data):
        self.archive = zipfile.ZipFile(io.BytesIO(data))
        assert self.archive.testzip() is None
        self.strings = []
        if 'xl/sharedStrings.xml' in self.archive.namelist():
            self.strings = [''.join(si.itertext()) for si in
                            self.xml('xl/sharedStrings.xml')]
        targets = {r.attrib['Id']: r.attrib['Target'] for r in
                   self.xml('xl/_rels/workbook.xml.rels')}
        self.paths = {}
        for sheet in self.xml('xl/workbook.xml').find('m:sheets', NS):
            rid = sheet.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']
            target = targets[rid]
            self.paths[sheet.attrib['name']] = (target.lstrip('/') if target.startswith('/')
                                                 else 'xl/' + target)

    def xml(self, path):
        return ET.fromstring(self.archive.read(path))

    def sheet(self, name):
        return self.xml(self.paths[name])

    def value(self, cell):
        value = cell.find('m:v', NS)
        if cell.attrib.get('t') == 's':
            return self.strings[int(value.text)]
        if cell.attrib.get('t') == 'inlineStr':
            return ''.join(cell.find('m:is', NS).itertext())
        return float(value.text) if value is not None else None

    def rows(self, name):
        return [[self.value(c) for c in row] for row in
                self.sheet(name).findall('m:sheetData/m:row', NS)]

    def text(self):
        return '\n'.join(self.strings)


def test_register_preserves_inputs_columns_and_numeric_totals():
    source = records()
    before = copy.deepcopy(source)
    report = sales_report(source)
    assert source == before
    assert report['sheet_name'] == 'Sales Register'
    assert [c['key'] for c in report['columns']] == COLUMNS
    assert all(c['kind'] == 'money' for c in report['columns'] if c['key'] in MONEY)
    by_id = {r['transaction_id']: r for r in report['rows']}
    assert set(by_id) == {'CASH-1', 'SPLIT-2', 'CARD-3'}
    assert by_id['CASH-1']['username'] == 'alice'
    assert by_id['SPLIT-2']['branch_name'] == 'Beta'
    assert by_id['SPLIT-2']['change_given'] == 20
    assert by_id['CASH-1']['total'] == 100.25
    assert by_id['SPLIT-2']['payment_breakdown']
    for key, expected in dict(total=210.75, tax=10.75,
                              cash_received=250.75, change_given=40).items():
        assert report['totals'][key] == expected


def test_identity_period_currency_and_generation_metadata():
    report = sales_report()
    assert report['brand']['name'] == 'Sales Test Brand'
    assert report['currency_suffix'] == 'MMK'
    meta = ' '.join(str(item['value']) for item in report['meta'])
    for expected in ('Alpha', 'report-manager', '2026-09-23',
                     '2026-09-21 to 2026-09-22', 'Payment: all'):
        assert expected in meta



def test_daily_summary_groups_days_without_mutating_report():
    report = sales_report()
    before = copy.deepcopy(report)
    daily = reports.build_sales_daily_summary_sheet(report)
    assert report == before
    assert daily['sheet_name'] == 'Daily Summary'
    rows = {r['date']: r for r in daily['rows']}
    assert set(rows) == {'2026-09-21', '2026-09-22'}
    for key, expected in dict(transactions=2, total=180.25, tax=9.25,
                              cash_received=220.25, change_given=40).items():
        assert rows['2026-09-21'][key] == expected
    assert rows['2026-09-22']['transactions'] == 1
    assert rows['2026-09-22']['total'] == 30.50
    assert daily['totals']['transactions'] == 3
    assert daily['totals']['total'] == 210.75


def test_payment_summary_allocates_sales_not_cash_tendered():
    report = sales_report()
    before = copy.deepcopy(report)
    payment = reports.build_sales_payment_summary_sheet(report)
    assert report == before
    assert payment['sheet_name'] == 'Payment Summary'
    rows = {r['method'].lower(): r for r in payment['rows']}
    assert set(rows) == {'cash', 'card'}
    assert rows['cash']['transactions'] == 2
    assert rows['cash']['amount'] == 160.25
    assert rows['card']['transactions'] == 2
    assert rows['card']['amount'] == 50.50
    assert payment['totals']['amount'] == 210.75


def test_decimal_cent_totals_and_nullable_cash_are_not_binary_sums():
    rows = [dict(records()[0], total=value, tax='0.01', cash_received=None,
                 refund_amount=None, payment_breakdown=None)
            for value in ('0.10', '0.20', '0.30')]
    report = sales_report(rows)
    from decimal import Decimal
    assert report['totals']['total'] == Decimal('0.60')
    assert report['totals']['tax'] == Decimal('0.03')
    assert report['totals']['cash_received'] == 0
    assert report['totals']['change_given'] == 0
    assert reports.build_sales_payment_summary_sheet(report)['totals']['amount'] == Decimal('0.60')



def test_builder_defaults_and_empty_summaries_are_renderable():
    report = reports.build_sales_report([])
    assert report['rows'] == []
    assert report['currency_suffix'] == '$'
    for key in MONEY:
        assert report['totals'][key] == 0
    extra = sheets(report)
    assert all(s['rows'] == [] for s in extra)
    workbook = Workbook(reports.build_report_xlsx(report, extra_sheets=extra))
    assert set(workbook.paths) == {'Sales Register', 'Daily Summary',
                                    'Payment Summary', 'Report Info'}
    for name in ('Sales Register', 'Daily Summary', 'Payment Summary'):
        xml = workbook.sheet(name)
        assert xml.find('m:sheetViews/m:sheetView/m:pane', NS) is not None
        assert all(int(row.attrib['r']) <= 4 for row in
                   xml.findall('m:sheetData/m:row', NS))
    assert reports.build_report_pdf(report).startswith(b'%PDF')


def test_workbook_numeric_styles_freeze_filters_and_literal_totals():
    report = sales_report()
    workbook = Workbook(reports.build_report_xlsx(report, extra_sheets=sheets(report)))
    styles = workbook.xml('xl/styles.xml')
    formats = {int(f.attrib['numFmtId']): f.attrib['formatCode'] for f in
               styles.findall('m:numFmts/m:numFmt', NS)}
    xfs = styles.find('m:cellXfs', NS)
    for name, count, last_col in [('Sales Register', 3, 'J'),
                                   ('Daily Summary', 2, 'F'),
                                   ('Payment Summary', 2, 'C')]:
        xml = workbook.sheet(name)
        pane = xml.find('m:sheetViews/m:sheetView/m:pane', NS)
        assert pane.attrib['state'] == 'frozen'
        assert pane.attrib['ySplit'] == '4'
        assert xml.find('m:autoFilter', NS).attrib['ref'] == f'A4:{last_col}{4 + count}'
        total_row = workbook.rows(name)[-1]
        assert total_row[0] == 'TOTAL'
        if name == 'Sales Register':
            assert total_row[6:10] == [210.75, 10.75, 250.75, 40.0]
            for cell in xml.findall('m:sheetData/m:row/m:c', NS):
                if cell.attrib['r'] in ('G5', 'H5', 'I5', 'J5'):
                    assert cell.attrib.get('t') not in ('s', 'inlineStr')
                    fmt = formats[int(xfs[int(cell.attrib['s'])].attrib['numFmtId'])]
                    assert '0.00' in fmt
                    assert 'MMK' in workbook.text()
    assert reports.build_report_pdf(report).startswith(b'%PDF')


@pytest.mark.parametrize('payload', ['=1+1', '+SUM(1,2)', '-1+2', '@SUM(1,2)',
                                      '=HYPERLINK("https://evil.invalid","click")',
                                      'https://evil.invalid/path', 'mailto:evil@example.test'])
def test_untrusted_text_is_never_formula_or_hyperlink_on_any_sheet(payload):
    row = records()[0]
    row.update(transaction_id=payload, username=payload, branch_name=payload,
               payment_method=payload, payment_breakdown={payload: 120.25})
    report = sales_report([row], brand={'name': payload}, branch_name=payload,
                          generated_by=payload, filters_text=payload)
    workbook = Workbook(reports.build_report_xlsx(report, extra_sheets=sheets(report)))
    for name in workbook.paths:
        xml = workbook.sheet(name)
        assert xml.findall('.//m:f', NS) == [], name
        assert xml.findall('.//m:hyperlink', NS) == [], name
    assert payload in workbook.text()
    assert payload in str(workbook.rows('Sales Register'))
    assert payload in str(workbook.rows('Payment Summary'))


@pytest.fixture(scope='module')
def backend(tmp_path_factory):
    # A separately named app and Flask instance isolate even from other test
    # modules collected in the same disposable run_tests.py checkout.
    from flask import Flask
    root = tmp_path_factory.mktemp('sales-reporting')
    original = Flask.__init__

    def isolated_init(self, *args, **kwargs):
        kwargs['instance_path'] = str(root)
        original(self, *args, **kwargs)

    name = '_isolated_sales_reporting_app'
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name('app.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    with patch.object(Flask, '__init__', isolated_init):
        spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    with module.app.app_context():
        assert Path(module.db.engine.url.database).parent == root
    yield module
    with module.app.app_context():
        module.db.session.remove()
        module.db.engine.dispose()
    sys.modules.pop(name, None)


@pytest.fixture
def world(backend):
    m = backend
    with m.app.app_context():
        m.db.session.remove()
        m.db.drop_all()
        m.db.create_all()
        branches = [m.Branch(name='Alpha', code='A', is_default=True),
                    m.Branch(name='Beta', code='B'),
                    m.Branch(name='Closed', code='C', is_active=False)]
        m.db.session.add_all(branches)
        m.db.session.flush()
        users = {role: m.User(username='sales-' + role, password='test-only',
                              role=role, branch_id=branches[0].id)
                 for role in ('cashier', 'manager', 'boss')}
        users['other'] = m.User(username='other-clerk', password='test-only',
                                role='cashier', branch_id=branches[0].id)
        m.db.session.add_all(users.values())
        m.db.session.flush()
        # Naive stored Yangon wall-clock, with both day boundaries and a
        # microsecond sale that an upper bound of 23:59:59 would lose.
        cases = [
            ('BEFORE', datetime(2026, 9, 20, 23, 59, 59, 999999), 0, 'cashier', 'cash'),
            ('MIDNIGHT', datetime(2026, 9, 21), 0, 'cashier', 'cash'),
            ('LAST-MICRO', datetime(2026, 9, 21, 23, 59, 59, 999999), 0, 'other', 'card'),
            ('AFTER', datetime(2026, 9, 22), 0, 'cashier', 'cash'),
            ('FOREIGN', datetime(2026, 9, 21, 12), 1, 'cashier', 'cash'),
        ]
        for txn, date, branch, role, method in cases:
            m.db.session.add(m.Sale(transaction_id=txn, date=date, total=10,
                                   tax=1, cash_received=12, refund_amount=2,
                                   payment_method=method,
                                   payment_breakdown=json.dumps({method: 12}),
                                   branch_id=branches[branch].id, user_id=users[role].id))
        m.db.session.commit()
        m.set_setting('currency_code', 'MMK')
        m.set_setting('receipt_brand_name', 'Installation Sales Brand')
        m.set_branch_setting('receipt_brand_name', 'Alpha Sales Brand', branches[0].id)
        m.set_branch_setting('receipt_email', 'alpha@example.test', branches[0].id)
        m.set_branch_setting('receipt_brand_name', 'Beta Sales Brand', branches[1].id)
        yield dict(m=m, branches=[b.id for b in branches],
                   users={k: u.id for k, u in users.items()})
        m.db.session.rollback()
        m.db.session.remove()


def client(world, role='manager', branch=0):
    result = world['m'].app.test_client()
    if role is not None:
        with result.session_transaction() as session:
            session.update(user_id=world['users'][role], username='sales-' + role,
                           role=role, branch_id=world['branches'][branch])
    return result


API = '/api/reports/sales'
EXPORT = API + '/export'
DAY = dict(start='2026-09-21', end='2026-09-21')


def api_ids(response):
    assert response.status_code == 200, response.get_data(as_text=True)
    data = response.get_json()
    return {r['transaction_id'] for r in (data['items'] if isinstance(data, dict) else data)}


def export_ids(response):
    assert response.status_code == 200, repr(response.data[:500])
    workbook = Workbook(response.data)
    return {row[0] for row in workbook.rows('Sales Register')[3:]
            if row and row[0] != 'TOTAL'}


@pytest.mark.parametrize('role,status', [(None, 403), ('cashier', 403),
                                         ('manager', 200), ('boss', 200)])
def test_export_authentication_and_manager_gate(world, role, status):
    response = client(world, role).get(EXPORT, query_string={'format': 'xlsx'})
    assert response.status_code == status


@pytest.mark.parametrize('endpoint', [API, EXPORT])
@pytest.mark.parametrize('bounds', [
    {'start': 'not-a-date'}, {'end': '2026-02-30'},
    {'start': '2026-9-21'}, {'end': '2026-09-1'},
    {'start': '20260921'}, {'end': '2026-09-21T12:00:00'},
    {'start': '2026-09-22', 'end': '2026-09-21'},
])
def test_strict_invalid_and_reversed_dates_are_400(world, endpoint, bounds):
    response = client(world).get(endpoint, query_string=dict(format='xlsx', **bounds))
    assert response.status_code == 400
    assert response.is_json


@pytest.mark.parametrize('paginated', [False, True])
def test_api_inclusive_microsecond_day_uses_stored_yangon_clock(world, paginated):
    params = dict(DAY)
    if paginated:
        params.update(page=1, per_page=20)
    assert api_ids(client(world).get(API, query_string=params)) == {'MIDNIGHT', 'LAST-MICRO'}


def test_export_inclusive_microsecond_day_matches_api(world):
    response = client(world).get(EXPORT, query_string=dict(format='xlsx', **DAY))
    assert export_ids(response) == {'MIDNIGHT', 'LAST-MICRO'}


@pytest.mark.parametrize('role', ['manager', 'boss'])
@pytest.mark.parametrize('scope', ['current', 'branch', 'all'])
def test_export_and_api_respect_existing_manager_scope(world, role, scope):
    params = dict(DAY, scope=scope, format='xlsx')
    if scope == 'branch':
        params['report_branch_id'] = world['branches'][1]
    expected = ({'FOREIGN'} if scope == 'branch' else
                {'MIDNIGHT', 'LAST-MICRO', 'FOREIGN'} if scope == 'all' else
                {'MIDNIGHT', 'LAST-MICRO'})
    c = client(world, role)
    assert api_ids(c.get(API, query_string=params)) == expected
    assert export_ids(c.get(EXPORT, query_string=params)) == expected


@pytest.mark.parametrize('target', ['invalid', 'inactive', 'missing'])
def test_invalid_explicit_branch_keeps_existing_current_branch_fallback(world, target):
    branch_id = {'invalid': 'bad', 'inactive': world['branches'][2],
                 'missing': 999999}[target]
    params = dict(DAY, scope='branch', report_branch_id=branch_id, format='xlsx')
    c = client(world)
    assert api_ids(c.get(API, query_string=params)) == {'MIDNIGHT', 'LAST-MICRO'}
    assert export_ids(c.get(EXPORT, query_string=params)) == {'MIDNIGHT', 'LAST-MICRO'}


def test_cashier_list_cannot_widen_to_other_users_or_branches(world):
    c = client(world, 'cashier')
    assert api_ids(c.get(API, query_string=dict(DAY, scope='all'))) == {'MIDNIGHT'}
    assert c.get(EXPORT, query_string=dict(DAY, scope='all')).status_code == 403


@pytest.mark.parametrize('query,expected', [
    ('last-micro', {'LAST-MICRO'}), ('CARD', {'LAST-MICRO'}),
    ('other-clerk', {'LAST-MICRO'}), ('no-match-here', set()),
])
def test_export_search_matches_api_transaction_payment_and_username(world, query, expected):
    params = dict(DAY, q=query, format='xlsx')
    c = client(world)
    assert api_ids(c.get(API, query_string=params)) == expected
    assert export_ids(c.get(EXPORT, query_string=params)) == expected


@pytest.mark.parametrize('scope,brand', [('current', 'Alpha Sales Brand'),
                                         ('branch', 'Beta Sales Brand')])
def test_export_workbook_identity_period_and_currency(world, scope, brand):
    params = dict(DAY, format='xlsx', scope=scope)
    if scope == 'branch':
        params['report_branch_id'] = world['branches'][1]
    response = client(world).get(EXPORT, query_string=params)
    assert response.status_code == 200
    assert response.headers['Content-Type'] == reports.XLSX_CONTENT_TYPE
    disposition = response.headers['Content-Disposition']
    assert disposition.startswith('attachment;') and '.xlsx' in disposition
    workbook = Workbook(response.data)
    assert set(workbook.paths) == {'Sales Register', 'Daily Summary',
                                    'Payment Summary', 'Report Info'}
    text = workbook.text()
    for expected in (brand, 'sales-manager', '2026-09-21', 'MMK'):
        assert expected in text
    assert ('Beta Sales Brand' if scope == 'current' else 'Alpha Sales Brand') not in text


def test_export_remains_xlsx_and_empty_workbook(world):
    # Sales export remains a workbook endpoint, even for an unknown format.
    c = client(world)
    response = c.get(EXPORT, query_string=dict(DAY, format='pdf'))
    assert response.status_code == 200
    assert response.headers['Content-Type'] == reports.XLSX_CONTENT_TYPE
    assert response.data.startswith(b'PK')
    assert response.headers['Content-Disposition'].startswith('attachment;')
    empty = c.get(EXPORT, query_string=dict(start='2027-01-01', end='2027-01-01', format='xlsx'))
    assert export_ids(empty) == set()
    workbook = Workbook(empty.data)
    for name in ('Sales Register', 'Daily Summary', 'Payment Summary'):
        assert all(int(row.attrib['r']) <= 4 for row in
                   workbook.sheet(name).findall('m:sheetData/m:row', NS))
