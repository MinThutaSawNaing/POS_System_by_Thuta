"""Report delivery regressions: report attachments, sending, and schedules.

Run with ``python run_tests.py test_report_delivery.py -q --tb=short``.
No workspace ``app`` import occurs (instance/uploads are redirected to a temp
directory) and the SMTP boundary is faked, so nothing can leave the machine.
"""

import importlib.util
from pathlib import Path
import shutil
import smtplib
import sqlite3
import sys
import time
from unittest.mock import patch

from flask import Flask
import pytest

import reports
import report_scheduler


PASSWORD = 'report-password-123!'
API_KEY = 're_report_delivery_secret'
FROM_EMAIL = 'POS Reports <reports@example.com>'
MANAGER_EMAIL = 'manager@example.com'
CASHIER_EMAIL = 'cashier@example.com'


@pytest.fixture(scope='module')
def delivery_app(tmp_path_factory):
    root = tmp_path_factory.mktemp('report-delivery')
    (root / 'instance').mkdir()
    shutil.copytree(Path(__file__).parent / 'templates', root / 'templates')
    original_init = Flask.__init__

    def isolated_init(application, *args, **kwargs):
        kwargs.update(instance_path=str(root / 'instance'), root_path=str(root))
        original_init(application, *args, **kwargs)

    name = '_isolated_report_delivery_app'
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name('app.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        with patch.object(Flask, '__init__', isolated_init):
            spec.loader.exec_module(module)
        module.app.config.update(TESTING=True, SECRET_KEY='isolated-report-delivery-only')
        with module.app.app_context():
            assert Path(module.db.engine.url.database).resolve().is_relative_to(root)
            module.db.drop_all()
            module.db.create_all()
        yield module
    finally:
        if hasattr(module, 'db'):
            with module.app.app_context():
                module.db.session.remove()
                module.db.engine.dispose()
        sys.modules.pop(name, None)


@pytest.fixture
def world(delivery_app, monkeypatch):
    """Fresh data, fake SMTP transport and configured Resend settings."""
    m = delivery_app
    deliveries = []

    class SMTP:
        def __init__(self, host, port, **kwargs):
            self.record = {'host': host, 'port': port, 'messages': [], **kwargs}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            return 250, b'OK'

        def starttls(self, *, context):
            return 220, b'OK'

        def login(self, username, password):
            self.record['username'] = username
            return 235, b'OK'

        def send_message(self, message):
            self.record['messages'].append(message)
            deliveries.append(self.record)
            return {}

    class SMTP_SSL(SMTP):
        pass

    monkeypatch.setattr(smtplib, 'SMTP', SMTP)
    monkeypatch.setattr(smtplib, 'SMTP_SSL', SMTP_SSL)
    for name in ('RESEND_API_KEY', 'RESEND_FROM_EMAIL', 'RESEND_SMTP_HOST',
                 'RESEND_SMTP_PORT', 'RESEND_SMTP_USERNAME'):
        monkeypatch.delenv(name, raising=False)

    with m.app.app_context():
        m.db.session.remove()
        m.db.drop_all()
        m.db.create_all()
        branch = m.Branch(name='Alpha', code='RPT-A', is_active=True, is_default=True)
        other = m.Branch(name='Beta', code='RPT-B', is_active=True)
        m.db.session.add_all([branch, other])
        m.db.session.flush()
        manager = m.User(username='manager', role='manager', branch_id=branch.id,
                         password='x', email=MANAGER_EMAIL)
        cashier = m.User(username='cashier', role='cashier', branch_id=branch.id,
                         password='x', email=CASHIER_EMAIL)
        m.db.session.add_all([manager, cashier])
        m.db.session.flush()
        m.db.session.add_all([
            m.Product(name='Cola', barcode='C1', price=2.0, cost=1.0, stock=2,
                      reorder_point=10, reorder_quantity=48, reorder_enabled=True,
                      branch_id=branch.id, category='Drinks'),
            m.Product(name='Bread', barcode='B1', price=3.0, cost=1.5, stock=40,
                      reorder_point=10, reorder_quantity=20, reorder_enabled=True,
                      branch_id=branch.id, category='Bakery'),
        ])
        # Report delivery writes settings through the same helper the app uses.
        for key, value in (('resend_api_key', API_KEY), ('email_from', FROM_EMAIL),
                           ('email_smtp_host', 'smtp.resend.com'),
                           ('email_smtp_port', '465'),
                           ('email_smtp_username', 'resend')):
            m.set_setting(key, value)
        m.db.session.commit()
        ids = {'branch': branch.id, 'other_branch': other.id,
               'manager': manager.id, 'cashier': cashier.id}

    class World:
        pass

    w = World()
    w.m, w.app, w.ids, w.deliveries = m, m.app, ids, deliveries
    w.service = m.report_scheduler
    w.manager = {'user_id': ids['manager'], 'role': 'manager', 'branch_id': ids['branch']}
    w.cashier = {'user_id': ids['cashier'], 'role': 'cashier', 'branch_id': ids['branch']}
    w.attachments = lambda: [part for message in deliveries[-1]['messages']
                             for part in message.walk()
                             if part.get_content_disposition() == 'attachment']
    yield w
    with m.app.app_context():
        m.db.session.remove()
        m.db.drop_all()


def test_low_stock_report_lists_only_products_at_or_below_reorder_point(world):
    with world.app.app_context():
        records = report_scheduler.low_stock_records(world.m.Product, world.ids['branch'])
        report = reports.build_low_stock_report(records, branch_name='Alpha')
    assert [row['product_name'] for row in report['rows']] == ['Cola']
    row = report['rows'][0]
    assert (row['current_stock'], row['reorder_point'], row['suggested_qty']) == (2, 10, 48)
    assert report['summary'][0] == {'label': 'Products to reorder', 'value': '1'}


def test_report_renders_real_excel_and_pdf_bytes(world):
    with world.app.app_context():
        records = report_scheduler.low_stock_records(world.m.Product, world.ids['branch'])
        report = reports.build_low_stock_report(records, branch_name='Alpha')
    files = report_scheduler.render_attachments(report, 'both', stamp='20260302')
    assert [name for name, *_ in files] == ['low_stock_report_20260302.xlsx',
                                            'low_stock_report_20260302.pdf']
    assert files[0][1][:2] == b'PK'
    assert files[1][1][:4] == b'%PDF'


def test_send_now_emails_the_requester_with_an_excel_attachment(world):
    with world.app.app_context():
        result = world.service.send_now(world.manager, 'low_stock')
    assert result['status'] == 'sent'
    assert result['recipients'] == [MANAGER_EMAIL]
    assert result['attachments'][0].endswith('.xlsx')
    assert result['rows'] == 1
    message = world.deliveries[0]['messages'][0]
    assert message['To'] == MANAGER_EMAIL
    assert 'Low stock report' in message['Subject']
    assert 'Products to reorder' in message.get_body(preferencelist=('plain',)).get_content()
    attachment = world.attachments()[0]
    assert attachment.get_filename().endswith('.xlsx')
    assert attachment.get_payload(decode=True)[:2] == b'PK'


def test_cashier_can_only_address_themselves(world):
    with world.app.app_context():
        with pytest.raises(report_scheduler.ReportDeliveryError) as failure:
            world.service.send_now(world.cashier, 'low_stock', recipient='managers')
        assert 'only a manager' in str(failure.value).lower()
        assert world.deliveries == []
        own = world.service.send_now(world.cashier, 'low_stock')
    assert own['recipients'] == [CASHIER_EMAIL]


def test_schedule_fires_at_store_time_once_per_day(world):
    ScheduledReport = world.service.models['ScheduledReport']
    with world.app.app_context():
        task = world.service.create(world.manager, 'low_stock', send_time='09:00')['task']
        assert task['schedule'] == 'every day at 09:00'
        assert task['next_run'].endswith('09:00')
        row = ScheduledReport.query.filter_by(id=task['task_id']).first()
        due = row.next_run_at
        assert world.service.run_due(now=due)[0]['status'] == 'sent'
        assert len(world.deliveries) == 1
        # Same schedule must not fire twice, and must move to the next day.
        assert world.service.run_due(now=due + 30) == []
        assert len(world.deliveries) == 1
        row = ScheduledReport.query.filter_by(id=task['task_id']).first()
        assert row.last_status == 'sent'
        assert row.next_run_at == pytest.approx(due + 86400)



def test_missed_run_is_recorded_without_sending_a_stale_report(world):
    ScheduledReport = world.service.models['ScheduledReport']
    with world.app.app_context():
        task = world.service.create(world.manager, 'low_stock', send_time='09:00')['task']
        row = ScheduledReport.query.filter_by(id=task['task_id']).first()
        late = row.next_run_at + report_scheduler.CATCH_UP_SECONDS + 60
        results = world.service.run_due(now=late)
    assert results[0]['status'] == 'missed'
    assert world.deliveries == []


def test_unlinked_requester_switches_the_schedule_off(world):
    ScheduledReport = world.service.models['ScheduledReport']
    with world.app.app_context():
        task = world.service.create(world.manager, 'low_stock', send_time='09:00')['task']
        user = world.m.User.query.filter_by(id=world.ids['manager']).first()
        user.email = None
        world.m.db.session.commit()
        row = ScheduledReport.query.filter_by(id=task['task_id']).first()
        results = world.service.run_due(now=row.next_run_at)
        row = ScheduledReport.query.filter_by(id=task['task_id']).first()
        assert results[0]['status'] == 'disabled'
        assert row.enabled is False and row.next_run_at is None
        user = world.m.User.query.filter_by(id=world.ids['manager']).first()
        user.email = MANAGER_EMAIL
        world.m.db.session.commit()
    assert world.deliveries == []


def test_update_reschedules_and_cancel_stops_delivery(world):
    with world.app.app_context():
        task = world.service.create(world.manager, 'low_stock', send_time='09:00')['task']
        updated = world.service.update(world.manager, task['task_id'], send_time='07:30')
        assert updated['task']['schedule'] == 'every day at 07:30'
        assert updated['task']['next_run'].endswith('07:30')
        assert world.service.cancel(world.manager, task['task_id'])['task']['enabled'] is False
        assert world.service.run_due(now=time.time() + 2 * 86400) == []
    assert world.deliveries == []


def test_weekly_pdf_schedule_is_listed_in_plain_language(world):
    with world.app.app_context():
        world.service.create(world.manager, 'inventory', send_time='18:00',
                             delivery_format='pdf', frequency='weekly', weekday='friday')
        listing = world.service.list_tasks(world.manager)
        sent = world.service.send_now(world.manager, 'low_stock', delivery_format='pdf')
    assert listing['count'] == 1
    task = listing['tasks'][0]
    assert task['schedule'] == 'every Friday at 18:00'
    assert task['format'] == 'pdf'
    assert sent['attachments'][0].endswith('.pdf')
    assert world.attachments()[0].get_content_type() == 'application/pdf'


def test_unconfigured_email_reports_a_clear_error_and_sends_nothing(world):
    with world.app.app_context():
        world.m.set_setting('resend_api_key', '')
        with pytest.raises(report_scheduler.ReportDeliveryError) as failure:
            world.service.send_now(world.manager, 'low_stock')
        assert 'not configured' in str(failure.value).lower()
        world.m.set_setting('resend_api_key', API_KEY)
    assert world.deliveries == []


def test_schedule_tables_exist_after_startup(world):
    """The new tables come from the app's own create_all, not a migration."""
    with world.app.app_context():
        database = world.m.db.engine.url.database
    with sqlite3.connect(database) as connection:
        names = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'scheduled_report', 'scheduled_report_run'} <= names



def test_send_test_email_reaches_the_given_address_without_attachments(world):
    with world.app.app_context():
        result = world.service.send_test(world.manager, 'qa@example.com', note='Check SMTP')
    assert result['status'] == 'sent'
    assert result['recipients'] == ['qa@example.com']
    assert result['attachments'] == []
    message = world.deliveries[0]['messages'][0]
    assert message['To'] == 'qa@example.com'
    assert 'Check SMTP' in message.get_body(preferencelist=('plain',)).get_content()
    assert world.attachments() == []


def test_send_test_email_rejects_an_invalid_address(world):
    with world.app.app_context():
        with pytest.raises(report_scheduler.ReportDeliveryError):
            world.service.send_test(world.manager, 'not-an-address')
    assert world.deliveries == []

