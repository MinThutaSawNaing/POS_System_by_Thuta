"""Report delivery for Loli: send a report now, or on a daily/weekly schedule.

Design notes
------------
* The transport is the existing Resend SMTP relay (``email_auth.send_mail``), so
  a store that already sends sign-in codes needs no extra configuration.
* Schedules live in SQLite, so they survive restarts. One daemon poller inside
  the web process claims due rows with ``BEGIN IMMEDIATE`` — the same
  serialization ``email_auth`` uses to survive concurrent workers.
* A job remembers who created it (user, branch, role) and re-validates that
  identity before every send: a deactivated account, an inactive branch or an
  unlinked mailbox disables the job instead of quietly mailing stale data.
* The model never supplies an attachment or a raw address list: it names a
  report and a recipient selector, and this module renders the file and
  resolves the addresses from user records.
"""

import datetime as dt
import json
import os
import re
import sys
import threading
import time
from types import SimpleNamespace

import pytz
from sqlalchemy import text

import email_auth
import reports


BUSINESS_TIMEZONE = pytz.timezone('Asia/Yangon')
DEFAULT_SEND_TIME = '09:00'
DEFAULT_FORMAT = 'xlsx'
POLL_SECONDS = 30
CATCH_UP_SECONDS = 6 * 3600
STALE_CLAIM_SECONDS = 15 * 60
MAX_TASKS_PER_USER = 12
FREQUENCIES = ('daily', 'weekly')
FORMATS = ('xlsx', 'pdf', 'both')
WEEKDAYS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')
RECIPIENT_SELECTORS = ('me', 'managers', 'boss', 'staff', 'everyone', 'custom')

REPORT_KINDS = {
    'low_stock': {
        'label': 'Low stock',
        'file_stem': 'low_stock_report',
        'subject': 'Low stock report',
        'description': 'Products at or below their reorder point with suggested quantities.',
    },
    'inventory': {
        'label': 'Inventory list',
        'file_stem': 'product_list',
        'subject': 'Inventory report',
        'description': 'Every product in the branch with price, stock and tax.',
    },
    'sales_summary': {
        'label': 'Sales summary',
        'file_stem': 'sales_report',
        'subject': 'Sales report',
        'description': 'Sales register for the recent period.',
    },
}


class ReportDeliveryError(Exception):
    """A delivery problem that is safe to show the user verbatim."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


# ---------------------------------------------------------------------------
# Report data collection
# ---------------------------------------------------------------------------

def low_stock_records(Product, branch_id):
    """Reorder list in the shape ``reports.build_low_stock_report`` expects."""
    items = []
    for product in Product.query.filter_by(branch_id=branch_id, reorder_enabled=True).all():
        current = int(product.stock or 0)
        point = max(int(product.reorder_point or 0), 0)
        if current > point:
            continue
        suggested = max(int(product.reorder_quantity or 0), 0) or max(point - current, 1)
        items.append({
            'name': product.name,
            'barcode': product.barcode,
            'category': product.category,
            'current_stock': current,
            'reorder_point': point,
            'suggested_reorder_qty': suggested,
            'unit_cost': float(product.cost or 0),
            'estimated_cost': suggested * float(product.cost or 0),
        })
    return items


def inventory_records(Product, branch_id):
    return [{
        'barcode': product.barcode,
        'product_name': product.name,
        'category': product.category,
        'price': float(product.price or 0),
        'stock': int(product.stock or 0),
        'unit': getattr(product, 'unit', None),
        'tax_rate': float(getattr(product, 'tax_rate', 0) or 0),
    } for product in Product.query.filter_by(branch_id=branch_id).order_by(Product.name).all()]


def sales_records(Sale, User, Branch, branch_id, days=7):
    """Sales register rows for the recent window, in report-builder shape."""
    days = max(1, min(int(days or 7), 365))
    since = dt.datetime.utcnow() - dt.timedelta(days=days)
    branch = Branch.query.filter_by(id=branch_id).first()
    branch_name = branch.name if branch else ''
    rows = []
    for sale in (Sale.query.filter(Sale.branch_id == branch_id, Sale.date >= since)
                 .order_by(Sale.date).all()):
        breakdown = {}
        if sale.payment_breakdown:
            try:
                parsed = json.loads(sale.payment_breakdown)
                breakdown = parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                breakdown = {}
        cashier = User.query.filter_by(id=sale.user_id).first()
        rows.append({
            'transaction_id': sale.transaction_id,
            'date': sale.date,
            'total': sale.total,
            'tax': sale.tax,
            'cash_received': sale.cash_received,
            'refund_amount': sale.refund_amount,
            'payment_method': sale.payment_method,
            'payment_breakdown': breakdown,
            'username': cashier.username if cashier else '',
            'branch_name': branch_name,
        })
    return rows


def build_report(kind, models, *, branch_id, branch_name='', generated_by='',
                 currency_suffix='$', params=None, brand=None):
    """Render the named report dict for a branch. Raises on an unknown kind."""
    params = dict(params or {})
    if kind not in REPORT_KINDS:
        raise ReportDeliveryError(
            f"Unknown report '{kind}'. Choose one of: {', '.join(sorted(REPORT_KINDS))}.")
    if kind == 'low_stock':
        return reports.build_low_stock_report(
            low_stock_records(models.Product, branch_id), brand=brand,
            branch_name=branch_name, generated_by=generated_by,
            currency_suffix=currency_suffix)
    if kind == 'inventory':
        return reports.build_product_catalog_report(
            inventory_records(models.Product, branch_id), brand=brand,
            branch_name=branch_name, generated_by=generated_by,
            currency_suffix=currency_suffix)
    days = max(1, min(int(params.get('days') or 7), 365))
    return reports.build_sales_report(
        sales_records(models.Sale, models.User, models.Branch, branch_id, days=days),
        brand=brand, branch_name=branch_name, generated_by=generated_by,
        currency_suffix=currency_suffix, period_text=f'Last {days} days')


XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def render_attachments(report, delivery_format, stamp=None):
    """File attachments for one report; ``both`` sends Excel and PDF."""
    delivery_format = str(delivery_format or DEFAULT_FORMAT).lower()
    if delivery_format not in FORMATS:
        raise ReportDeliveryError(f"format must be one of: {', '.join(FORMATS)}.")
    stamp = stamp or dt.datetime.now(BUSINESS_TIMEZONE).strftime('%Y%m%d')
    stem = f"{report.get('file_stem') or 'report'}_{stamp}"
    files = []
    if delivery_format in ('xlsx', 'both'):
        files.append((f'{stem}.xlsx', reports.build_report_xlsx(report), XLSX_MIME))
    if delivery_format in ('pdf', 'both'):
        files.append((f'{stem}.pdf', reports.build_report_pdf(report), 'application/pdf'))
    return files



# ---------------------------------------------------------------------------
# Schedule math (Asia/Yangon wall clock, stored as UTC epoch seconds)
# ---------------------------------------------------------------------------

def parse_send_time(value):
    """'9:00' / '09:00' -> (9, 0). Raises for anything that is not a wall clock."""
    match = re.fullmatch(r'\s*([01]?\d|2[0-3])\s*:\s*([0-5]\d)\s*', str(value or ''))
    if not match:
        raise ReportDeliveryError("send_time must look like 09:00 (24-hour clock).")
    return int(match.group(1)), int(match.group(2))


def normalize_frequency(value):
    text_value = str(value or 'daily').strip().lower()
    aliases = {'every day': 'daily', 'everyday': 'daily', 'day': 'daily',
               'every week': 'weekly', 'week': 'weekly'}
    text_value = aliases.get(text_value, text_value)
    if text_value not in FREQUENCIES:
        raise ReportDeliveryError(f"frequency must be one of: {', '.join(FREQUENCIES)}.")
    return text_value


def normalize_weekday(value):
    text_value = str(value or '').strip().lower()
    if not text_value:
        return ''
    if text_value not in WEEKDAYS:
        raise ReportDeliveryError(f"weekday must be one of: {', '.join(WEEKDAYS)}.")
    return text_value


def next_run_epoch(send_time, frequency='daily', weekday='', after=None, timezone_name=None):
    """Next delivery instant as UTC epoch seconds, strictly after ``after``."""
    zone = pytz.timezone(timezone_name or BUSINESS_TIMEZONE.zone)
    hour, minute = parse_send_time(send_time)
    frequency = normalize_frequency(frequency)
    weekday = normalize_weekday(weekday)
    anchor = dt.datetime.fromtimestamp(time.time() if after is None else float(after), zone)
    candidate = anchor.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if frequency == 'weekly':
        target = WEEKDAYS.index(weekday or 'monday')
        candidate += dt.timedelta(days=(target - candidate.weekday()) % 7)
    while candidate <= anchor:
        candidate += dt.timedelta(days=7 if frequency == 'weekly' else 1)
    return candidate.timestamp()


def describe_schedule(frequency, send_time, weekday=''):
    frequency = normalize_frequency(frequency)
    if frequency == 'weekly':
        return f"every {str(weekday or 'monday').title()} at {send_time}"
    return f"every day at {send_time}"


def local_label(epoch, timezone_name=None):
    zone = pytz.timezone(timezone_name or BUSINESS_TIMEZONE.zone)
    return dt.datetime.fromtimestamp(float(epoch), zone).strftime('%Y-%m-%d %H:%M')



# ---------------------------------------------------------------------------
# Recipients and message bodies
# ---------------------------------------------------------------------------

def resolve_recipients(selector, User, *, actor_user_id, branch_id, custom=None):
    """Turn a selector into real addresses.

    The model names a selector; this function decides which addresses that means
    from live user records, so a prompt can never type in an outsider's address.
    """
    selector = str(selector or 'me').strip().lower()
    if selector not in RECIPIENT_SELECTORS:
        raise ReportDeliveryError(
            f"recipient must be one of: {', '.join(RECIPIENT_SELECTORS)}.")
    if selector == 'custom':
        addresses = []
        for value in ([custom] if isinstance(custom, str) else list(custom or [])):
            try:
                addresses.append(email_auth.normalize_email(value))
            except ValueError:
                raise ReportDeliveryError(f"'{value}' is not a valid email address.") from None
        if not addresses:
            raise ReportDeliveryError("A custom recipient needs at least one email address.")
        return sorted(set(addresses))

    def with_email(query):
        return [user for user in query.all() if user.email]

    if selector == 'me':
        user = User.query.filter_by(id=actor_user_id).first()
        if not user:
            raise ReportDeliveryError("Your account could not be found.", 403)
        if not user.email:
            raise ReportDeliveryError(
                "Your account has no email address yet. Link one in Users -> Add email first.")
        return [user.email]
    if selector == 'everyone':
        users = with_email(User.query.filter_by(branch_id=branch_id))
    elif selector == 'boss':
        users = with_email(User.query.filter_by(role='boss'))
    elif selector == 'managers':
        users = with_email(User.query.filter_by(branch_id=branch_id, role='manager'))
    else:
        users = with_email(User.query.filter(User.branch_id == branch_id,
                                             User.role.in_({'cashier', 'staff'})))
    addresses = sorted({user.email for user in users})
    if not addresses:
        raise ReportDeliveryError(f"No account with a linked email matches '{selector}'.")
    return addresses


def _escape_html(value):
    return (str(value).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def report_email(report, attachments, *, note='', schedule_text='', brand_name=''):
    """Plain-text and HTML bodies that wrap the generated attachment."""
    title = str(report.get('title') or 'Report')
    summaries = report.get('summary') or []
    lines = [title]
    if brand_name:
        lines.append(brand_name)
    lines.append('')
    lines.append(f"Rows: {len(report.get('rows') or []):,}")
    lines += [f"{item.get('label')}: {item.get('value')}" for item in summaries]
    if note:
        lines += ['', note]
    if attachments:
        lines += ['', 'Attached: ' + ', '.join(name for name, *_rest in attachments)]
    if schedule_text:
        lines += ['', f"Schedule: {schedule_text}"]
    lines += ['', 'Sent automatically by Loli, the POS assistant. Replies are not monitored.']
    text = '\n'.join(lines)

    cards = ''.join(
        f'<td style="padding:8px 14px;border:1px solid #e0e0e0;">'
        f'<div style="color:#555;font-size:12px;">{_escape_html(item.get("label"))}</div>'
        f'<div style="color:#111;font-size:16px;font-weight:700;">{_escape_html(item.get("value"))}</div>'
        f'</td>' for item in summaries)
    files = ''.join(f'<li>{_escape_html(name)}</li>' for name, *_rest in attachments)
    html = (
        '<!DOCTYPE html><html lang="en"><body style="margin:0;padding:0;background-color:#f4f4f4;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="padding:24px 12px;">'
        '<tr><td align="center"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="max-width:560px;background-color:#ffffff;border:1px solid #e0e0e0;border-radius:8px;'
        'font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;">'
        '<tr><td bgcolor="#111" style="background-color:#111;padding:18px 24px;color:#ffffff;'
        'font-size:15px;font-weight:700;letter-spacing:.06em;">'
        + _escape_html(brand_name or 'PARROT POS') + '</td></tr>'
        '<tr><td style="padding:24px;">'
        f'<h1 style="margin:0 0 4px;font-size:20px;color:#111;">{_escape_html(title)}</h1>'
        f'<p style="margin:0 0 18px;color:#555;font-size:14px;">'
        f'{_escape_html(report.get("subtitle") or "")}</p>'
        f'<table role="presentation" cellpadding="0" cellspacing="0"><tr>{cards}</tr></table>'
        + (f'<p style="margin:18px 0 0;color:#333;font-size:14px;">{_escape_html(note)}</p>'
           if note else '')
        + (f'<p style="margin:18px 0 0;color:#333;font-size:14px;">Attached:<ul>{files}</ul></p>'
           if files else '')
        + (f'<p style="margin:14px 0 0;color:#555;font-size:13px;">Schedule: '
           f'{_escape_html(schedule_text)}</p>' if schedule_text else '')
        + '</td></tr><tr><td style="background-color:#f8fafc;border-top:1px solid #e0e0e0;'
          'padding:14px 24px;color:#6f6f6f;font-size:12px;">Sent automatically by Loli, the POS '
          'assistant. Replies are not monitored.</td></tr></table></td></tr></table></body></html>')
    return text, html




class _DisableJob(Exception):
    """A schedule that can no longer run safely; the job switches itself off."""


_scheduler_thread = None
_scheduler_lock = threading.Lock()
_scheduler_stop = threading.Event()


def _should_autostart(app):
    if os.environ.get('AI_SCHEDULER_ENABLED', '1').strip().lower() in ('0', 'false', 'off', 'no'):
        return False
    if 'pytest' in sys.modules or app.config.get('TESTING'):
        return False
    return True


def register_report_scheduler(app, db, pos):
    """Create the schedule tables and return the API the AI tools call."""

    class ScheduledReport(db.Model):
        __tablename__ = 'scheduled_report'

        id = db.Column(db.Integer, primary_key=True)
        user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
        branch_id = db.Column(db.Integer, nullable=False, index=True)
        username = db.Column(db.String(80), nullable=False, default='')
        role = db.Column(db.String(20), nullable=False, default='cashier')
        report_kind = db.Column(db.String(40), nullable=False)
        params_json = db.Column(db.Text, nullable=True)
        delivery_format = db.Column(db.String(10), nullable=False, default=DEFAULT_FORMAT)
        recipient = db.Column(db.String(20), nullable=False, default='me')
        custom_recipients = db.Column(db.String(500), nullable=True)
        note = db.Column(db.String(500), nullable=True)
        send_time = db.Column(db.String(5), nullable=False, default=DEFAULT_SEND_TIME)
        frequency = db.Column(db.String(10), nullable=False, default='daily')
        weekday = db.Column(db.String(10), nullable=True)
        timezone = db.Column(db.String(40), nullable=False, default=BUSINESS_TIMEZONE.zone)
        notify_when_empty = db.Column(db.Boolean, nullable=False, default=True)
        enabled = db.Column(db.Boolean, nullable=False, default=True)
        next_run_at = db.Column(db.Float, nullable=True, index=True)
        last_run_at = db.Column(db.Float, nullable=True)
        last_status = db.Column(db.String(20), nullable=True)
        last_error = db.Column(db.String(300), nullable=True)
        created_at = db.Column(db.DateTime, default=dt.datetime.utcnow, nullable=False)
        updated_at = db.Column(db.DateTime, default=dt.datetime.utcnow,
                               onupdate=dt.datetime.utcnow, nullable=False)

        def to_dict(self):
            return {
                'task_id': self.id,
                'user_id': self.user_id,
                'username': self.username,
                'role': self.role,
                'branch_id': self.branch_id,
                'report': self.report_kind,
                'format': self.delivery_format,
                'recipient': self.recipient,
                'custom_recipients': _loads(self.custom_recipients, []),
                'note': self.note or '',
                'send_time': self.send_time,
                'frequency': self.frequency,
                'weekday': self.weekday or '',
                'timezone': self.timezone,
                'notify_when_empty': bool(self.notify_when_empty),
                'enabled': bool(self.enabled),
                'schedule': describe_schedule(self.frequency, self.send_time, self.weekday or ''),
                'next_run': local_label(self.next_run_at, self.timezone) if self.next_run_at else '',
                'last_run': local_label(self.last_run_at, self.timezone) if self.last_run_at else '',
                'last_status': self.last_status or '',
                'last_error': self.last_error or '',
                'params': _loads(self.params_json, {}),
            }

    class ScheduledReportRun(db.Model):
        __tablename__ = 'scheduled_report_run'

        id = db.Column(db.Integer, primary_key=True)
        task_id = db.Column(db.Integer, nullable=False, index=True)
        started_at = db.Column(db.Float, nullable=False)
        finished_at = db.Column(db.Float, nullable=True)
        status = db.Column(db.String(20), nullable=False, default='running')
        recipients = db.Column(db.String(500), nullable=True)
        attachments = db.Column(db.String(500), nullable=True)
        error = db.Column(db.String(300), nullable=True)

    Models = SimpleNamespace(User=pos.get('User'), Branch=pos.get('Branch'),
                             Product=pos.get('Product'), Sale=pos.get('Sale'))

    def _loads(raw, fallback):
        try:
            value = json.loads(raw) if raw else fallback
        except (TypeError, ValueError):
            return fallback
        return value

    def _dumps(value):
        return json.dumps(value, separators=(',', ':'), sort_keys=True)

    def _setting(key, default=None):
        try:
            return pos['get_setting'](key, default)
        except Exception:
            return default

    def _brand():
        return {'name': _setting('pos_name') or 'Parrot POS'}

    def _currency():
        try:
            return pos['get_currency_suffix']()
        except Exception:
            return '$'

    def _branch_name(branch_id):
        if not Models.Branch:
            return ''
        branch = Models.Branch.query.filter_by(id=branch_id).first()
        return branch.name if branch else ''

    def _actor(actor):
        scope = dict(actor or {})
        user = None
        if scope.get('user_id') and Models.User:
            user = Models.User.query.filter_by(id=scope['user_id']).first()
        if user is not None:
            scope.setdefault('username', user.username)
            scope['role'] = user.role
        return scope

    def _audit(action, summary, actor=None, entity_id=None, changes=None):
        AuditLog = pos.get('AuditLog')
        if not AuditLog:
            return
        encode = pos.get('encode_audit_changes')
        try:
            changes_blob = encode(changes) if (encode and changes) else None
        except Exception:
            changes_blob = None
        try:
            db.session.add(AuditLog(
                actor_user_id=(actor or {}).get('user_id'),
                actor_username=(actor or {}).get('username') or 'Loli',
                actor_role=(actor or {}).get('role'),
                branch_id=(actor or {}).get('branch_id'),
                category='AI',
                action=action,
                entity_type='ScheduledReport',
                entity_id=str(entity_id) if entity_id is not None else None,
                summary=summary[:300],
                changes_blob=changes_blob,
            ))
        except Exception:
            pass

    def _format(value):
        value = str(value or DEFAULT_FORMAT).strip().lower()
        aliases = {'excel': 'xlsx', 'xls': 'xlsx', 'all': 'both', 'excel+pdf': 'both'}
        value = aliases.get(value, value)
        if value not in FORMATS:
            raise ReportDeliveryError(f"format must be one of: {', '.join(FORMATS)}.")
        return value

    def _selector(actor, recipient, custom):
        value = str(recipient or 'me').strip().lower()
        if value not in RECIPIENT_SELECTORS:
            raise ReportDeliveryError(
                f"recipient must be one of: {', '.join(RECIPIENT_SELECTORS)}.")
        if value != 'me' and str(actor.get('role') or '') not in ('manager', 'boss'):
            raise ReportDeliveryError(
                "Only a manager can send a report to someone other than themselves.", 403)
        if value == 'custom' and not (custom or ''):
            raise ReportDeliveryError('Add at least one custom recipient email address.')
        return value

    def _deliver(actor, report_kind, *, recipient='me', custom=None,
                 delivery_format=DEFAULT_FORMAT, note='', params=None,
                 notify_when_empty=True, schedule_text=''):
        """Render one report and email it. Shared by manual sends and schedules."""
        branch_id = actor.get('branch_id')
        if not branch_id:
            raise ReportDeliveryError('An active branch is required to build a report.', 403)
        if report_kind not in REPORT_KINDS:
            raise ReportDeliveryError(
                f"Unknown report '{report_kind}'. Choose one of: {', '.join(sorted(REPORT_KINDS))}.")
        selector = _selector(actor, recipient, custom)
        recipients = resolve_recipients(selector, Models.User, actor_user_id=actor.get('user_id'),
                                        branch_id=branch_id, custom=custom)
        report = build_report(report_kind, Models, branch_id=branch_id,
                              branch_name=_branch_name(branch_id),
                              generated_by=actor.get('username') or '',
                              currency_suffix=_currency(), params=params, brand=_brand())
        row_count = len(report.get('rows') or [])
        if not row_count and not notify_when_empty:
            return {'success': True, 'status': 'skipped', 'report': report_kind,
                    'title': report.get('title'), 'rows': 0, 'recipients': [],
                    'attachments': [],
                    'message': 'There was nothing to report, so no email was sent.'}
        attachments = render_attachments(report, delivery_format)
        text, html = report_email(report, attachments, note=note, schedule_text=schedule_text,
                                  brand_name=_brand()['name'])
        subject = _subject(report_kind, branch_id)
        try:
            sent = email_auth.send_mail(pos['get_setting'], app.secret_key, recipients,
                                        subject, text, html=html, attachments=attachments)
        except email_auth.EmailAuthError as failure:
            raise ReportDeliveryError(failure.message, failure.status) from None
        return {'success': True, 'status': 'sent', 'report': report_kind,
                'title': report.get('title'), 'rows': row_count,
                'recipients': sent['to'], 'attachments': sent['attachments'],
                'subject': subject,
                'summary': [{'label': item.get('label'), 'value': item.get('value')}
                            for item in report.get('summary') or []]}

    def send_now(actor, report_kind, **options):
        """Email a report immediately."""
        actor = _actor(actor)
        return _deliver(actor, report_kind,
                        recipient=options.get('recipient') or 'me',
                        custom=options.get('custom_recipients'),
                        delivery_format=options.get('delivery_format'),
                        note=options.get('note') or '',
                        params=options.get('params'),
                        notify_when_empty=options.get('notify_when_empty', True))

    def _owned_task(actor, task_id):
        try:
            identifier = int(task_id)
        except (TypeError, ValueError):
            raise ReportDeliveryError('task_id must be a number.') from None
        task = ScheduledReport.query.filter_by(id=identifier).first()
        if not task:
            raise ReportDeliveryError(f'Scheduled report {identifier} was not found.', 404)
        same_user = task.user_id == actor.get('user_id')
        manager = (str(actor.get('role') or '') in ('manager', 'boss')
                   and task.branch_id == actor.get('branch_id'))
        if not (same_user or manager):
            raise ReportDeliveryError('That scheduled report belongs to someone else.', 403)
        return task

    def _subject(report_kind, branch_id):
        stamp = dt.datetime.now(BUSINESS_TIMEZONE).strftime('%Y-%m-%d')
        label = REPORT_KINDS[report_kind]['subject']
        branch = _branch_name(branch_id) or 'POS'
        return f'{label} - {branch} - {stamp}'


    def create(actor, report_kind, **options):
        """Save a new daily or weekly report schedule."""
        actor = _actor(actor)
        user_id, branch_id = actor.get('user_id'), actor.get('branch_id')
        if not user_id or not branch_id:
            raise ReportDeliveryError('Sign in again to schedule a report.', 403)
        if report_kind not in REPORT_KINDS:
            raise ReportDeliveryError(
                f"Unknown report '{report_kind}'. Choose one of: {', '.join(sorted(REPORT_KINDS))}.")
        hour, minute = parse_send_time(options.get('send_time') or DEFAULT_SEND_TIME)
        send_time = f'{hour:02d}:{minute:02d}'
        frequency = normalize_frequency(options.get('frequency') or 'daily')
        weekday = normalize_weekday(options.get('weekday')) if frequency == 'weekly' else ''
        if frequency == 'weekly' and not weekday:
            weekday = 'monday'
        selector = _selector(actor, options.get('recipient'), options.get('custom_recipients'))
        delivery_format = _format(options.get('delivery_format'))
        active = ScheduledReport.query.filter_by(user_id=user_id, enabled=True).count()
        if active >= MAX_TASKS_PER_USER:
            raise ReportDeliveryError(
                f'You already have {MAX_TASKS_PER_USER} active scheduled reports. Cancel one first.')
        task = ScheduledReport(
            user_id=user_id, branch_id=branch_id,
            username=actor.get('username') or '', role=actor.get('role') or 'cashier',
            report_kind=report_kind, params_json=_dumps(options.get('params') or {}),
            delivery_format=delivery_format, recipient=selector,
            custom_recipients=(_dumps(list(options.get('custom_recipients') or []))
                               if selector == 'custom' else None),
            note=(str(options.get('note') or '').strip()[:500] or None),
            send_time=send_time, frequency=frequency, weekday=weekday or None,
            timezone=BUSINESS_TIMEZONE.zone,
            notify_when_empty=bool(options.get('notify_when_empty', True)), enabled=True,
            next_run_at=next_run_epoch(send_time, frequency, weekday or '',
                                       timezone_name=BUSINESS_TIMEZONE.zone))
        db.session.add(task)
        db.session.flush()
        _audit('create',
               f"Scheduled {REPORT_KINDS[report_kind]['label']} "
               f"{describe_schedule(frequency, send_time, weekday)} to {selector}",
               actor, task.id,
               {'report': report_kind, 'send_time': send_time,
                'frequency': frequency, 'recipient': selector})
        db.session.commit()
        return {'success': True, 'task': task.to_dict(),
                'message': f"Saved. I will email the {REPORT_KINDS[report_kind]['label'].lower()} "
                           f"to {selector} {describe_schedule(frequency, send_time, weekday)} "
                           f"({BUSINESS_TIMEZONE.zone})."}

    def list_tasks(actor, include_disabled=False):
        """Scheduled reports the caller may see."""
        actor = _actor(actor)
        query = ScheduledReport.query
        if str(actor.get('role') or '') in ('manager', 'boss') and actor.get('branch_id'):
            query = query.filter_by(branch_id=actor['branch_id'])
        else:
            query = query.filter_by(user_id=actor.get('user_id'))
        if not include_disabled:
            query = query.filter_by(enabled=True)
        tasks = query.order_by(ScheduledReport.next_run_at).all()
        return {'success': True, 'count': len(tasks), 'tasks': [task.to_dict() for task in tasks]}


    def update(actor, task_id, **changes):
        """Change an existing schedule (time, frequency, format, recipient, report)."""
        actor = _actor(actor)
        task = _owned_task(actor, task_id)
        applied = {}
        if changes.get('send_time'):
            hour, minute = parse_send_time(changes['send_time'])
            task.send_time = f'{hour:02d}:{minute:02d}'
            applied['send_time'] = task.send_time
        if changes.get('frequency'):
            task.frequency = normalize_frequency(changes['frequency'])
            applied['frequency'] = task.frequency
        if changes.get('weekday') is not None:
            task.weekday = normalize_weekday(changes['weekday']) or None
            applied['weekday'] = task.weekday
        if changes.get('delivery_format'):
            task.delivery_format = _format(changes['delivery_format'])
            applied['delivery_format'] = task.delivery_format
        if changes.get('recipient'):
            selector = _selector(actor, changes['recipient'], changes.get('custom_recipients'))
            task.recipient = selector
            task.custom_recipients = (_dumps(list(changes.get('custom_recipients') or []))
                                      if selector == 'custom' else None)
            applied['recipient'] = selector
        if changes.get('report_kind'):
            if changes['report_kind'] not in REPORT_KINDS:
                raise ReportDeliveryError(
                    f"Unknown report '{changes['report_kind']}'. "
                    f"Choose one of: {', '.join(sorted(REPORT_KINDS))}.")
            task.report_kind = changes['report_kind']
            applied['report'] = task.report_kind
        if changes.get('params') is not None:
            task.params_json = _dumps(changes['params'] or {})
        if changes.get('note') is not None:
            task.note = str(changes.get('note') or '').strip()[:500] or None
        if changes.get('notify_when_empty') is not None:
            task.notify_when_empty = bool(changes['notify_when_empty'])
        if changes.get('enabled') is not None:
            task.enabled = bool(changes['enabled'])
        if not applied and not changes:
            raise ReportDeliveryError('Tell me what to change: time, frequency, weekday, '
                                      'format, recipient, report or note.')
        task.next_run_at = (next_run_epoch(task.send_time, task.frequency, task.weekday or '',
                                           timezone_name=task.timezone)
                            if task.enabled else None)
        _audit('update', f"Updated scheduled {task.report_kind} for task {task.id}",
               actor, task.id, applied)
        db.session.commit()
        return {'success': True, 'task': task.to_dict(),
                'message': f"Updated. Next run {local_label(task.next_run_at, task.timezone)}."
                           if task.next_run_at else 'Updated. This schedule is now paused.'}

    def cancel(actor, task_id, reason=''):
        """Stop a schedule. The row is kept so history stays auditable."""
        actor = _actor(actor)
        task = _owned_task(actor, task_id)
        task.enabled = False
        task.next_run_at = None
        _audit('cancel', f"Cancelled scheduled {task.report_kind} for task {task.id}",
               actor, task.id, {'reason': str(reason or '')[:120]})
        db.session.commit()
        return {'success': True, 'task': task.to_dict(),
                'message': 'That schedule is cancelled. No further emails will be sent.'}


    # -----------------------------------------------------------------
    # Background delivery
    # -----------------------------------------------------------------

    def _claim_due(now):
        """Atomically reserve the next due schedule. Safe with two workers."""
        # A row left 'running' by a crash is reclaimable after the stale window.
        stale_before = now - STALE_CLAIM_SECONDS
        with db.engine.connect() as connection:
            connection.exec_driver_sql('BEGIN IMMEDIATE')
            try:
                row = connection.execute(text(
                    "SELECT id FROM scheduled_report WHERE enabled = 1 "
                    "AND next_run_at IS NOT NULL AND next_run_at <= :now "
                    "AND (COALESCE(last_status, '') != 'running' "
                    "     OR last_run_at IS NULL OR last_run_at < :stale_before) "
                    "ORDER BY next_run_at LIMIT 1"),
                    {'now': now, 'stale_before': stale_before}).fetchone()
                if row is None:
                    connection.rollback()
                    return None
                connection.execute(
                    text("UPDATE scheduled_report SET last_status = 'running' WHERE id = :id"),
                    {'id': row[0]})
                connection.commit()
                task_id = row[0]
            except Exception:
                connection.rollback()
                raise
        # Drop cached identity so the worker reads the row it just claimed.
        db.session.remove()
        return task_id

    def _execute_task(task_id, trigger='schedule', now=None):
        """Run one schedule. Returns a result dict and never raises."""
        now = time.time() if now is None else float(now)
        task = ScheduledReport.query.filter_by(id=task_id).first()
        if task is None:
            return {'task_id': task_id, 'status': 'missing'}
        run = ScheduledReportRun(task_id=task.id, started_at=now, status='running')
        db.session.add(run)
        db.session.commit()
        run_id = run.id
        status, error, recipients, attachments = 'sent', '', [], []
        stale = (task.next_run_at is not None
                 and (now - float(task.next_run_at)) > CATCH_UP_SECONDS)
        try:
            user = Models.User.query.filter_by(id=task.user_id).first() if Models.User else None
            if user is None:
                raise _DisableJob('The account that scheduled this report no longer exists.')
            branch = (Models.Branch.query.filter_by(id=task.branch_id).first()
                      if Models.Branch else None)
            if branch is None or not getattr(branch, 'is_active', True):
                raise _DisableJob('The branch for this report is inactive.')
            if task.recipient == 'me' and not user.email:
                raise _DisableJob('The requester no longer has a linked email address.')
            actor = {'user_id': user.id, 'role': user.role, 'branch_id': task.branch_id,
                     'username': user.username}
            if task.role != user.role:
                task.role = user.role  # provenance always follows the live account
            if trigger == 'schedule' and stale:
                status = 'missed'
                error = 'Missed while the app was offline; the next run is unchanged.'
            else:
                outcome = _deliver(
                    actor, task.report_kind, recipient=task.recipient,
                    custom=_loads(task.custom_recipients, []),
                    delivery_format=task.delivery_format, note=task.note or '',
                    params=_loads(task.params_json, {}),
                    notify_when_empty=bool(task.notify_when_empty),
                    schedule_text=describe_schedule(task.frequency, task.send_time,
                                                    task.weekday or ''))
                status = outcome.get('status') or 'sent'
                recipients = outcome.get('recipients') or []
                attachments = outcome.get('attachments') or []
                if status == 'skipped':
                    error = outcome.get('message') or ''
        except _DisableJob as stop:
            task.enabled = False
            task.next_run_at = None
            status, error = 'disabled', str(stop)
        except (ReportDeliveryError, email_auth.EmailAuthError) as failure:
            status, error = 'failed', getattr(failure, 'message', str(failure))
        except Exception as exc:  # one bad job must never stop the poller
            db.session.rollback()
            status, error = 'failed', str(exc)[:280]
        finally:
            task = ScheduledReport.query.filter_by(id=task_id).first()
            run = ScheduledReportRun.query.filter_by(id=run_id).first()
            if task is not None:
                task.last_status = status
                task.last_run_at = now
                task.last_error = (error or '')[:300] or None
                if task.enabled:
                    try:
                        task.next_run_at = next_run_epoch(
                            task.send_time, task.frequency, task.weekday or '',
                            after=now, timezone_name=task.timezone)
                    except ReportDeliveryError:
                        task.enabled = False
                        task.next_run_at = None
            if run is not None:
                run.status = status
                run.error = (error or '')[:300] or None
                run.recipients = ', '.join(recipients)[:500] or None
                run.attachments = ', '.join(attachments)[:500] or None
                run.finished_at = time.time()
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
        return {'task_id': task_id, 'status': status, 'error': error,
                'recipients': recipients}


    def mail_status():
        """Whether the Resend relay is configured for outbound report mail."""
        settings = email_auth.mail_config(pos['get_setting'])
        return {'configured': bool(settings.valid
                                   and email_auth.is_secure_session_key(app.secret_key)),
                'sender': settings.sender, 'host': settings.host, 'port': settings.port}

    def run_due(now=None):
        """Send every schedule that is due. Returns one result per job."""
        now = time.time() if now is None else float(now)
        results = []
        while True:
            task_id = _claim_due(now)
            if task_id is None:
                break
            results.append(_execute_task(task_id, trigger='schedule', now=now))
        return results

    def run_task(task_id, now=None):
        """Run one schedule now, as if its time had arrived."""
        return _execute_task(task_id, trigger='manual', now=now)

    def status(actor=None):
        actor = _actor(actor or {})
        query = (ScheduledReport.query.filter_by(user_id=actor['user_id'])
                 if actor.get('user_id') else ScheduledReport.query)
        active = query.filter_by(enabled=True).all()
        upcoming = sorted(task.next_run_at for task in active if task.next_run_at)
        return {'success': True, 'active': len(active),
                'next_run': local_label(upcoming[0]) if upcoming else '',
                'timezone': BUSINESS_TIMEZONE.zone,
                'report_kinds': sorted(REPORT_KINDS),
                'scheduler_running': bool(_scheduler_thread and _scheduler_thread.is_alive())}

    def start(force=False):
        """Start the single background poller (no-op in tests and when disabled)."""
        global _scheduler_thread
        if not force and not _should_autostart(app):
            return None
        with _scheduler_lock:
            if _scheduler_thread is not None and _scheduler_thread.is_alive():
                return _scheduler_thread
            try:
                interval = max(5.0, float(os.environ.get('AI_SCHEDULER_INTERVAL', POLL_SECONDS)))
            except (TypeError, ValueError):
                interval = POLL_SECONDS
            _scheduler_stop.clear()

            def loop():
                while not _scheduler_stop.is_set():
                    try:
                        with app.app_context():
                            run_due()
                    except Exception as exc:  # keep polling; one bad tick is not fatal
                        print(f'[Report scheduler] tick failed: {exc}')
                    _scheduler_stop.wait(interval)

            _scheduler_thread = threading.Thread(target=loop, name='report-scheduler',
                                                 daemon=True)
            _scheduler_thread.start()
            print(f'[Report scheduler] started every {interval:.0f}s '
                  f'({BUSINESS_TIMEZONE.zone})')
            return _scheduler_thread

    def stop():
        """Ask the poller to exit (used by tests and hot reloads)."""
        _scheduler_stop.set()
        return True

    return SimpleNamespace(
        models={'ScheduledReport': ScheduledReport,
                'ScheduledReportRun': ScheduledReportRun},
        kinds=lambda: {name: dict(meta) for name, meta in REPORT_KINDS.items()},
        recipients=RECIPIENT_SELECTORS,
        send_now=send_now,
        create=create,
        list_tasks=list_tasks,
        update=update,
        cancel=cancel,
        mail_status=mail_status,
        run_due=run_due,
        run_task=run_task,
        status=status,
        start=start,
        stop=stop,
    )

