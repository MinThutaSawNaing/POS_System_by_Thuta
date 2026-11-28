"""Per-item MOQ low-stock reminders.

Each product's ``reorder_point`` is its MOQ: the minimum stock level the shop
wants to keep. When stock is at or below that number the item is "running low":
the dashboard lists it, and one email a day goes to the branch's managers plus
every boss account with a linked address.

``inventory_reminder_log`` records which product was emailed on which day, so a
restart (or a second poll tick) never repeats the same reminder. Rows carry no
foreign keys: deleting a product must not break the reminder history.
"""

import datetime as dt
import os
import re
import sys
import threading
from types import SimpleNamespace

import email_auth
from report_scheduler import BUSINESS_TIMEZONE


DEFAULT_INTERVAL_SECONDS = 300
MAX_EMAIL_RECIPIENTS = 20
REMINDER_SETTING = 'inventory_reminder_email'
REMINDER_SEND_TIME_SETTING = 'inventory_reminder_send_time'
DEFAULT_SEND_TIME = '09:00'


class ReminderError(Exception):
    """A reminder problem that is safe to show the manager verbatim."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


def normalize_send_time(value):
    """Return a strict ``HH:MM`` wall-clock time or raise ``ReminderError``."""
    if not isinstance(value, str) or re.fullmatch(
            r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value) is None:
        raise ReminderError(
            'Enter a valid daily send time in HH:MM format (00:00 through 23:59).')
    return value


def _escape(value):
    return str(value).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def reminder_email(branch_name, items, brand_name=''):
    """Plain-text and HTML bodies for one branch's new low-stock items."""
    brand = brand_name or 'PARROT POS'
    title = f'Low stock in {branch_name}' if branch_name else 'Low stock reminder'
    lines = [
        title,
        brand,
        '',
        f'{len(items)} item(s) at or below MOQ (minimum stock level):',
        '',
    ]
    for item in items:
        sku = f" [SKU {item['sku']}]" if item.get('sku') else ''
        lines.append(
            f"- {item['name']}{sku}: {item['current_stock']} on hand, "
            f"MOQ {item['moq']}, suggested order {item['suggested_qty']}")
    lines += ['',
              'Restock in Warehouse, then transfer the units to the branch so '
              'branch stock updates.',
              '',
              'Sent automatically by Loli, the POS assistant. Replies are not monitored.']
    text = '\n'.join(lines)

    rows = ''.join(
        '<tr>'
        f'<td style="padding:8px 12px;border-bottom:1px solid #ececec;">'
        f'<strong>{_escape(item["name"])}</strong>'
        + (f'<div style="color:#6f6f6f;font-size:12px;">SKU {_escape(item["sku"])}</div>'
           if item.get('sku') else '')
        + '</td>'
        f'<td style="padding:8px 12px;border-bottom:1px solid #ececec;text-align:right;">'
        f'{_escape(item["current_stock"])}</td>'
        f'<td style="padding:8px 12px;border-bottom:1px solid #ececec;text-align:right;">'
        f'{_escape(item["moq"])}</td>'
        f'<td style="padding:8px 12px;border-bottom:1px solid #ececec;text-align:right;">'
        f'{_escape(item["suggested_qty"])}</td>'
        '</tr>' for item in items)
    html = (
        '<!DOCTYPE html><html lang="en"><body style="margin:0;padding:0;background-color:#f4f4f4;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="padding:24px 12px;"><tr><td align="center">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="max-width:600px;background-color:#ffffff;border:1px solid #e0e0e0;'
        'border-radius:8px;font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;">'
        '<tr><td bgcolor="#111" style="background-color:#111;padding:18px 24px;color:#ffffff;'
        'font-size:15px;font-weight:700;letter-spacing:.06em;">'
        + _escape(brand) + '</td></tr>'
        '<tr><td style="padding:24px;">'
        f'<h1 style="margin:0 0 4px;font-size:20px;color:#111;">{_escape(title)}</h1>'
        f'<p style="margin:0 0 18px;color:#555;font-size:14px;">'
        f'{len(items)} item(s) are at or below their MOQ.</p>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;font-size:14px;color:#111;">'
        '<tr style="background-color:#f8fafc;">'
        '<th style="padding:8px 12px;text-align:left;">Product</th>'
        '<th style="padding:8px 12px;text-align:right;">On hand</th>'
        '<th style="padding:8px 12px;text-align:right;">MOQ</th>'
        '<th style="padding:8px 12px;text-align:right;">Suggested order</th>'
        '</tr>' + rows + '</table>'
        '<p style="margin:18px 0 0;color:#333;font-size:14px;">Restock in Warehouse, then '
        'transfer the units to the branch so branch stock updates.</p>'
        '</td></tr><tr><td style="background-color:#f8fafc;border-top:1px solid #e0e0e0;'
        'padding:14px 24px;color:#6f6f6f;font-size:12px;">Sent automatically by Loli, the POS '
        'assistant. Replies are not monitored.</td></tr></table></td></tr></table>'
        '</body></html>')
    return f'Low stock: {len(items)} item(s) at or below MOQ' + (
        f' — {branch_name}' if branch_name else ''), text, html


def register_inventory_reminders(app, db, pos):
    """Create the reminder log table and return the service the app uses."""

    class InventoryReminderLog(db.Model):
        __tablename__ = 'inventory_reminder_log'
        __table_args__ = (
            db.UniqueConstraint('product_id', 'branch_id', 'reminder_date',
                                name='uq_inventory_reminder_day'),
        )

        id = db.Column(db.Integer, primary_key=True)
        product_id = db.Column(db.Integer, nullable=False, index=True)
        branch_id = db.Column(db.Integer, nullable=False, index=True)
        reminder_date = db.Column(db.Date, nullable=False, index=True)
        recipients = db.Column(db.String(500), nullable=True)
        created_at = db.Column(db.DateTime, default=dt.datetime.utcnow, nullable=False)

    Models = SimpleNamespace(Product=pos.get('Product'), Branch=pos.get('Branch'),
                             User=pos.get('User'))

    def _setting(key, default=None):
        try:
            return pos['get_setting'](key, default)
        except Exception:
            return default

    def _email_enabled():
        value = str(_setting(REMINDER_SETTING, '1') or '1').strip().lower()
        return value not in ('0', 'false', 'off', 'no')

    def _send_time():
        try:
            return normalize_send_time(
                _setting(REMINDER_SEND_TIME_SETTING, DEFAULT_SEND_TIME))
        except ReminderError:
            return DEFAULT_SEND_TIME

    def mail_status():
        try:
            ready = bool(email_auth.mail_config(pos.get('get_setting')).valid)
        except Exception:
            ready = False
        return {'enabled': _email_enabled(), 'ready': ready}

    def low_stock_items(branch_id=None):
        """Enabled products whose stock is at or below their own MOQ."""
        Product = Models.Product
        if Product is None:
            return []
        stock = db.func.coalesce(Product.stock, 0)
        moq = db.func.coalesce(Product.reorder_point, 0)
        query = Product.query.filter(Product.reorder_enabled.is_(True), stock <= moq)
        if branch_id is not None:
            query = query.filter(Product.branch_id == branch_id)
        items = []
        for product in query.order_by(Product.name.asc(), Product.id.asc()).all():
            current = int(product.stock or 0)
            moq_value = max(int(product.reorder_point or 0), 0)
            order_qty = max(int(product.reorder_quantity or 0), 0)
            suggested = order_qty or max(moq_value - current, 1)
            items.append({
                'product_id': product.id,
                'name': product.name,
                'barcode': product.barcode,
                'sku': product.sku,
                'category': product.category,
                'current_stock': current,
                'moq': moq_value,
                'reorder_point': moq_value,
                'reorder_quantity': order_qty,
                'suggested_qty': suggested,
            })
        return items

    def payload(branch_id=None):
        Product = Models.Product
        total = 0
        out_of_stock = 0
        if Product is not None:
            stock = db.func.coalesce(Product.stock, 0)
            totals = Product.query
            empty = Product.query.filter(stock <= 0)
            if branch_id is not None:
                totals = totals.filter(Product.branch_id == branch_id)
                empty = empty.filter(Product.branch_id == branch_id)
            total = totals.count()
            out_of_stock = empty.count()
        items = low_stock_items(branch_id)
        return {
            'summary': {
                'total_products': total,
                'low_stock_count': len(items),
                'out_of_stock_count': out_of_stock,
            },
            'items': items,
            'email': mail_status(),
        }

    def recipients(branch_id):
        """Branch managers plus every boss; cashiers never receive reminders."""
        User = Models.User
        if User is None:
            return []
        users = User.query.filter(db.or_(
            db.and_(User.role == 'manager', User.branch_id == branch_id),
            User.role == 'boss')).all()
        addresses = set()
        for user in users:
            address = (user.email or '').strip()
            if not address:
                continue
            try:
                addresses.add(email_auth.normalize_email(address))
            except ValueError:
                continue
        return sorted(addresses)[:MAX_EMAIL_RECIPIENTS]

    def run(now=None):
        """Send due reminders, reading the configured time on every tick."""
        results = []
        if not _email_enabled() or Models.Product is None or Models.Branch is None:
            return results
        if not mail_status()['ready']:
            return results
        if now is None:
            now = dt.datetime.now(BUSINESS_TIMEZONE)
        elif now.tzinfo is not None:
            now = now.astimezone(BUSINESS_TIMEZONE)
        send_hour, send_minute = map(int, _send_time().split(':'))
        if (now.hour, now.minute) < (send_hour, send_minute):
            return results
        today = now.date()
        for branch in Models.Branch.query.filter_by(is_active=True).order_by(
                Models.Branch.id).all():
            items = low_stock_items(branch.id)
            if not items:
                continue
            already = {row.product_id for row in InventoryReminderLog.query.filter_by(
                branch_id=branch.id, reminder_date=today).all()}
            pending = [item for item in items if item['product_id'] not in already]
            if not pending:
                continue
            addresses = recipients(branch.id)
            if not addresses:
                continue
            brand = _setting('pos_name') or 'Parrot POS'
            subject, text, html = reminder_email(branch.name, pending, brand)
            try:
                email_auth.send_mail(_setting, app.secret_key, addresses, subject, text,
                                     html=html)
            except email_auth.EmailAuthError as failure:
                app.logger.warning('Low-stock reminder email failed for branch %s: %s',
                                   branch.id, failure)
                continue
            for item in pending:
                db.session.add(InventoryReminderLog(
                    product_id=item['product_id'], branch_id=branch.id,
                    reminder_date=today, recipients=','.join(addresses)[:500]))
            try:
                db.session.commit()
            except Exception as exc:
                db.session.rollback()
                app.logger.warning(
                    'Could not record low-stock reminders for branch %s: %s', branch.id, exc)
                continue
            results.append({'status': 'sent', 'branch_id': branch.id,
                            'items': len(pending), 'recipients': len(addresses)})
        return results

    def send_test(branch_id, recipient):
        """Email the real reminder template for this branch to one address.

        A test never writes to ``inventory_reminder_log``: it must not suppress
        the scheduled reminder for the same item later that day.
        """
        if Models.Product is None or Models.Branch is None:
            raise ReminderError('Inventory is unavailable right now.', 503)
        status = mail_status()
        if not status['enabled']:
            raise ReminderError(
                'Reminder emails are switched off. Choose "On" and save first.', 400)
        if not status['ready']:
            raise ReminderError(
                'Email is not configured. Save the Resend SMTP settings first.', 503)
        items = low_stock_items(branch_id)
        if not items:
            raise ReminderError(
                'No item is at or below its MOQ right now, so there is nothing to '
                'preview. Lower an item\'s stock or raise its MOQ first.', 400)
        branch = Models.Branch.query.filter_by(id=branch_id).first()
        brand = _setting('pos_name') or 'Parrot POS'
        subject, text, html = reminder_email(branch.name if branch else '', items, brand)
        try:
            email_auth.send_mail(_setting, app.secret_key, [recipient], subject, text,
                                 html=html)
        except email_auth.EmailAuthError as failure:
            raise ReminderError(failure.message, failure.status) from None
        return {'items': len(items), 'recipient': recipient}

    _scheduler_thread = None
    _scheduler_lock = threading.Lock()
    _scheduler_stop = threading.Event()

    def _should_autostart():
        if os.environ.get('INVENTORY_REMINDER_ENABLED', '1').strip().lower() in (
                '0', 'false', 'off', 'no'):
            return False
        return not ('pytest' in sys.modules or app.config.get('TESTING'))

    def start(force=False):
        """Start the single background poller (no-op in tests and when disabled)."""
        nonlocal _scheduler_thread
        if not force and not _should_autostart():
            return None
        with _scheduler_lock:
            if _scheduler_thread is not None and _scheduler_thread.is_alive():
                return _scheduler_thread
            try:
                interval = max(30.0, float(
                    os.environ.get('INVENTORY_REMINDER_INTERVAL', DEFAULT_INTERVAL_SECONDS)))
            except (TypeError, ValueError):
                interval = DEFAULT_INTERVAL_SECONDS
            _scheduler_stop.clear()

            def loop():
                while not _scheduler_stop.is_set():
                    try:
                        with app.app_context():
                            run()
                    except Exception as exc:  # one bad tick is not fatal
                        print(f'[Inventory reminders] tick failed: {exc}')
                    _scheduler_stop.wait(interval)

            _scheduler_thread = threading.Thread(
                target=loop, name='inventory-reminders', daemon=True)
            _scheduler_thread.start()
            print(f'[Inventory reminders] started every {interval:.0f}s')
            return _scheduler_thread

    def stop():
        _scheduler_stop.set()
        return True

    return SimpleNamespace(
        models={'InventoryReminderLog': InventoryReminderLog},
        setting_key=REMINDER_SETTING,
        send_time_setting=REMINDER_SEND_TIME_SETTING,
        default_send_time=DEFAULT_SEND_TIME,
        timezone=BUSINESS_TIMEZONE.zone,
        normalize_send_time=normalize_send_time,
        send_time=_send_time,
        items=low_stock_items,
        payload=payload,
        recipients=recipients,
        run=run,
        send_test=send_test,
        mail_status=mail_status,
        start=start,
        stop=stop,
    )
