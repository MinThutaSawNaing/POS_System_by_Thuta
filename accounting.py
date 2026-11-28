"""Accrual ledger and accounting HTTP API. No application import or startup I/O.

Amounts are signed integer minor units (debits positive). Source integrations
must use post()/document() in their own transaction; neither method commits.
"""
from collections import defaultdict
from datetime import date as Date, datetime, timedelta, timezone
from decimal import Decimal
from functools import wraps
import hashlib
import json
import re

from flask import Blueprint, current_app, has_app_context, has_request_context, jsonify, request, session
from sqlalchemy import event, func, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, attributes, selectinload


ACCOUNTS = {
    code: dict(code=code, name=name, type=kind, control=control)
    for code, name, kind, control in (
        ('CASH', 'Cash', 'asset', False), ('BANK', 'Bank', 'asset', False),
        ('AR', 'Accounts receivable', 'asset', True),
        ('INVENTORY', 'Inventory', 'asset', True),
        ('FIXED_ASSETS', 'Fixed assets', 'asset', True),
        ('ACCUM_DEPR', 'Accumulated depreciation', 'asset', True),
        ('AP', 'Accounts payable', 'liability', True),
        ('GRNI', 'Goods received not invoiced', 'liability', True),
        ('TAX_PAYABLE', 'Tax payable', 'liability', False),
        ('OPENING_EQUITY', 'Opening equity', 'equity', True),
        ('CAPITAL', 'Capital', 'equity', False),
        ('INTERBRANCH', 'Interbranch clearing', 'asset', True),
        ('SALES', 'Sales', 'income', False),
        ('SALES_RETURNS', 'Sales returns', 'income', False),
        ('OTHER_INCOME', 'Other income', 'income', False),
        ('COGS', 'Cost of goods sold', 'expense', True),
        ('RENT', 'Rent', 'expense', False),
        ('UTILITIES', 'Utilities', 'expense', False),
        ('SALARIES', 'Salaries', 'expense', False),
        ('GENERAL_EXPENSE', 'General expense', 'expense', False),
        ('DEPRECIATION', 'Depreciation', 'expense', True),
        ('INVENTORY_ADJUSTMENT', 'Inventory adjustment', 'expense', True),
        ('ASSET_DISPOSAL', 'Asset disposal gain / loss', 'expense', True),
    )
}
MAX_MINOR = 99_999_999_999_999


class AccountingError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


def parse_money(value, allow_negative=False):
    """Strict fixed-point money, at most twelve whole digits and two decimals."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise AccountingError('Money must be a decimal string with at most two decimal places')
    if isinstance(value, Decimal):
        if not value.is_finite() or value.adjusted() > 11 or value.as_tuple().exponent < -2:
            raise AccountingError('Money is out of range or has more than two decimal places')
        value = format(value, 'f')
    value = str(value)
    pattern = r'-?[0-9]{1,12}(?:\.[0-9]{1,2})?' if allow_negative else r'[0-9]{1,12}(?:\.[0-9]{1,2})?'
    if not re.fullmatch(pattern, value):
        raise AccountingError('Money must have at most twelve whole digits and two decimal places')
    result = int(Decimal(value) * 100)
    if abs(result) > MAX_MINOR:
        raise AccountingError('Money is out of range')
    return result


def money(value):
    if type(value) is not int:
        raise AccountingError('Minor units must be integers')
    # Integer formatting also preserves exact very large report totals.
    sign = '-' if value < 0 else ''
    whole, fraction = divmod(abs(value), 100)
    return f'{sign}{whole}.{fraction:02d}'


def _string(value, label, maximum=200, empty=False):
    if not isinstance(value, str) or len(value) > maximum or any(not c.isprintable() for c in value):
        raise AccountingError(f'{label} must be printable text of at most {maximum} characters')
    value = value.strip()
    if not empty and not value:
        raise AccountingError(f'{label} is required')
    return value


def _integer(value, label, minimum=1, maximum=2_147_483_647):
    if type(value) is not int or not minimum <= value <= maximum:
        raise AccountingError(f'{label} must be an integer from {minimum} to {maximum}')
    return value


def _shape(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise AccountingError('Invalid fields; required: ' + ', '.join(sorted(required)))
    return value


def _models(db):
    if hasattr(db, '_accounting_models'):
        return db._accounting_models

    class Config(db.Model):
        __tablename__ = 'accounting_config'
        id = db.Column(db.Integer, primary_key=True, default=1)
        currency = db.Column(db.String(12), nullable=False)
        start_date = db.Column(db.Date, nullable=False)
        activated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
        locked_through = db.Column(db.Date)
        __table_args__ = (db.CheckConstraint('id = 1'),)

    class Location(db.Model):
        __tablename__ = 'accounting_location'
        id = db.Column(db.Integer, primary_key=True)
        branch_id = db.Column(db.Integer, unique=True)
        kind = db.Column(db.String(16), nullable=False)
        name = db.Column(db.String(160), nullable=False)
        __table_args__ = (
            db.CheckConstraint("(kind = 'warehouse' AND branch_id IS NULL) OR (kind = 'branch' AND branch_id IS NOT NULL)"),
            db.Index('uq_accounting_warehouse', 'kind', unique=True,
                     sqlite_where=text("kind = 'warehouse'"), postgresql_where=text("kind = 'warehouse'")),
        )

    class Journal(db.Model):
        __tablename__ = 'accounting_journal'
        id = db.Column(db.Integer, primary_key=True)
        source_key = db.Column(db.String(200), unique=True, nullable=False)
        source_type = db.Column(db.String(48), nullable=False, index=True)
        date = db.Column(db.Date, nullable=False, index=True)
        memo = db.Column(db.String(1000), nullable=False, default='')
        currency = db.Column(db.String(12), nullable=False)
        actor_id = db.Column(db.Integer)
        reversal_of = db.Column(db.Integer, db.ForeignKey('accounting_journal.id'), unique=True)
        lines = db.relationship('Line', back_populates='journal', lazy='selectin', order_by='Line.id')

    class Line(db.Model):
        __tablename__ = 'accounting_line'
        id = db.Column(db.Integer, primary_key=True)
        journal_id = db.Column(db.Integer, db.ForeignKey('accounting_journal.id'), nullable=False, index=True)
        location_id = db.Column(db.Integer, db.ForeignKey('accounting_location.id'), nullable=False, index=True)
        account_code = db.Column(db.String(40), nullable=False, index=True)
        amount_minor = db.Column(db.BigInteger, nullable=False)
        reference = db.Column(db.String(200))
        journal = db.relationship(Journal, back_populates='lines')
        __table_args__ = (db.CheckConstraint(f'amount_minor != 0 AND amount_minor BETWEEN {-MAX_MINOR} AND {MAX_MINOR}'),)

    class Asset(db.Model):
        __tablename__ = 'accounting_asset'
        id = db.Column(db.Integer, primary_key=True)
        location_id = db.Column(db.Integer, db.ForeignKey('accounting_location.id'), nullable=False, index=True)
        name = db.Column(db.String(160), nullable=False)
        category = db.Column(db.String(100), nullable=False)
        acquired_on = db.Column(db.Date, nullable=False)
        cost_minor = db.Column(db.BigInteger, nullable=False)
        salvage_minor = db.Column(db.BigInteger, nullable=False, default=0)
        life_months = db.Column(db.Integer, nullable=False)
        accumulated_minor = db.Column(db.BigInteger, nullable=False, default=0)
        status = db.Column(db.String(16), nullable=False, default='active')
        last_depreciation_date = db.Column(db.Date)
        __table_args__ = (
            db.CheckConstraint(f'cost_minor > 0 AND cost_minor <= {MAX_MINOR}'),
            db.CheckConstraint('salvage_minor >= 0 AND salvage_minor <= cost_minor'),
            db.CheckConstraint('accumulated_minor >= 0 AND accumulated_minor <= cost_minor - salvage_minor'),
            db.CheckConstraint("status IN ('active', 'disposed') AND life_months > 0"),
        )

    class Payable(db.Model):
        __tablename__ = 'accounting_payable'
        id = db.Column(db.Integer, primary_key=True)
        location_id = db.Column(db.Integer, db.ForeignKey('accounting_location.id'), nullable=False, index=True)
        supplier_id = db.Column(db.Integer, nullable=False, index=True)
        reference = db.Column(db.String(120), nullable=False)
        invoice_date = db.Column(db.Date, nullable=False)
        due_date = db.Column(db.Date)
        total_minor = db.Column(db.BigInteger, nullable=False)
        balance_minor = db.Column(db.BigInteger, nullable=False)
        source_key = db.Column(db.String(200), unique=True, nullable=False)
        __table_args__ = (
            db.UniqueConstraint('supplier_id', 'reference', name='uq_accounting_supplier_reference'),
            db.CheckConstraint(f'total_minor >= 0 AND total_minor <= {MAX_MINOR}'),
            db.CheckConstraint('balance_minor >= 0 AND balance_minor <= total_minor'),
        )

    class Document(db.Model):
        __tablename__ = 'accounting_document'
        id = db.Column(db.Integer, primary_key=True)
        source_key = db.Column(db.String(200), unique=True, nullable=False)
        kind = db.Column(db.String(24), nullable=False, index=True)
        location_id = db.Column(db.Integer, db.ForeignKey('accounting_location.id'), nullable=False, index=True)
        date = db.Column(db.Date, nullable=False, index=True)
        number = db.Column(db.String(48), unique=True)
        snapshot = db.Column(db.JSON, nullable=False)
        total_minor = db.Column(db.BigInteger, nullable=False)
        __table_args__ = (
            db.CheckConstraint("kind IN ('sales', 'sales-return', 'purchase', 'purchase-return')"),
            db.CheckConstraint(f'total_minor >= 0 AND total_minor <= {MAX_MINOR}'),
        )

    class Command(db.Model):
        """Atomic request receipts prevent reapplying mutable subledger actions."""
        __tablename__ = 'accounting_command'
        id = db.Column(db.Integer, primary_key=True)
        key = db.Column(db.String(200), nullable=False, unique=True)
        fingerprint = db.Column(db.String(64), nullable=False)
        response = db.Column(db.JSON, nullable=False)

    models = {c.__name__: c for c in (Config, Location, Journal, Line, Asset, Payable, Document, Command)}
    db._accounting_models = models
    return models


class AccountingService:
    parse_money = staticmethod(parse_money)
    money = staticmethod(money)
    AccountingError = AccountingError
    accounts = ACCOUNTS

    def __init__(self, app, db, ns):
        self.app, self.db, self.ns = app, db, ns
        self.__dict__.update(_models(db))
        self._insert_token = object()
        self._mutation_token = object()

    @staticmethod
    def today():
        # Yangon has no DST; avoid relying on OS tzdata on Windows.
        return datetime.now(timezone(timedelta(hours=6, minutes=30))).date()

    @staticmethod
    def external_date(value):
        if type(value) is Date:
            return value
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise AccountingError('Date must be YYYY-MM-DD')
        try:
            return Date.fromisoformat(value)
        except ValueError:
            raise AccountingError('Invalid calendar date') from None

    def config(self):
        with self.db.session.no_autoflush:
            pending = next((o for o in self.db.session.new if isinstance(o, self.Config)), None)
            if pending is not None:
                return pending
            if self.db.session.info.get('_accounting_config_missing'):
                return None
            config = self.db.session.get(self.Config, 1)
            if config is None:
                self.db.session.info['_accounting_config_missing'] = True
            return config

    def require_config(self):
        config = self.config()
        if config is None:
            raise AccountingError('Accounting must be explicitly activated first', 409)
        if config.currency != self.ns['get_currency_code']():
            raise AccountingError('Accounting currency cannot change after activation', 409)
        return config

    def validate_date(self, value):
        value = self.external_date(value)
        config = self.require_config()
        if value < config.start_date or value > self.today():
            raise AccountingError('Posting date must be between activation and today')
        if config.locked_through and value <= config.locked_through:
            raise AccountingError('This accounting period is locked', 409)
        return value

    def location(self, branch_id):
        if branch_id is not None:
            _integer(branch_id, 'Branch')
        with self.db.session.no_autoflush:
            for obj in self.db.session.new:
                if isinstance(obj, self.Location) and obj.branch_id == branch_id:
                    return obj
            result = self.Location.query.filter_by(branch_id=branch_id).first()
        if result is None:
            raise AccountingError('Branch has no configured accounting location', 409)
        return result

    def require_location(self, location_id, active=False):
        _integer(location_id, 'Location')
        result = self.db.session.get(self.Location, location_id)
        if result is None:
            raise AccountingError('Accounting location not found', 404)
        if active and result.branch_id is not None:
            branch = self.db.session.get(self.ns['Branch'], result.branch_id)
            if branch is None or not branch.is_active:
                raise AccountingError('This branch is not active', 403)
        return result

    def write_lock(self):
        """Begin a fresh serialized writer after the request's authentication reads."""
        if self.db.session.new or self.db.session.dirty or self.db.session.deleted:
            raise AccountingError('Cannot acquire accounting lock with pending changes', 409)
        self.db.session.rollback()
        if self.db.engine.dialect.name != 'sqlite':
            raise AccountingError('Accounting writes require the configured SQLite writer lock', 503)
        self.db.session.execute(text('BEGIN IMMEDIATE'))

    def _add_immutable(self, obj):
        obj._accounting_insert_token = self._insert_token
        if isinstance(obj, self.Config):
            self.db.session.info['_accounting_config_missing'] = False
        self.db.session.add(obj)
        return obj

    def _mutate(self, obj, **values):
        mapper = inspect(type(obj))
        model = mapper.class_
        if model not in self._mutable_columns or not values or not values.keys() <= self._mutable_columns[model]:
            raise AccountingError('Accounting field cannot be changed', 409)
        identity = inspect(obj).identity
        if identity is None or obj in self.db.session.deleted:
            raise AccountingError('Accounting record is not available for update', 409)
        criteria = [column == value for column, value in zip(mapper.primary_key, identity)]
        statement = model.__table__.update().where(*criteria).values(**values)
        self.db.session.execute(statement.execution_options(_accounting_guard_token=self._mutation_token))
        for key, value in values.items():
            attributes.set_committed_value(obj, key, value)
        return obj

    def _flush(self):
        # Source installers may call post from before_flush. SQLAlchemy will
        # persist the newly added journal and relationships in that same flush.
        if not self.db.session()._flushing:
            self.db.session.flush()

    def _find_source(self, cls, source_key):
        with self.db.session.no_autoflush:
            for obj in self.db.session.new:
                if isinstance(obj, cls) and obj.source_key == source_key:
                    return obj
            return cls.query.filter_by(source_key=source_key).first()

    def post(self, source_key, source_type, date, memo, lines, *, reversal_of=None, actor_id=None):
        config = self.require_config()
        source_key = _string(source_key, 'Source key', 200)
        source_type = _string(source_type, 'Source type', 48)
        date = self.external_date(date)
        memo = _string(memo, 'Memo', 1000, empty=True)
        if not isinstance(lines, list) or not 2 <= len(lines) <= 1000:
            raise AccountingError('A journal requires 2–1000 lines')
        cleaned, totals = [], defaultdict(int)
        with self.db.session.no_autoflush:
            for line in lines:
                _shape(line, ('location_id', 'account_code', 'amount_minor'), ('reference',))
                loc = self.require_location(line['location_id'])
                code = line['account_code']
                if not isinstance(code, str) or code not in ACCOUNTS:
                    raise AccountingError('Unknown account code')
                amount = _integer(line['amount_minor'], 'Minor units', -MAX_MINOR, MAX_MINOR)
                reference = line.get('reference')
                if reference is not None:
                    reference = _string(reference, 'Reference', 200, empty=True) or None
                if amount:
                    cleaned.append(dict(location_id=loc.id, account_code=code,
                                        amount_minor=amount, reference=reference))
                    totals[loc.id] += amount
        if len(cleaned) < 2 or any(totals.values()):
            raise AccountingError('Journal must balance separately at every location')
        canonical = lambda rows: sorted((r['location_id'], r['account_code'], r['amount_minor'], r.get('reference') or '') for r in rows)
        previous = self._find_source(self.Journal, source_key)
        if previous:
            previous_lines = [dict(location_id=l.location_id, account_code=l.account_code,
                                   amount_minor=l.amount_minor, reference=l.reference) for l in previous.lines]
            if (previous.source_type, previous.date, previous.memo, previous.currency, canonical(previous_lines)) != (
                    source_type, date, memo, config.currency, canonical(cleaned)):
                raise AccountingError('Source key was already used for different journal content', 409)
            return previous
        self.validate_date(date)
        journal = self.Journal(source_key=source_key, source_type=source_type, date=date,
                               memo=memo, currency=config.currency,
                               actor_id=actor_id if actor_id is not None else (
                                   session.get('user_id') if has_request_context() else None),
                               reversal_of=reversal_of)
        self._add_immutable(journal)
        for values in cleaned:
            line = self.Line(journal=journal, **values)
            self._add_immutable(line)
        self._flush()
        return journal

    def _post_reversal(self, original, source_key, date):
        source_key = _string(source_key, 'Reversal source key', 200)
        date = self.external_date(date)
        previous = self._find_source(self.Journal, source_key)
        lines = [dict(location_id=line.location_id, account_code=line.account_code,
                      amount_minor=-line.amount_minor, reference=line.reference)
                 for line in original.lines]
        if previous:
            if (previous.source_type, previous.date, previous.reversal_of, previous.memo, previous.currency,
                    sorted((line.location_id, line.account_code, line.amount_minor, line.reference or '')
                           for line in previous.lines)) != (
                    'reversal', date, original.id, 'Reversal of journal ' + str(original.id),
                    original.currency,
                    sorted((line['location_id'], line['account_code'], line['amount_minor'], line['reference'] or '')
                           for line in lines)):
                raise AccountingError('Reversal key was already used for different content', 409)
            return previous
        if self.Journal.query.filter_by(reversal_of=original.id).first():
            raise AccountingError('Journal was already reversed', 409)
        return self.post(source_key, 'reversal', date, 'Reversal of journal ' + str(original.id), lines,
                         reversal_of=original.id)

    def document(self, kind, source_key, location_id, date, total_minor, snapshot):
        self.require_config()
        if kind not in ('sales', 'sales-return', 'purchase', 'purchase-return'):
            raise AccountingError('Invalid document kind')
        source_key = _string(source_key, 'Document source key', 200)
        self.require_location(location_id)
        date = self.external_date(date)
        _integer(total_minor, 'Document total', 0, MAX_MINOR)
        if not isinstance(snapshot, dict):
            raise AccountingError('Document snapshot must be an object')
        try:
            encoded = json.dumps(snapshot, sort_keys=True, allow_nan=False, ensure_ascii=False)
            if len(encoded.encode('utf-8')) > 1_000_000:
                raise AccountingError('Document snapshot is too large', 413)
            snapshot = json.loads(encoded)
        except (TypeError, ValueError, RecursionError):
            raise AccountingError('Document snapshot must contain finite JSON values') from None
        previous = self._find_source(self.Document, source_key)
        if previous:
            if (previous.kind, previous.location_id, previous.date, previous.total_minor, previous.snapshot) != (
                    kind, location_id, date, total_minor, snapshot):
                raise AccountingError('Document source key was already used for different content', 409)
            return previous
        self.validate_date(date)
        doc = self.Document(kind=kind, source_key=source_key, location_id=location_id,
                            date=date, total_minor=total_minor, snapshot=snapshot)
        self._add_immutable(doc)
        self._flush()
        return doc

    def journal_dict(self, journal):
        return dict(id=journal.id, date=journal.date.isoformat(), memo=journal.memo,
                    source_type=journal.source_type, source_key=journal.source_key,
                    currency=journal.currency, reversal_of=journal.reversal_of,
                    lines=[dict(location_id=l.location_id, account_code=l.account_code,
                                debit=money(max(l.amount_minor, 0)), credit=money(max(-l.amount_minor, 0)),
                                reference=l.reference) for l in journal.lines])

    def asset_dict(self, asset):
        return dict(id=asset.id, location_id=asset.location_id, name=asset.name, category=asset.category,
                    acquired_on=asset.acquired_on.isoformat(), cost=money(asset.cost_minor),
                    salvage_value=money(asset.salvage_minor), life_months=asset.life_months,
                    accumulated_depreciation=money(asset.accumulated_minor),
                    carrying_value=money(0 if asset.status == 'disposed' else asset.cost_minor - asset.accumulated_minor),
                    status=asset.status, last_depreciation_date=asset.last_depreciation_date.isoformat() if asset.last_depreciation_date else None)

    def payable_dict(self, payable):
        supplier = self.db.session.get(self.ns['Supplier'], payable.supplier_id)
        return dict(id=payable.id, location_id=payable.location_id, supplier_id=payable.supplier_id,
                    supplier_name=supplier.name if supplier else 'Deleted supplier', reference=payable.reference,
                    invoice_date=payable.invoice_date.isoformat(),
                    due_date=payable.due_date.isoformat() if payable.due_date else None,
                    total=money(payable.total_minor), balance=money(payable.balance_minor))

    def document_dict(self, doc, detail=False):
        result = dict(id=doc.id, source_key=doc.source_key, number=doc.number, kind=doc.kind,
                      date=doc.date.isoformat(), location_id=doc.location_id, total=money(doc.total_minor))
        if detail:
            # Never use live products/prices to reconstruct historical documents.
            result['snapshot'] = doc.snapshot
        return result

    def statement(self, kind, location_id=None, start=None, end=None):
        config = self.require_config()
        if kind not in ('profit-loss', 'balance-sheet', 'trial-balance'):
            raise AccountingError('Invalid statement kind')
        if location_id is not None:
            self.require_location(location_id)
        start = self.external_date(start) if start is not None else config.start_date
        end = self.external_date(end) if end is not None else self.today()
        if start < config.start_date or end < start or end > self.today():
            raise AccountingError('Invalid statement date range')
        # Balance sheet and trial balance are as-of reports, not period movement.
        since = start if kind == 'profit-loss' else config.start_date
        query = self.db.session.query(self.Line.account_code, func.sum(self.Line.amount_minor)).join(
            self.Journal, self.Line.journal_id == self.Journal.id).filter(
                self.Journal.date >= since, self.Journal.date <= end)
        if location_id is not None:
            query = query.filter(self.Line.location_id == location_id)
        balances = dict(query.group_by(self.Line.account_code).all())
        rows, totals = [], {}
        income = -sum(balances.get(code, 0) for code, a in ACCOUNTS.items() if a['type'] == 'income')
        expenses = sum(balances.get(code, 0) for code, a in ACCOUNTS.items() if a['type'] == 'expense')
        for code, account in ACCOUNTS.items():
            amount = int(balances.get(code, 0))
            category = account['type']
            if kind == 'profit-loss' and category not in ('income', 'expense'):
                continue
            if kind == 'balance-sheet' and category not in ('asset', 'liability', 'equity'):
                continue
            value = -amount if category in ('income', 'liability', 'equity') and kind != 'trial-balance' else amount
            row = dict(section=category, code=code, label=account['name'], amount=money(value))
            if kind == 'trial-balance':
                row.update(debit=money(max(amount, 0)), credit=money(max(-amount, 0)))
            rows.append(row)
        if kind == 'profit-loss':
            totals = dict(income=income, expenses=expenses, net_profit=income - expenses)
        elif kind == 'balance-sheet':
            assets = sum(balances.get(c, 0) for c, a in ACCOUNTS.items() if a['type'] == 'asset')
            liabilities = -sum(balances.get(c, 0) for c, a in ACCOUNTS.items() if a['type'] == 'liability')
            retained = income - expenses
            equity = -sum(balances.get(c, 0) for c, a in ACCOUNTS.items() if a['type'] == 'equity') + retained
            rows.append(dict(section='equity', code='RETAINED_EARNINGS', label='Current retained earnings', amount=money(retained)))
            totals = dict(assets=assets, liabilities=liabilities, equity=equity,
                          retained_earnings=retained, liabilities_and_equity=liabilities + equity,
                          difference=assets - liabilities - equity)
        else:
            debits = sum(max(v, 0) for v in balances.values())
            credits = sum(max(-v, 0) for v in balances.values())
            totals = dict(debit=debits, credit=credits, difference=debits - credits)
        return dict(title={'profit-loss': 'Profit and loss', 'balance-sheet': 'Balance sheet',
                           'trial-balance': 'Trial balance'}[kind], kind=kind, currency=config.currency,
                    start=since.isoformat(), end=end.isoformat(), location_id=location_id,
                    rows=rows, totals={k: money(int(v)) for k, v in totals.items()})


def _install_guards(service):
    """Guard ORM, legacy bulk methods and direct SQL; schema drop_all is allowed."""
    db = service.db
    if getattr(db, '_accounting_guards', False):
        return
    db._accounting_guards = True
    classes = (service.Journal, service.Line, service.Document, service.Command,
               service.Asset, service.Payable, service.Layer, service.Allocation,
               service.Operation, service.Config, service.Location)
    protected = {cls.__tablename__ for cls in classes}
    mutable_columns = {
        service.Asset: {'accumulated_minor', 'status', 'last_depreciation_date'},
        service.Payable: {'balance_minor'},
        service.Layer: {'remaining', 'remaining_minor'},
        service.Allocation: {'returned', 'returned_minor'},
        service.Config: {'locked_through'},
        service.Location: {'name'},
    }
    service._mutable_columns = mutable_columns
    token = object()

    @event.listens_for(Session, 'before_flush')
    def validate_flush(s, context, instances):
        objects = list(s.new) + list(s.dirty) + list(s.deleted)
        relevant = [o for o in objects if isinstance(o, classes)]
        if not relevant:
            return
        active = service if has_app_context() and current_app.extensions.get('accounting') is service else None
        for obj in relevant:
            if obj in s.deleted:
                raise AccountingError('Accounting records cannot be deleted', 409)
            if obj in s.new:
                if active is None or getattr(obj, '_accounting_insert_token', None) is not active._insert_token:
                    raise AccountingError('Accounting records must be created through the accounting service', 409)
                if isinstance(obj, service.Line) and obj.journal not in s.new:
                    raise AccountingError('Cannot append lines to an existing journal', 409)
            elif s.is_modified(obj, include_collections=False):
                raise AccountingError('Accounting records cannot be changed outside an authorized operation', 409)
        # Only ORM flush INSERTs are authorized. Bulk SQL cannot enter this path.
        connection = s.connection()
        connection.info['_accounting_insert_tables'] = protected
        s.info['_accounting_guard_connection'] = connection

    @event.listens_for(Session, 'after_flush_postexec')
    def after_flush(s, context):
        connection = s.info.pop('_accounting_guard_connection', None)
        if connection is not None and not connection.closed:
            connection.info.pop('_accounting_insert_tables', None)

    @event.listens_for(Session, 'after_commit')
    @event.listens_for(Session, 'after_rollback')
    def clear_config_cache(session):
        session.info.pop('_accounting_config_missing', None)

    @event.listens_for(service.Document, 'after_insert')
    def number_document(mapper, connection, target):
        prefix = {'sales': 'SI', 'sales-return': 'SR', 'purchase': 'PI', 'purchase-return': 'PR'}[target.kind]
        number = f'{prefix}-{target.id:08d}'
        connection.execute(service.Document.__table__.update().where(service.Document.id == target.id).values(
            number=number).execution_options(_accounting_guard_token=token))
        attributes.set_committed_value(target, 'number', number)

    with service.app.app_context():
        engine = db.engine

    @event.listens_for(engine, 'rollback')
    @event.listens_for(engine, 'commit')
    def clear_connection(connection):
        connection.info.pop('_accounting_insert_tables', None)

    @event.listens_for(engine, 'before_cursor_execute')
    def guard_sql(connection, cursor, statement, parameters, context, executemany):
        if (context.execution_options.get('_accounting_guard_token') is service._mutation_token
                or context.execution_options.get('_accounting_guard_token') is token):
            return
        compiled = getattr(context, 'compiled', None)
        obj = getattr(compiled, 'statement', None)
        table = getattr(getattr(obj, 'table', None), 'name', None)
        if table in protected and getattr(obj, 'is_insert', False):
            if table in connection.info.get('_accounting_insert_tables', ()):
                return
            raise AccountingError('Bulk inserts into accounting records are prohibited', 409)
        if table in protected and (getattr(obj, 'is_update', False) or getattr(obj, 'is_delete', False)):
            raise AccountingError('Accounting records are immutable', 409)
        # Catch textual/driver SQL too, including CTE and REPLACE writes. Strip
        # comments and string literals; DDL (including test drop_all) is untouched.
        sql = re.sub(r"'(?:''|[^'])*'|/\*[\s\S]*?\*/|--[^\r\n]*", ' ', statement).lower()
        if re.search(r'\b(insert|update|delete|replace)\b', sql) and any(
                re.search(r'\b' + name + r'\b', sql) for name in protected):
            if table in protected and getattr(obj, 'is_insert', False):
                return
            raise AccountingError('Direct accounting record writes are prohibited', 409)


def _register_routes(service):
    s, db = service, service.db
    bp = Blueprint('accounting', __name__, url_prefix='/api/accounting')
    authorize = s.ns['manager_or_boss_required']

    def route(path, methods=('GET',), setup=False):
        def decorate(fn):
            @wraps(fn)
            @authorize
            def wrapped(*args, **kwargs):
                try:
                    if not setup:
                        s.require_config()
                    return fn(*args, **kwargs)
                except AccountingError:
                    db.session.rollback()
                    raise
                except IntegrityError:
                    db.session.rollback()
                    raise AccountingError('Duplicate reference or conflicting accounting record', 409) from None
                except OperationalError:
                    db.session.rollback()
                    raise AccountingError('Accounting database is busy or unavailable; retry the same key', 409) from None
                except Exception:
                    db.session.rollback()
                    raise
            bp.add_url_rule(path, fn.__name__, wrapped, methods=list(methods))
            return wrapped
        return decorate

    @bp.after_request
    def no_store(response):
        response.headers['Cache-Control'] = 'no-store'
        return response

    def payload(required, optional=()):
        if request.content_length and request.content_length > 1_000_000:
            raise AccountingError('Request is too large', 413)
        return _shape(request.get_json(silent=True), required, optional)

    def query_fields(allowed):
        identity_fields = {'pos_user_id', 'pos_role', 'pos_branch_id'}
        if (set(request.args) - set(allowed) - identity_fields
                or any(len(request.args.getlist(k)) != 1 for k in request.args)):
            raise AccountingError('Invalid or repeated query parameters')

    def location_filter():
        value = request.args.get('location_id')
        if value in (None, ''):
            return None
        if not re.fullmatch(r'[1-9][0-9]{0,9}', value):
            raise AccountingError('Location must be a positive integer')
        return s.require_location(int(value)).id

    def page_query(query, serializer):
        def positive_param(name, default, maximum):
            value = request.args.get(name, str(default))
            if not re.fullmatch(r'[1-9][0-9]{0,8}', value) or int(value) > maximum:
                raise AccountingError(f'Invalid {name}')
            return int(value)
        page = positive_param('page', 1, 10_000_000)
        size = positive_param('per_page', 50, 100)
        total = query.count()
        return dict(items=[serializer(o) for o in query.offset((page - 1) * size).limit(size).all()],
                    page=page, total_pages=(total + size - 1) // size, total=total)

    def scoped(query, model, location_id):
        return query.filter(model.location_id == location_id) if location_id is not None else query

    def positive_money(value):
        result = parse_money(value)
        if not result:
            raise AccountingError('Amount must be greater than zero')
        return result

    def cash_account(value):
        if value not in ('CASH', 'BANK'):
            raise AccountingError('Payment account must be CASH or BANK')
        return value

    def expense_account(value):
        if not isinstance(value, str) or value not in ACCOUNTS or ACCOUNTS[value]['type'] != 'expense' or ACCOUNTS[value]['control']:
            raise AccountingError('Choose a non-control expense account')
        return value

    def lines(location_id, *entries):
        return [dict(location_id=location_id, account_code=code, amount_minor=amount,
                     reference=reference) for code, amount, reference in entries if amount]

    def command(action, data, operation, natural_key=None, setup=False):
        key = natural_key if natural_key is not None else _string(data.get('key'), 'Idempotency key', 128)
        receipt_key = f'{action}:{key}'
        fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, allow_nan=False,
                                                separators=(',', ':')).encode()).hexdigest()
        s.write_lock()
        # Authentication session may have become stale since the decorator ran.
        if 'User' in s.ns:
            actor = db.session.get(s.ns['User'], session.get('user_id'))
            if actor is None or actor.role not in ('manager', 'boss'):
                raise AccountingError('Manager or boss access required', 403)
        if not setup:
            s.require_config()
        previous = s.Command.query.filter_by(key=receipt_key).first()
        if previous:
            if previous.fingerprint != fingerprint:
                raise AccountingError('Idempotency key was already used for a different request', 409)
            result = dict(previous.response, replayed=True)
            db.session.rollback()
            return jsonify(result)
        result = operation()
        if isinstance(result, s.Journal):
            result = {'journal': s.journal_dict(result)}
        elif isinstance(result, s.Document):
            result = {'document': s.document_dict(result, True)}
        if not isinstance(result, dict):
            raise AccountingError('Accounting operation returned an invalid response', 500)
        result = dict(result, success=True)
        s._add_immutable(s.Command(key=receipt_key, fingerprint=fingerprint, response=result))
        db.session.commit()
        return jsonify(result)

    @route('/status', setup=True)
    def status():
        query_fields(())
        config = s.config()
        return jsonify(configured=config is not None, currency=config.currency if config else s.ns['get_currency_code'](),
                       start_date=config.start_date.isoformat() if config else None,
                       locked_through=config.locked_through.isoformat() if config and config.locked_through else None,
                       locations=[dict(id=l.id, branch_id=l.branch_id, kind=l.kind, name=l.name)
                                  for l in s.Location.query.order_by(s.Location.id).all()],
                       accounts=list(ACCOUNTS.values()))

    @route('/opening', setup=True)
    def opening():
        query_fields(())
        return jsonify(s.opening_preview())

    @route('/setup', ('POST',), setup=True)
    def setup():
        data = payload(('confirm', 'stock_costs_reviewed', 'opening_balances', 'payables'), ('key',))
        if data['confirm'] is not True or data['stock_costs_reviewed'] is not True:
            raise AccountingError('Opening balances and stock costs require explicit review')
        for name in ('opening_balances', 'payables'):
            if not isinstance(data[name], list) or len(data[name]) > 1000:
                raise AccountingError(f'{name} must be a list of at most 1000 items')
        return command('setup', data, lambda: s.activate(data), natural_key='activation', setup=True)

    def journals_result(expenses=False):
        query_fields(('location_id', 'page', 'per_page', 'start', 'end'))
        location_id = location_filter()
        query = s.Journal.query.options(selectinload(s.Journal.lines))
        if expenses:
            query = query.filter(s.Journal.source_type == 'expense')
        if location_id is not None:
            query = query.filter(s.Journal.lines.any(s.Line.location_id == location_id))
        start = s.external_date(request.args['start']) if request.args.get('start') else None
        end = s.external_date(request.args['end']) if request.args.get('end') else None
        if start and end and start > end:
            raise AccountingError('Start date must not follow end date')
        if start:
            query = query.filter(s.Journal.date >= start)
        if end:
            query = query.filter(s.Journal.date <= end)
        def serialize(journal):
            result = s.journal_dict(journal)
            if location_id is not None:
                result['lines'] = [l for l in result['lines'] if l['location_id'] == location_id]
            if expenses:
                expense = next((line for line in result['lines']
                                if line['account_code'] not in ('CASH', 'BANK')), None)
                payment = next((line for line in result['lines']
                                if line['account_code'] in ('CASH', 'BANK')), None)
                payee, separator, description = journal.memo.partition(': ')
                result.update(payee=payee, description=description if separator else '',
                              account_code=expense['account_code'] if expense else None,
                              paid_from=payment['account_code'] if payment else None,
                              amount=expense['debit'] if expense and expense['debit'] != '0.00' else
                                     (expense['credit'] if expense else '0.00'),
                              location_id=expense['location_id'] if expense else None)
            return result
        return jsonify(page_query(query.order_by(s.Journal.date.desc(), s.Journal.id.desc()), serialize))

    @route('/journals')
    def journals():
        return journals_result()

    @route('/journals', ('POST',))
    def create_journal():
        data = payload(('key', 'date', 'memo', 'lines'))
        def perform():
            if not isinstance(data['lines'], list) or not 2 <= len(data['lines']) <= 1000:
                raise AccountingError('A journal requires 2–1000 lines')
            values = []
            for row in data['lines']:
                _shape(row, ('location_id', 'account_code', 'debit', 'credit'))
                loc = s.require_location(row['location_id'], active=True)
                code = row['account_code']
                if not isinstance(code, str) or code not in ACCOUNTS or ACCOUNTS[code]['control']:
                    raise AccountingError('Manual journals cannot use control, opening or interbranch accounts')
                debit, credit = parse_money(row['debit']), parse_money(row['credit'])
                if bool(debit) == bool(credit):
                    raise AccountingError('Each line must have exactly one nonzero debit or credit')
                values.append(dict(location_id=loc.id, account_code=code, amount_minor=debit - credit))
            return s.post('manual:' + data['key'], 'manual', data['date'], data['memo'], values)
        return command('manual', data, perform)

    @route('/journals/<int:journal_id>/reverse', ('POST',))
    def reverse_journal(journal_id):
        data = payload(('key', 'date'))
        def perform():
            original = db.session.get(s.Journal, journal_id)
            if original is None:
                raise AccountingError('Journal not found', 404)
            if original.source_type not in ('manual', 'expense') or original.reversal_of is not None:
                raise AccountingError('Only original manual journals and expenses may be reversed', 409)
            if s.Journal.query.filter_by(reversal_of=journal_id).first():
                raise AccountingError('Journal was already reversed', 409)
            date = s.validate_date(data['date'])
            if date < original.date:
                raise AccountingError('Reversal cannot precede the original journal')
            for line in original.lines:
                s.require_location(line.location_id, active=True)
            return s._post_reversal(original, 'reversal:' + data['key'], date)
        return command('reversal', dict(data, journal_id=journal_id), perform)

    @route('/expenses')
    def expenses():
        return journals_result(True)

    @route('/expenses', ('POST',))
    def create_expense():
        data = payload(('key', 'date', 'location_id', 'account_code', 'amount', 'paid_from', 'payee', 'description'))
        def perform():
            loc = s.require_location(data['location_id'], active=True)
            amount = positive_money(data['amount'])
            code, cash = expense_account(data['account_code']), cash_account(data['paid_from'])
            payee = _string(data['payee'], 'Payee', 160)
            description = _string(data['description'], 'Description', 600)
            return s.post('expense:' + data['key'], 'expense', data['date'], f'{payee}: {description}',
                          lines(loc.id, (code, amount, payee), (cash, -amount, payee)))
        return command('expense', data, perform)

    @route('/assets')
    def assets():
        query_fields(('location_id', 'page', 'per_page'))
        return jsonify(page_query(scoped(s.Asset.query, s.Asset, location_filter()).order_by(s.Asset.id.desc()), s.asset_dict))

    @route('/assets', ('POST',))
    def create_asset():
        data = payload(('key', 'date', 'location_id', 'name', 'category', 'cost', 'salvage_value', 'life_months', 'paid_from'))
        def perform():
            loc = s.require_location(data['location_id'], active=True)
            date = s.validate_date(data['date'])
            cost, salvage = positive_money(data['cost']), parse_money(data['salvage_value'])
            if salvage > cost:
                raise AccountingError('Salvage value cannot exceed cost')
            paid_from = data['paid_from']
            if paid_from == 'OPENING_EQUITY':
                if date != s.config().start_date:
                    raise AccountingError('Opening assets must be dated on the activation date')
            else:
                cash_account(paid_from)
            asset = s.Asset(location_id=loc.id, acquired_on=date, name=_string(data['name'], 'Asset name', 160),
                            category=_string(data['category'], 'Category', 100), cost_minor=cost,
                            salvage_minor=salvage, life_months=_integer(data['life_months'], 'Useful life', 1, 1200),
                            accumulated_minor=0, status='active')
            s._add_immutable(asset)
            db.session.flush()
            reference = f'asset:{asset.id}'
            journal = s.post('asset:' + data['key'], 'asset-acquisition', date,
                             ('Reviewed opening asset: ' if paid_from == 'OPENING_EQUITY' else 'Asset acquisition: ') + asset.name,
                             lines(loc.id, ('FIXED_ASSETS', cost, reference), (paid_from, -cost, reference)))
            return dict(asset=s.asset_dict(asset), journal=s.journal_dict(journal))
        return command('asset', data, perform)

    def get_asset(asset_id, date):
        asset = db.session.get(s.Asset, asset_id)
        if asset is None:
            raise AccountingError('Asset not found', 404)
        s.require_location(asset.location_id, active=True)
        if asset.status != 'active':
            raise AccountingError('Asset is already disposed', 409)
        if date < asset.acquired_on or (asset.last_depreciation_date and date < asset.last_depreciation_date):
            raise AccountingError('Asset posting cannot precede acquisition or the last depreciation')
        return asset

    @route('/assets/<int:asset_id>/depreciate', ('POST',))
    def depreciate_asset(asset_id):
        data = payload(('key', 'date', 'amount'))
        def perform():
            date = s.validate_date(data['date'])
            asset = get_asset(asset_id, date)
            amount = positive_money(data['amount'])
            if amount > asset.cost_minor - asset.salvage_minor - asset.accumulated_minor:
                raise AccountingError('Depreciation exceeds the remaining depreciable value')
            reference = f'asset:{asset.id}'
            journal = s.post('depreciation:' + data['key'], 'asset-depreciation', date, 'Reviewed manual depreciation: ' + asset.name,
                             lines(asset.location_id, ('DEPRECIATION', amount, reference), ('ACCUM_DEPR', -amount, reference)))
            s._mutate(asset, accumulated_minor=asset.accumulated_minor + amount,
                      last_depreciation_date=date)
            return dict(asset=s.asset_dict(asset), journal=s.journal_dict(journal))
        return command('depreciation', dict(data, asset_id=asset_id), perform)

    @route('/assets/<int:asset_id>/dispose', ('POST',))
    def dispose_asset(asset_id):
        data = payload(('key', 'date', 'proceeds', 'paid_to'))
        def perform():
            date = s.validate_date(data['date'])
            asset = get_asset(asset_id, date)
            proceeds, paid_to = parse_money(data['proceeds']), cash_account(data['paid_to'])
            ref = f'asset:{asset.id}'
            journal = s.post('disposal:' + data['key'], 'asset-disposal', date, 'Asset disposal: ' + asset.name,
                             lines(asset.location_id, (paid_to, proceeds, ref), ('FIXED_ASSETS', -asset.cost_minor, ref),
                                   ('ACCUM_DEPR', asset.accumulated_minor, ref),
                                   ('ASSET_DISPOSAL', asset.cost_minor - asset.accumulated_minor - proceeds, ref)))
            s._mutate(asset, status='disposed')
            return dict(asset=s.asset_dict(asset), journal=s.journal_dict(journal))
        return command('disposal', dict(data, asset_id=asset_id), perform)

    @route('/payables')
    def payables():
        query_fields(('location_id', 'page', 'per_page'))
        return jsonify(page_query(scoped(s.Payable.query, s.Payable, location_filter()).order_by(s.Payable.id.desc()), s.payable_dict))

    @route('/payables', ('POST',))
    def create_payable():
        data = payload(('key', 'date', 'supplier_id', 'reference', 'amount', 'expense_account'), ('due_date',))
        def perform():
            date = s.validate_date(data['date'])
            loc = s.location(None)
            supplier_id = _integer(data['supplier_id'], 'Supplier')
            supplier = db.session.get(s.ns['Supplier'], supplier_id)
            if supplier is None or not supplier.is_active:
                raise AccountingError('Active supplier not found', 404)
            amount, code = positive_money(data['amount']), expense_account(data['expense_account'])
            due = s.external_date(data['due_date']) if data.get('due_date') else None
            if due and due < date:
                raise AccountingError('Due date cannot precede invoice date')
            payable = s.Payable(location_id=loc.id, supplier_id=supplier_id,
                                reference=_string(data['reference'], 'Supplier reference', 120), invoice_date=date,
                                due_date=due, total_minor=amount, balance_minor=amount, source_key='bill:' + data['key'])
            s._add_immutable(payable)
            db.session.flush()
            reference = f'payable:{payable.id}'
            journal = s.post('bill:' + data['key'], 'supplier-bill', date, 'Supplier expense bill: ' + payable.reference,
                             lines(loc.id, (code, amount, reference), ('AP', -amount, reference)))
            return dict(payable=s.payable_dict(payable), journal=s.journal_dict(journal))
        return command('bill', data, perform)

    @route('/payables/<int:payable_id>/pay', ('POST',))
    def pay_supplier(payable_id):
        data = payload(('key', 'date', 'amount', 'paid_from'))
        def perform():
            payable = db.session.get(s.Payable, payable_id)
            if payable is None:
                raise AccountingError('Payable not found', 404)
            if payable.location_id != s.location(None).id:
                raise AccountingError('Supplier payables must belong to the central warehouse', 409)
            date = s.validate_date(data['date'])
            if date < payable.invoice_date:
                raise AccountingError('Payment cannot precede the supplier invoice')
            amount, cash = positive_money(data['amount']), cash_account(data['paid_from'])
            if amount > payable.balance_minor:
                raise AccountingError('Payment exceeds the payable balance')
            ref = f'payable:{payable.id}'
            journal = s.post('supplier-payment:' + data['key'], 'supplier-payment', date,
                             'Supplier payment: ' + payable.reference,
                             lines(payable.location_id, ('AP', amount, ref), (cash, -amount, ref)))
            s._mutate(payable, balance_minor=payable.balance_minor - amount)
            return dict(payable=s.payable_dict(payable), journal=s.journal_dict(journal))
        return command('supplier-payment', dict(data, payable_id=payable_id), perform)

    @route('/receivables')
    def receivables():
        query_fields(('location_id', 'page', 'per_page'))
        location_id = location_filter()
        Debt = s.ns['Debt']
        query = Debt.query.filter(Debt.balance > 0)
        if location_id is not None:
            loc = s.require_location(location_id)
            query = query.filter(Debt.branch_id == loc.branch_id) if loc.branch_id else query.filter(text('1=0'))
        def serialize(debt):
            return dict(id=debt.id, customer_name=debt.customer.name if debt.customer else 'Deleted customer',
                        branch_id=debt.branch_id, sale_id=debt.sale_id, amount=money(parse_money(str(debt.amount))),
                        balance=money(parse_money(str(debt.balance))),
                        due_date=debt.due_date.date().isoformat() if isinstance(debt.due_date, datetime) else (
                            debt.due_date.isoformat() if debt.due_date else None))
        return jsonify(page_query(query.order_by(Debt.id.desc()), serialize))

    @route('/receivables/<int:debt_id>/pay', ('POST',))
    def collect_receivable(debt_id):
        data = payload(('key', 'date', 'amount', 'paid_to'))
        def perform():
            Debt, DebtPayment = s.ns['Debt'], s.ns['DebtPayment']
            debt = db.session.get(Debt, debt_id)
            if debt is None:
                raise AccountingError('Receivable not found', 404)
            loc = s.location(debt.branch_id)
            s.require_location(loc.id, active=True)
            date = s.external_date(data['date'])
            amount, cash = positive_money(data['amount']), cash_account(data['paid_to'])
            source_key, ref = 'collection:' + data['key'], f'debt:{debt.id}'
            previous = s.Journal.query.filter_by(source_key=source_key).first()
            if previous:
                actual = sorted((l.location_id, l.account_code, l.amount_minor, l.reference) for l in previous.lines)
                expected = sorted(((loc.id, 'AR', -amount, ref), (loc.id, cash, amount, ref)))
                if previous.date != date or actual != expected:
                    raise AccountingError('Collection key was already used for different content', 409)
                return dict(journal=s.journal_dict(previous), replayed=True)
            s.validate_date(date)
            debt_date = debt.date.date() if isinstance(debt.date, datetime) else debt.date
            if debt_date and date < debt_date:
                raise AccountingError('Collection cannot precede the receivable')
            balance = parse_money(str(debt.balance))
            if amount > balance:
                raise AccountingError('Collection exceeds the receivable balance')
            payment = DebtPayment(debt_id=debt.id, customer_id=debt.customer_id, amount=float(Decimal(amount) / 100),
                                  branch_id=debt.branch_id, processed_by=session.get('user_id'),
                                  payment_date=datetime.combine(date, datetime.min.time()), notes='Accounting collection')
            payment._accounting_key = data['key']
            payment._accounting_date = date
            payment._accounting_account = cash
            remaining = balance - amount
            debt.balance = float(Decimal(remaining) / 100)
            if not remaining:
                debt.status = 'paid'
            else:
                due = debt.due_date
                if isinstance(due, Date) and not isinstance(due, datetime):
                    due = datetime.combine(due, datetime.min.time())
                if due and due.tzinfo:
                    due = due.astimezone(timezone.utc).replace(tzinfo=None)
                debt.status = 'overdue' if due and datetime.utcnow() > due else 'partial'
            db.session.add(payment)
            db.session.flush()
            s.post_collection(payment)
            journal = s._find_source(s.Journal, source_key)
            if journal is None:
                raise AccountingError('Receivable collection integration did not post its journal', 500)
            db.session.flush()
            return dict(journal=s.journal_dict(journal), balance=money(balance - amount))
        return command('collection', dict(data, debt_id=debt_id), perform)

    @route('/inventory')
    def inventory():
        query_fields(('location_id',))
        return jsonify(s.inventory_report(location_filter()))

    @route('/inventory/adjust', ('POST',))
    def adjust_inventory():
        # Detailed adjustment shape is owned and validated by the FIFO installer.
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise AccountingError('Expected a JSON object')
        return command('inventory-adjust', data, lambda: s.adjust_inventory(data))

    @route('/purchase-returns', ('POST',))
    def purchase_return():
        data = payload(('key', 'date', 'payable_id', 'items', 'reason'), ('paid_to',))
        if not isinstance(data['items'], list) or not 1 <= len(data['items']) <= 500:
            raise AccountingError('Purchase return requires 1–500 items')
        _integer(data['payable_id'], 'Payable')
        _string(data['reason'], 'Return reason', 600)
        for item in data['items']:
            _shape(item, ('warehouse_inventory_id', 'quantity'))
            _integer(item['warehouse_inventory_id'], 'Warehouse inventory')
            _integer(item['quantity'], 'Quantity')
        return command('purchase-return', data, lambda: s.purchase_return(data))

    @route('/invoices')
    def invoices():
        query_fields(('kind', 'location_id', 'page', 'per_page'))
        query = scoped(s.Document.query, s.Document, location_filter())
        kind = request.args.get('kind')
        if kind:
            if kind not in ('sales', 'sales-return', 'purchase', 'purchase-return'):
                raise AccountingError('Invalid document kind')
            query = query.filter_by(kind=kind)
        return jsonify(page_query(query.order_by(s.Document.date.desc(), s.Document.id.desc()), s.document_dict))

    def get_document(document_id):
        doc = db.session.get(s.Document, document_id)
        if doc is None:
            raise AccountingError('Document not found', 404)
        s.require_location(doc.location_id)
        return doc

    @route('/invoices/<int:document_id>')
    def invoice(document_id):
        query_fields(())
        return jsonify(s.document_dict(get_document(document_id), True))

    def export_report(title, rows, columns, meta, totals=None):
        from reports import build_report_pdf, build_report_xlsx, column
        fmt = request.args.get('format', 'pdf')
        if fmt not in ('pdf', 'xlsx'):
            raise AccountingError('Export format must be pdf or xlsx')
        # Text money columns intentionally preserve exact Decimal strings: the
        # generic reports money formatter converts to float. Builders escape PDF
        # text and disable Excel formulas/hyperlinks for all string cells.
        report = dict(title=title, subtitle='Accrual accounting · ' + s.config().currency,
                      sheet_name='Accounting', file_stem='accounting',
                      columns=[column(key, label, width=width, align=align) for key, label, width, align in columns],
                      rows=rows, meta=meta, totals={}, summary=[dict(label=k.replace('_', ' ').title(), value=v)
                                                             for k, v in (totals or {}).items()],
                      notes=['Amounts are exact decimal text in the accounting currency.'], currency_suffix=s.config().currency)
        content = build_report_pdf(report) if fmt == 'pdf' else build_report_xlsx(report)
        response = current_app.response_class(content, mimetype='application/pdf' if fmt == 'pdf' else
                                               'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response.headers['Content-Disposition'] = f'attachment; filename="accounting.{fmt}"'
        return response

    @route('/invoices/<int:document_id>/export')
    def export_invoice(document_id):
        query_fields(('format',))
        doc = get_document(document_id)
        # Flatten every frozen field, including unknown future source fields.
        rows = []
        def flatten(value, path=''):
            if isinstance(value, dict):
                for key, child in value.items():
                    flatten(child, f'{path}.{key}' if path else key)
            elif isinstance(value, list):
                for index, child in enumerate(value, 1):
                    flatten(child, f'{path}[{index}]')
            else:
                rows.append(dict(field=str(path), value='' if value is None else str(value)))
        flatten(doc.snapshot)
        return export_report(doc.number, rows, [('field', 'Field', 35, 'left'), ('value', 'Value', 65, 'left')],
                             [dict(label='Date', value=doc.date.isoformat()), dict(label='Kind', value=doc.kind),
                              dict(label='Total', value=money(doc.total_minor))])

    def statement_result():
        query_fields(('kind', 'location_id', 'start', 'end', 'format'))
        return s.statement(request.args.get('kind', 'profit-loss'), location_filter(),
                           request.args.get('start'), request.args.get('end'))

    @route('/statements')
    def statements():
        return jsonify(statement_result())

    @route('/statements/export')
    def export_statement():
        result = statement_result()
        columns = [('section', 'Section', 15, 'left'), ('code', 'Account', 22, 'left'), ('label', 'Name', 35, 'left')]
        columns += [('debit', 'Debit', 18, 'right'), ('credit', 'Credit', 18, 'right')] if result['kind'] == 'trial-balance' else [
            ('amount', 'Amount', 22, 'right')]
        return export_report(result['title'], result['rows'], columns,
                             [dict(label='From', value=result['start']), dict(label='Through', value=result['end']),
                              dict(label='Location', value=str(result['location_id'] or 'Consolidated'))], result['totals'])

    @route('/period-lock', ('POST',))
    def period_lock():
        data = payload(('through',), ('key',))
        through = s.external_date(data['through'])
        def perform():
            config = s.config()
            if config.locked_through and through < config.locked_through:
                raise AccountingError('Accounting periods cannot be unlocked', 409)
            if through < config.start_date or through > s.today():
                raise AccountingError('Lock date must be between activation and today', 400)
            balances = db.session.query(s.Line.location_id, func.sum(s.Line.amount_minor)).join(
                s.Journal, s.Line.journal_id == s.Journal.id).filter(s.Journal.date <= through).group_by(s.Line.location_id).all()
            if any(amount for _, amount in balances):
                raise AccountingError('Cannot lock an unbalanced trial balance', 409)
            s._mutate(config, locked_through=through)
            return dict(locked_through=through.isoformat())
        return command('period-lock', data, perform, natural_key=through.isoformat())

    service.app.register_blueprint(bp)


def register_accounting(app, db, ns):
    """Register once before create_all and return the shared accounting service."""
    if 'accounting' in app.extensions:
        return app.extensions['accounting']
    service = AccountingService(app, db, ns)
    app.extensions['accounting'] = service
    db.accounting = service

    @app.errorhandler(AccountingError)
    def accounting_error(error):
        db.session.rollback()
        response = jsonify(success=False, message=error.message)
        response.headers['Cache-Control'] = 'no-store'
        return response, error.status

    from accounting_operations import install_operations
    install_operations(service)
    _install_guards(service)
    _register_routes(service)
    return service
