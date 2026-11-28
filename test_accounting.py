"""Isolated accrual-ledger, FIFO, subledger and HTTP contract tests."""

from datetime import datetime, timedelta
from decimal import Decimal
from functools import wraps
import unittest
from unittest.mock import patch
import uuid

from flask import Flask, abort, jsonify, session
from sqlalchemy import text

from accounting import AccountingError, parse_money, register_accounting


def build_app(*, missing_cost=False):
    from flask_sqlalchemy import SQLAlchemy

    app = Flask(__name__)
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite://',
                      SQLALCHEMY_TRACK_MODIFICATIONS=False, SECRET_KEY='ledger-tests')
    db = SQLAlchemy(app)

    class Branch(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        name = db.Column(db.String(160), nullable=False)
        is_active = db.Column(db.Boolean, nullable=False, default=True)

    class Product(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        name = db.Column(db.String(160), nullable=False)
        branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'))
        stock = db.Column(db.Integer, nullable=False, default=0)
        cost = db.Column(db.Float)
        price = db.Column(db.Float, default=0)
        tax_rate = db.Column(db.Float, default=0)
        reorder_point = db.Column(db.Integer, default=10)
        reorder_quantity = db.Column(db.Integer, default=50)
        barcode = db.Column(db.String(120))
        sku = db.Column(db.String(100))
        category = db.Column(db.String(160))
        category_id = db.Column(db.Integer, db.ForeignKey('category.id'))

    class Category(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        name = db.Column(db.String(160), nullable=False)
        branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'))

    class WarehouseInventory(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        product_id = db.Column(db.Integer, nullable=False)
        quantity = db.Column(db.Integer, nullable=False)
        unit_cost = db.Column(db.Float)
        batch_number = db.Column(db.String(80))
        received_date = db.Column(db.DateTime, default=datetime.utcnow)

    class Supplier(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        name = db.Column(db.String(160), nullable=False)
        is_active = db.Column(db.Boolean, nullable=False, default=True)

    class Customer(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        name = db.Column(db.String(160), nullable=False)
        address = db.Column(db.String(200))
        phone = db.Column(db.String(40))

    class User(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        username = db.Column(db.String(80), nullable=False)
        role = db.Column(db.String(16), nullable=False)

    class Sale(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        transaction_id = db.Column(db.String(128), nullable=False, unique=True)
        branch_id = db.Column(db.Integer, nullable=False)
        customer_id = db.Column(db.Integer)
        user_id = db.Column(db.Integer)
        date = db.Column(db.DateTime, default=datetime.utcnow)
        total = db.Column(db.Float, nullable=False)
        tax = db.Column(db.Float, nullable=False, default=0)
        payment_method = db.Column(db.String(32), nullable=False)
        payment_breakdown = db.Column(db.Text)
        receipt_snapshot = db.Column(db.Text)
        cash_received = db.Column(db.Float)
        refund_amount = db.Column(db.Float, default=0)

    class SaleItem(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        sale_id = db.Column(db.Integer, nullable=False)
        product_id = db.Column(db.Integer, nullable=False)
        quantity = db.Column(db.Integer, nullable=False)
        price = db.Column(db.Float, nullable=False)
        tax = db.Column(db.Float, nullable=False, default=0)

    class Debt(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        customer_id = db.Column(db.Integer)
        sale_id = db.Column(db.Integer)
        branch_id = db.Column(db.Integer, nullable=False)
        amount = db.Column(db.Float, nullable=False)
        balance = db.Column(db.Float, nullable=False)
        date = db.Column(db.Date)
        due_date = db.Column(db.Date)
        status = db.Column(db.String(16), default='pending')
        communication_notes = db.Column(db.Text)

    class DebtPayment(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        debt_id = db.Column(db.Integer, nullable=False)
        customer_id = db.Column(db.Integer)
        amount = db.Column(db.Float, nullable=False)
        branch_id = db.Column(db.Integer, nullable=False)
        processed_by = db.Column(db.Integer)
        payment_date = db.Column(db.DateTime)
        notes = db.Column(db.String(300))

    class PurchaseOrder(db.Model):
        id = db.Column(db.Integer, primary_key=True)

    class PurchaseOrderItem(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        purchase_order_id = db.Column(db.Integer, db.ForeignKey('purchase_order.id'), nullable=False)
        received_qty = db.Column(db.Integer, nullable=False, default=0)
        unit_cost = db.Column(db.Float, nullable=False)

    class PurchaseInvoice(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        purchase_order_id = db.Column(db.Integer, nullable=False, unique=True)
        supplier_id = db.Column(db.Integer, nullable=False)
        supplier_reference = db.Column(db.String(120), nullable=False)
        invoice_date = db.Column(db.Date, nullable=False)
        total_minor = db.Column(db.Integer, nullable=False)
        currency = db.Column(db.String(12), nullable=False)
        snapshot = db.Column(db.JSON, nullable=False)

    class GoodsReceipt(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        purchase_order_id = db.Column(db.Integer, nullable=False)
        number = db.Column(db.String(80), nullable=False)
        snapshot = db.Column(db.JSON, nullable=False)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)

    class ReturnExchange(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        workflow_id = db.Column(db.String(80), nullable=False)
        mode = db.Column(db.String(16), nullable=False)
        original_sale_id = db.Column(db.Integer, nullable=False)
        adjustment_sale_id = db.Column(db.Integer)
        return_total = db.Column(db.Float, nullable=False)
        exchange_total = db.Column(db.Float, nullable=False, default=0)
        net_total = db.Column(db.Float, nullable=False)
        refund_amount = db.Column(db.Float, nullable=False)
        collected_amount = db.Column(db.Float, nullable=False, default=0)
        settlement_method = db.Column(db.String(32), nullable=False)
        notes = db.Column(db.String(300))
        user_id = db.Column(db.Integer)

    class ReturnExchangeItem(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        return_exchange_id = db.Column(db.Integer, nullable=False)
        original_sale_item_id = db.Column(db.Integer)
        product_id = db.Column(db.Integer, nullable=False)
        movement = db.Column(db.String(16), nullable=False)
        quantity = db.Column(db.Integer, nullable=False)
        unit_price = db.Column(db.Float, nullable=False)
        tax_rate = db.Column(db.Float, default=0)
        line_total = db.Column(db.Float, nullable=False)
        line_tax = db.Column(db.Float, default=0)

    class PaymentIntent(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        status = db.Column(db.String(24), nullable=False)
        sale_transaction_id = db.Column(db.String(128))

    def manager_or_boss_required(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if session.get('role') not in ('manager', 'boss'):
                abort(401)
            return fn(*args, **kwargs)
        return wrapped

    models = {model.__name__: model for model in (
        Branch, Product, Category, WarehouseInventory, Supplier, Customer, User, Sale, SaleItem,
        Debt, DebtPayment, PurchaseOrder, PurchaseOrderItem, PurchaseInvoice, GoodsReceipt,
        ReturnExchange, ReturnExchangeItem, PaymentIntent)}
    models.update(get_currency_code=lambda: 'USD', manager_or_boss_required=manager_or_boss_required)
    service = register_accounting(app, db, models)
    with app.app_context():
        db.create_all()
        manager = User(username='manager', role='manager')
        main = Branch(name='Main Shop')
        second = Branch(name='Second Shop')
        customer = Customer(name='Credit Customer')
        supplier = Supplier(name='Supplier')
        db.session.add_all([manager, main, second, customer, supplier])
        db.session.flush()
        product = Product(name='Main Product', branch_id=main.id, stock=10,
                          cost=None if missing_cost else 5.50)
        other = Product(name='Second Product', branch_id=second.id, stock=2, cost=2.00)
        batch = WarehouseInventory(product_id=product.id, quantity=4, unit_cost=3.25, batch_number='B-1')
        debt = Debt(customer_id=customer.id, branch_id=main.id, amount=20, balance=20, status='pending')
        db.session.add_all([product, other, debt])
        db.session.flush()
        batch = WarehouseInventory(product_id=product.id, quantity=4, unit_cost=3.25, batch_number='B-1')
        db.session.add(batch)
        db.session.commit()
        for row in (manager, main, second, customer, supplier, product, other, batch, debt):
            db.session.refresh(row)
        db.session.expunge_all()
        fixture = dict(user=manager, branch=main, second=second, customer=customer, supplier=supplier,
                       product=product, other=other, batch=batch, debt=debt)
    return app, db, models, service, fixture


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.app, self.db, self.models, self.service, self.f = build_app()
        self.context = self.app.app_context()
        self.context.push()
        self.f = {name: self.db.session.merge(row) for name, row in self.f.items()}
        self.db.session.commit()
        self.today = self.service.today()
        self.client = self.app.test_client()
        with self.client.session_transaction() as current:
            current.update(user_id=self.f['user'].id, username='manager', role='manager',
                           branch_id=self.f['branch'].id)
        response = self.client.post('/api/accounting/setup', json={
            'key': str(uuid.uuid4()), 'confirm': True, 'stock_costs_reviewed': True,
            'opening_balances': [{'branch_id': None, 'account_code': 'CASH', 'debit': '100.00', 'credit': '0.00'},
                                 {'branch_id': self.f['branch'].id, 'account_code': 'CASH', 'debit': '25.00', 'credit': '0.00'}],
            'payables': []})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def tearDown(self):
        self.db.session.remove()
        self.context.pop()

    def post(self, path, payload, *, status=200):
        response = self.client.post('/api/accounting' + path, json=payload)
        self.assertEqual(response.status_code, status, response.get_data(as_text=True))
        return response.get_json()

    def test_strict_money_and_missing_cost_activation(self):
        self.assertEqual(parse_money('999999999999.99'), 99_999_999_999_999)
        for value in (True, 1.25, '1.234', '-1.00', '1000000000000.00', 'NaN'):
            with self.subTest(value=value), self.assertRaises(AccountingError):
                parse_money(value)
        self.assertEqual(parse_money('-1.25', allow_negative=True), -125)
        app, db, _, service, fixture = build_app(missing_cost=True)
        with app.app_context():
            client = app.test_client()
            with client.session_transaction() as current:
                current.update(user_id=fixture['user'].id, role='manager', branch_id=fixture['branch'].id)
            response = client.post('/api/accounting/setup', json={
                'confirm': True, 'stock_costs_reviewed': True, 'opening_balances': [], 'payables': []})
            self.assertEqual(response.status_code, 409)
            self.assertIsNone(service.config())
            db.session.remove()

    def test_large_opening_splits_into_balanced_bounded_journals(self):
        app, db, _, service, fixture = build_app()
        with app.app_context():
            client = app.test_client()
            with client.session_transaction() as current:
                current.update(user_id=fixture['user'].id, role='manager', branch_id=fixture['branch'].id)
            payables = [{'supplier_id': fixture['supplier'].id, 'reference': f'OPEN-{index}',
                         'amount': '1.00'} for index in range(1000)]
            response = client.post('/api/accounting/setup', json={
                'key': str(uuid.uuid4()), 'confirm': True, 'stock_costs_reviewed': True,
                'opening_balances': [], 'payables': payables})
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            journals = service.Journal.query.filter_by(source_type='opening').all()
            self.assertGreaterEqual(len(journals), 2)
            self.assertTrue(all(2 <= len(journal.lines) <= 1000 for journal in journals))
            self.assertEqual(service.Line.query.filter_by(account_code='AP').count(), 1000)
            for journal in journals:
                totals = {}
                for row in journal.lines:
                    totals[row.location_id] = totals.get(row.location_id, 0) + row.amount_minor
                self.assertTrue(all(total == 0 for total in totals.values()))
            db.session.remove()

    def test_opening_chunks_keep_balancing_equity_within_money_limits(self):
        app, db, models, service, fixture = build_app()
        with app.app_context():
            db.session.get(models['WarehouseInventory'], fixture['batch'].id).quantity = 0
            db.session.commit()
            client = app.test_client()
            with client.session_transaction() as current:
                current.update(user_id=fixture['user'].id, role='manager', branch_id=fixture['branch'].id)
            payables = [{'supplier_id': fixture['supplier'].id, 'reference': f'MAX-{index}',
                         'amount': '999999999999.99'} for index in range(2)]
            response = client.post('/api/accounting/setup', json={
                'key': str(uuid.uuid4()), 'confirm': True, 'stock_costs_reviewed': True,
                'opening_balances': [], 'payables': payables})
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            journals = [journal for journal in service.Journal.query.filter_by(source_type='opening').all()
                        if any(row.account_code == 'AP' for row in journal.lines)]
            self.assertEqual(len(journals), 2)
            for journal in journals:
                self.assertEqual(len(journal.lines), 2)
                self.assertEqual(sum(row.amount_minor for row in journal.lines), 0)
                self.assertEqual(next(row.amount_minor for row in journal.lines
                                      if row.account_code == 'AP'), -99_999_999_999_999)
                self.assertEqual(next(row.amount_minor for row in journal.lines
                                      if row.account_code == 'OPENING_EQUITY'), 99_999_999_999_999)
            db.session.remove()

    def test_oversized_computed_opening_stock_is_rejected_before_setup(self):
        app, db, models, service, fixture = build_app()
        with app.app_context():
            product = db.session.get(models['Product'], fixture['product'].id)
            product.stock = 2147483647
            product.cost = 1000.00
            db.session.get(models['WarehouseInventory'], fixture['batch'].id).quantity = 0
            db.session.commit()
            client = app.test_client()
            with client.session_transaction() as current:
                current.update(user_id=fixture['user'].id, role='manager', branch_id=fixture['branch'].id)
            payload = {'key': str(uuid.uuid4()), 'confirm': True, 'stock_costs_reviewed': True,
                       'opening_balances': [], 'payables': []}
            for path in ('/api/accounting/opening', '/api/accounting/setup'):
                response = client.get(path) if path.endswith('/opening') else client.post(path, json=payload)
                self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
                self.assertIn('supported money limit', response.get_json()['message'])
            self.assertIsNone(service.config())
            self.assertEqual(service.Layer.query.count(), 0)
            self.assertEqual(service.Journal.query.count(), 0)
            db.session.remove()

    def test_opening_statements_exports_and_permissions(self):
        opening = self.client.get('/api/accounting/opening').get_json()
        self.assertEqual(opening['unbilled_receipts'], '0.00')
        main = next(row for row in opening['locations'] if row['branch_id'] == self.f['branch'].id)
        self.assertEqual((main['stock_value'], main['receivables']), ('55.00', '20.00'))
        balance = self.client.get('/api/accounting/statements', query_string={
            'kind': 'balance-sheet', 'start': self.today.isoformat(), 'end': self.today.isoformat()}).get_json()
        self.assertEqual(balance['totals']['assets'], '217.00')
        self.assertEqual(balance['totals']['difference'], '0.00')
        for fmt, signature in (('pdf', b'%PDF'), ('xlsx', b'PK')):
            response = self.client.get('/api/accounting/statements/export', query_string={
                'kind': 'profit-loss', 'start': self.today.isoformat(), 'end': self.today.isoformat(), 'format': fmt,
                'pos_user_id': str(self.f['user'].id), 'pos_role': 'manager',
                'pos_branch_id': str(self.f['branch'].id)})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.data.startswith(signature))
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
        repeated = self.client.get('/api/accounting/statements/export', query_string=[
            ('kind', 'profit-loss'), ('start', self.today.isoformat()), ('end', self.today.isoformat()),
            ('format', 'pdf'), ('pos_user_id', str(self.f['user'].id)),
            ('pos_user_id', str(self.f['user'].id))])
        self.assertEqual(repeated.status_code, 400)
        anon = self.app.test_client()
        self.assertEqual(anon.get('/api/accounting/status').status_code, 401)
        with self.client.session_transaction() as current:
            current['role'] = 'cashier'
        self.assertEqual(self.client.get('/api/accounting/status').status_code, 401)

    def _sale(self, *, product=None, quantity=3, total=33, tax=3):
        product = product or self.f['product']
        sale = self.models['Sale'](transaction_id='S-' + uuid.uuid4().hex, branch_id=self.f['branch'].id,
            customer_id=self.f['customer'].id, user_id=self.f['user'].id, total=total, tax=tax,
            payment_method='cash', receipt_snapshot='{"items": []}')
        self.db.session.add(sale)
        self.db.session.flush()
        item = self.models['SaleItem'](sale_id=sale.id, product_id=product.id, quantity=quantity,
                                       price=10, tax=tax)
        self.db.session.add(item)
        product.stock -= quantity
        self.db.session.flush()
        return sale, item

    def test_fifo_sale_cost_and_original_allocation_return(self):
        sale, item = self._sale()
        self.service.post_sale(sale)
        self.db.session.commit()
        journal = self.service.Journal.query.filter_by(source_key=f'sale:{sale.id}').one()
        cogs = next(line for line in journal.lines if line.account_code == 'COGS')
        self.assertEqual(cogs.amount_minor, 1650)
        workflow = self.models['ReturnExchange'](workflow_id=str(uuid.uuid4()), mode='return',
            original_sale_id=sale.id, return_total=33, exchange_total=0, net_total=-33,
            refund_amount=33, collected_amount=0, settlement_method='cash', user_id=self.f['user'].id)
        self.db.session.add(workflow)
        self.db.session.flush()
        self.db.session.add(self.models['ReturnExchangeItem'](return_exchange_id=workflow.id,
            original_sale_item_id=item.id, product_id=item.product_id, movement='return', quantity=1,
            unit_price=10, line_total=30, line_tax=3))
        return_item = self.models['ReturnExchangeItem'].query.filter_by(return_exchange_id=workflow.id).one()
        self.f['product'].stock += 1
        self.service.post_return(workflow)
        self.db.session.commit()
        return_item.quantity = 2
        with self.assertRaises(AccountingError):
            self.db.session.commit()
        self.db.session.rollback()
        self.db.session.delete(return_item)
        with self.assertRaises(AccountingError):
            self.db.session.commit()
        self.db.session.rollback()
        item.price = 11
        with self.assertRaises(AccountingError):
            self.db.session.commit()
        self.db.session.rollback()
        self.db.session.delete(item)
        with self.assertRaises(AccountingError):
            self.db.session.commit()
        self.db.session.rollback()
        allocation = self.service.Allocation.query.filter_by(sale_item_id=item.id).one()
        self.assertEqual((allocation.returned, allocation.returned_minor), (1, 550))
        inventory = self.service.inventory_report(self.service.location(self.f['branch'].id).id)
        self.assertEqual(inventory['total_value'], '44.00')
        trial = self.client.get('/api/accounting/statements', query_string={
            'kind': 'trial-balance', 'start': self.today.isoformat(), 'end': self.today.isoformat()}).get_json()
        self.assertEqual(trial['totals']['difference'], '0.00')
        sale.total = 99
        with self.assertRaises(AccountingError):
            self.db.session.commit()
        self.db.session.rollback()
        self.db.session.delete(self.db.session.get(self.models['Sale'], sale.id))
        with self.assertRaises(AccountingError):
            self.db.session.commit()

    def test_document_only_sale_is_immutable(self):
        sale = self.models['Sale'](transaction_id='S-' + uuid.uuid4().hex, branch_id=self.f['branch'].id,
            customer_id=self.f['customer'].id, user_id=self.f['user'].id, total=0, tax=0,
            payment_method='cash', receipt_snapshot='{"items": []}')
        self.db.session.add(sale)
        self.db.session.flush()
        self.service.post_sale(sale)
        self.db.session.commit()
        self.assertIsNone(self.service.Journal.query.filter_by(source_key=f'sale:{sale.id}').first())
        self.assertIsNotNone(self.service.Document.query.filter_by(source_key=f'sale:{sale.id}').first())
        sale.total = 1
        with self.assertRaises(AccountingError):
            self.db.session.commit()
        self.db.session.rollback()
        self.db.session.delete(sale)
        with self.assertRaises(AccountingError):
            self.db.session.commit()
        self.db.session.rollback()

    def test_pre_activation_debt_payment_history_is_immutable_after_activation(self):
        app, db, models, service, fixture = build_app()
        with app.app_context():
            rows = {name: db.session.merge(row) for name, row in fixture.items()}
            db.session.add(models['DebtPayment'](debt_id=rows['debt'].id, customer_id=rows['customer'].id,
                amount=2, branch_id=rows['branch'].id, processed_by=rows['user'].id,
                payment_date=datetime.utcnow(), notes='Historical collection'))
            db.session.commit()
            client = app.test_client()
            with client.session_transaction() as current:
                current.update(user_id=rows['user'].id, role='manager', branch_id=rows['branch'].id)
            response = client.post('/api/accounting/setup', json={
                'key': str(uuid.uuid4()), 'confirm': True, 'stock_costs_reviewed': True,
                'opening_balances': [], 'payables': []})
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            payment = models['DebtPayment'].query.one()
            payment.notes = 'Tampered history'
            with self.assertRaises(AccountingError):
                db.session.commit()
            db.session.rollback()
            db.session.delete(payment)
            with self.assertRaises(AccountingError):
                db.session.commit()
            db.session.rollback()
            db.session.remove()

    def test_only_customer_unlink_is_allowed_as_bulk_sale_update(self):
        sale, _ = self._sale()
        self.service.post_sale(sale)
        self.db.session.commit()
        document = self.service.Document.query.filter_by(source_key=f'sale:{sale.id}').one()
        self.assertEqual(document.snapshot['customer']['name'], 'Credit Customer')
        self.models['Sale'].query.filter_by(id=sale.id).update(
            {'customer_id': None}, synchronize_session=False)
        self.db.session.commit()
        self.assertIsNone(self.db.session.get(self.models['Sale'], sale.id).customer_id)
        self.assertEqual(document.snapshot['customer']['name'], 'Credit Customer')
        for values in ({'total': 99}, {'customer_id': None, 'total': 99}):
            with self.subTest(values=values), self.assertRaises(AccountingError):
                self.models['Sale'].query.filter_by(id=sale.id).update(values, synchronize_session=False)
            self.db.session.rollback()

    def test_legacy_return_requires_then_uses_reviewed_cost(self):
        sale, item = self._sale(quantity=0, total=0, tax=0)
        sale.payment_method = 'cash'
        workflow = self.models['ReturnExchange'](workflow_id=str(uuid.uuid4()), mode='return',
            original_sale_id=sale.id, return_total=10, exchange_total=0, net_total=-10,
            refund_amount=10, collected_amount=0, settlement_method='cash', user_id=self.f['user'].id)
        self.db.session.add(workflow)
        self.db.session.flush()
        self.db.session.add(self.models['ReturnExchangeItem'](return_exchange_id=workflow.id,
            original_sale_item_id=item.id, product_id=item.product_id, movement='return', quantity=1,
            unit_price=10, line_total=10, line_tax=0))
        self.f['product'].stock += 1
        with self.assertRaises(AccountingError):
            self.service.post_return(workflow)
        self.db.session.rollback()

        sale, item = self._sale(quantity=0, total=0, tax=0)
        workflow = self.models['ReturnExchange'](workflow_id=str(uuid.uuid4()), mode='return',
            original_sale_id=sale.id, return_total=10, exchange_total=0, net_total=-10,
            refund_amount=10, collected_amount=0, settlement_method='cash', user_id=self.f['user'].id)
        self.db.session.add(workflow)
        self.db.session.flush()
        self.db.session.add(self.models['ReturnExchangeItem'](return_exchange_id=workflow.id,
            original_sale_item_id=item.id, product_id=item.product_id, movement='return', quantity=1,
            unit_price=10, line_total=10, line_tax=0))
        self.f['product'].stock += 1
        self.service.post_return(workflow, legacy_costs={str(item.id): '5.50'})
        self.db.session.commit()
        layer = self.service.Layer.query.filter_by(source_key=f'return:{workflow.id}:{item.id}').first()
        self.assertIsNotNone(layer)
        self.assertEqual(layer.remaining_minor, 550)

    def test_central_transfer_preserves_fifo_cost(self):
        self.f['other'].stock += 2
        transfer = type('Transfer', (), dict(id=987, product_id=self.f['product'].id,
            target_product_id=self.f['other'].id, quantity=2, batch_number='B-1',
            branch_id=self.f['second'].id))()
        self.f['batch'].quantity = 2
        self.service.post_transfer(transfer)
        self.db.session.commit()
        central, shop = self.service.location(None), self.service.location(self.f['second'].id)
        self.assertEqual(self.service.inventory_report(central.id)['total_value'], '6.50')
        self.assertEqual(self.service.inventory_report(shop.id)['total_value'], '10.50')

    def test_inventory_adjustment_idempotency_and_fail_closed_stock(self):
        payload = {'key': 'adjust-1', 'date': self.today.isoformat(),
                   'location_id': self.service.location(self.f['branch'].id).id,
                   'product_id': self.f['product'].id, 'quantity': 2, 'unit_cost': '6.00',
                   'reason': 'Reviewed physical count'}
        result = self.post('/inventory/adjust', payload)
        self.assertEqual(result['quantity'], 12)
        self.assertTrue(self.post('/inventory/adjust', payload)['replayed'])
        self.post('/inventory/adjust', dict(payload, quantity=1), status=409)
        self.assertEqual(self.service.inventory_report()['total_value'], '84.00')
        self.f['product'].stock = 13
        with self.assertRaises(AccountingError):
            self.db.session.commit()
        self.db.session.rollback()

    def test_ai_product_stock_changes_post_fifo_and_preserve_omitted_stock(self):
        from ai_tools import AITools
        tools = AITools(self.db, self.models)
        tools.set_context({'user_id': self.f['user'].id, 'role': 'manager',
                           'branch_id': self.f['branch'].id})
        operations = self.service.Operation.query.count()
        catalog = tools.upsert_product('Main Product', price=12, cost=7.25,
                                       product_id=self.f['product'].id)
        self.assertTrue(catalog['success'], catalog)
        self.assertEqual(catalog['stock'], 10)
        self.assertNotIn('stock', catalog['changed_fields'])
        self.assertEqual(self.service.Operation.query.count(), operations)

        increased = tools.upsert_product('Main Product', price=12, cost=6,
                                         product_id=self.f['product'].id, stock=13)
        self.assertTrue(increased['success'], increased)
        self.assertEqual(increased['stock'], 13)
        adjusted = tools.adjust_product_stock(self.f['product'].id, 2, 'Reviewed count increase')
        self.assertTrue(adjusted['success'], adjusted)
        self.assertEqual(adjusted['new_stock'], 15)
        reduced = tools.adjust_product_stock(self.f['product'].id, -1, 'Reviewed count decrease')
        self.assertTrue(reduced['success'], reduced)
        self.assertEqual(reduced['new_stock'], 14)
        location = self.service.location(self.f['branch'].id)
        self.assertEqual(self.service.inventory_report(location.id)['total_value'], '79.50')
        journals = self.service.Journal.query.filter_by(source_type='stock-adjustment').order_by(
            self.service.Journal.id).all()
        self.assertEqual([next(line.amount_minor for line in journal.lines
                              if line.account_code == 'INVENTORY') for journal in journals],
                         [1800, 1200, -550])

        created = tools.upsert_product('AI Created', price=5, cost=2, stock=4)
        self.assertTrue(created['success'], created)
        self.assertEqual(created['stock'], 4)
        no_cost = tools.upsert_product('Needs Reviewed Cost', price=5)
        self.assertTrue(no_cost['success'], no_cost)
        failed = tools.upsert_product('Needs Reviewed Cost', price=5,
                                      product_id=no_cost['product_id'], stock=1)
        self.assertIn('reviewed cost', failed['error'])
        self.assertEqual(self.db.session.get(self.models['Product'], no_cost['product_id']).stock, 0)

    def test_stock_adjustment_rejects_inactive_shop(self):
        location = self.service.location(self.f['branch'].id)
        self.f['branch'].is_active = False
        with self.assertRaises(AccountingError) as caught:
            self.service.adjust_inventory({
                'key': 'inactive-shop', 'location_id': location.id,
                'product_id': self.f['product'].id, 'quantity': 1, 'unit_cost': '1.00',
                'date': self.today.isoformat(), 'reason': 'Inactive shop adjustment',
            })
        self.assertEqual(caught.exception.status, 403)
        self.db.session.rollback()

    def test_asset_depreciation_disposal_and_tamper_guard(self):
        base = {'key': 'asset-1', 'date': self.today.isoformat(),
                'location_id': self.service.location(None).id, 'name': 'Delivery van', 'category': 'Vehicles',
                'cost': '1200.00', 'salvage_value': '200.00', 'life_months': 120, 'paid_from': 'CASH'}
        asset = self.post('/assets', base)['asset']
        self.post(f'/assets/{asset["id"]}/depreciate', {
            'key': 'dep-1', 'date': self.today.isoformat(), 'amount': '100.00'})
        disposed = self.post(f'/assets/{asset["id"]}/dispose', {
            'key': 'dispose-1', 'date': self.today.isoformat(), 'proceeds': '500.00', 'paid_to': 'BANK'})['asset']
        self.assertEqual((disposed['status'], disposed['carrying_value']), ('disposed', '0.00'))
        row = self.db.session.get(self.service.Asset, asset['id'])
        row.name = 'Tampered'
        with self.assertRaises(AccountingError):
            self.db.session.commit()

    def test_payable_payment_idempotency_and_guard(self):
        bill = {'key': 'bill-1', 'date': self.today.isoformat(), 'supplier_id': self.f['supplier'].id,
                'reference': 'SUP-1', 'amount': '100.00', 'expense_account': 'RENT'}
        payable = self.post('/payables', bill)['payable']
        payment = {'key': 'pay-1', 'date': self.today.isoformat(), 'amount': '40.00', 'paid_from': 'CASH'}
        result = self.post(f'/payables/{payable["id"]}/pay', payment)
        self.assertEqual(result['payable']['balance'], '60.00')
        self.assertTrue(self.post(f'/payables/{payable["id"]}/pay', payment)['replayed'])
        self.post(f'/payables/{payable["id"]}/pay', dict(payment, amount='50.00'), status=409)
        self.assertEqual(self.service.Journal.query.filter_by(source_type='supplier-payment').count(), 1)
        row = self.db.session.get(self.service.Payable, payable['id'])
        row.balance_minor = 0
        with self.assertRaises(AccountingError):
            self.db.session.commit()

    def test_receivable_collection_posts_once_and_reconciles(self):
        self.f['debt'].due_date = self.today - timedelta(days=1)
        self.db.session.commit()
        payload = {'key': 'collect-1', 'date': self.today.isoformat(), 'amount': '5.00', 'paid_to': 'BANK'}
        result = self.post(f'/receivables/{self.f["debt"].id}/pay', payload)
        self.assertEqual(result['balance'], '15.00')
        self.assertEqual(self.f['debt'].status, 'overdue')
        self.assertTrue(self.post(f'/receivables/{self.f["debt"].id}/pay', payload)['replayed'])
        self.assertEqual(self.models['DebtPayment'].query.count(), 1)
        payment = self.models['DebtPayment'].query.one()
        self.assertEqual(self.f['debt'].balance, 15)
        self.f['debt'].balance = 14
        with self.assertRaises(AccountingError):
            self.db.session.commit()
        self.db.session.rollback()
        payment.amount = 4
        with self.assertRaises(AccountingError):
            self.db.session.commit()
        self.db.session.rollback()
        self.db.session.delete(payment)
        with self.assertRaises(AccountingError):
            self.db.session.commit()
        self.db.session.rollback()

    def test_ai_debt_payment_idempotency_quantizes_once_and_posts_collection(self):
        from ai_tools import AITools
        tools = AITools(self.db, self.models)
        tools.set_context({'user_id': self.f['user'].id, 'role': 'manager',
                           'branch_id': self.f['branch'].id})
        first = tools.record_debt_payment(self.f['debt'].id, '5.005', notes='Counter cash',
                                          idempotency_key='ai-collect-1')
        self.assertTrue(first['success'], first)
        self.assertEqual(first['amount_paid'], '5.01')
        self.assertEqual(first['remaining_balance'], 14.99)
        payment_id = first['payment_id']
        journal = self.service.Journal.query.filter_by(source_key='collection:ai-collect-1').one()
        self.assertEqual(journal.actor_id, self.f['user'].id)
        self.assertEqual(self.models['DebtPayment'].query.count(), 1)
        stored = self.service.Command.query.filter_by(key='debt-payment:ai-collect-1').one()
        self.assertEqual(stored.response, first)

        replay = tools.record_debt_payment(self.f['debt'].id, 5.005, notes='Counter cash',
                                           idempotency_key='ai-collect-1')
        self.assertTrue(replay['replayed'], replay)
        self.assertEqual(replay['payment_id'], payment_id)
        self.assertEqual(self.f['debt'].balance, 14.99)
        self.assertEqual(self.models['DebtPayment'].query.count(), 1)
        self.assertEqual(self.service.Journal.query.filter_by(source_type='collection').count(), 1)

        conflict = tools.record_debt_payment(self.f['debt'].id, '5.02', notes='Counter cash',
                                             idempotency_key='ai-collect-1')
        self.assertEqual(conflict['status_code'], 409)
        self.assertEqual(self.f['debt'].balance, 14.99)
        self.assertEqual(self.models['DebtPayment'].query.count(), 1)

        settled = tools.record_debt_payment(self.f['debt'].id, '14.99', idempotency_key='ai-collect-2')
        self.assertTrue(settled['success'], settled)
        self.assertEqual(settled['debt_status'], 'paid')
        self.assertTrue(tools.record_debt_payment(
            self.f['debt'].id, '5.005', notes='Counter cash',
            idempotency_key='ai-collect-1')['replayed'])
        self.assertEqual(self.models['DebtPayment'].query.count(), 2)
        self.assertEqual(self.f['debt'].balance, 0)
        self.assertEqual(sum(line.amount_minor for line in self.service.Journal.query.filter_by(
            source_type='collection').all()[0].lines), 0)

    def test_unkeyed_ai_debt_payment_posts_once_and_invalid_input_is_atomic(self):
        from ai_tools import AITools
        tools = AITools(self.db, self.models)
        tools.set_context({'user_id': self.f['user'].id, 'role': 'manager',
                           'branch_id': self.f['branch'].id})
        for amount in (True, 0, -1, 'NaN', '21.00'):
            with self.subTest(amount=amount):
                result = tools.record_debt_payment(self.f['debt'].id, amount)
                self.assertFalse(result['success'])
                self.assertEqual(self.f['debt'].balance, 20)
                self.assertEqual(self.models['DebtPayment'].query.count(), 0)
        result = tools.record_debt_payment(self.f['debt'].id, '5.00')
        self.assertTrue(result['success'], result)
        payment = self.db.session.get(self.models['DebtPayment'], result['payment_id'])
        journal = self.service.Journal.query.filter_by(source_key=f'collection:{payment.id}').one()
        self.assertEqual(journal.actor_id, self.f['user'].id)
        self.assertEqual(self.f['debt'].balance, 15)

    def test_manual_journal_reversal_and_irreversible_period_lock(self):
        location = self.service.location(None).id
        journal = self.post('/journals', {'key': 'journal-1', 'date': self.today.isoformat(), 'memo': 'Reviewed entry',
            'lines': [{'location_id': location, 'account_code': 'RENT', 'debit': '10.00', 'credit': '0.00'},
                      {'location_id': location, 'account_code': 'CASH', 'debit': '0.00', 'credit': '10.00'}]})['journal']
        self.post(f'/journals/{journal["id"]}/reverse', {'key': 'reverse-1', 'date': self.today.isoformat()})
        self.post(f'/journals/{journal["id"]}/reverse', {'key': 'reverse-2', 'date': self.today.isoformat()}, status=409)
        self.post('/period-lock', {'key': 'lock-1', 'through': self.today.isoformat()})
        self.post('/expenses', {'key': 'late', 'date': self.today.isoformat(), 'location_id': location,
            'account_code': 'RENT', 'amount': '1.00', 'paid_from': 'CASH', 'payee': 'Payee',
            'description': 'Locked period'}, status=409)
        self.post('/period-lock', {'key': 'unlock', 'through': '2020-01-01'}, status=409)

    def test_financial_tables_reject_direct_orm_and_sql_writes(self):
        layer = self.service.Layer.query.filter_by(source_key=f'opening:product:{self.f["product"].id}').one()
        layer.remaining -= 1
        with self.assertRaises(AccountingError):
            self.db.session.commit()
        self.db.session.rollback()
        for sql in ('UPDATE accounting_journal SET memo = \'tampered\'',
                    'DELETE FROM accounting_document',
                    'INSERT INTO accounting_command (key, fingerprint, response) VALUES (\'raw\', \'x\', \'{}\')'):
            with self.subTest(sql=sql), self.assertRaises(AccountingError):
                self.db.session.execute(text(sql))
                self.db.session.commit()
            self.db.session.rollback()
        self.db.session.add(self.service.Asset(location_id=self.service.location(None).id, name='Raw asset',
            category='Raw', acquired_on=self.today, cost_minor=100, salvage_minor=0,
            life_months=12, accumulated_minor=0, status='active'))
        with self.assertRaises(AccountingError):
            self.db.session.commit()

    def test_new_shop_location_and_unlayered_stock_fail_closed(self):
        branch = self.models['Branch'](name='New Shop', is_active=True)
        self.db.session.add(branch)
        product = self.models['Product'](name='New zero stock', branch_id=branch.id, stock=0, cost=1)
        self.db.session.add(product)
        self.db.session.flush()
        self.db.session.commit()
        self.assertIsNotNone(self.service.Location.query.filter_by(branch_id=branch.id).one())
        self.db.session.delete(branch)
        with self.assertRaises(AccountingError):
            self.db.session.commit()
        self.db.session.rollback()
        product.stock = 1
        with self.assertRaises(AccountingError):
            self.db.session.commit()
        self.db.session.rollback()

    def test_receipt_invoice_and_paid_or_unpaid_purchase_returns(self):
        po = self.models['PurchaseOrder']()
        self.db.session.add(po)
        self.db.session.flush()
        self.db.session.add(self.models['PurchaseOrderItem'](purchase_order_id=po.id, received_qty=2, unit_cost=3.25))
        batch = self.models['WarehouseInventory'](product_id=self.f['product'].id, quantity=2,
                                                  unit_cost=3.25, batch_number='B-2')
        self.db.session.add(batch)
        self.db.session.flush()
        receipt = self.models['GoodsReceipt'](purchase_order_id=po.id, number='GR-1', snapshot={
            'items': [{'product_id': batch.product_id, 'warehouse_inventory_id': batch.id,
                       'received_qty': 2, 'unit_cost': 3.25}]})
        self.db.session.add(receipt)
        self.db.session.flush()
        self.service.post_receipt(receipt)
        invoice = self.models['PurchaseInvoice'](purchase_order_id=po.id, supplier_id=self.f['supplier'].id,
            supplier_reference='SUP-2', invoice_date=self.today, total_minor=650, currency='USD',
            snapshot={'supplier_name': 'Supplier'})
        self.db.session.add(invoice)
        self.db.session.flush()
        with patch.object(self.service, 'today', return_value=self.today + timedelta(days=1)):
            self.service.post_invoice(invoice)
        self.db.session.commit()
        invoice_journal = self.service.Journal.query.filter_by(
            source_key=f'purchase-invoice:{po.id}').one()
        self.assertEqual(invoice_journal.date, invoice.invoice_date)
        line = self.models['PurchaseOrderItem'].query.filter_by(purchase_order_id=po.id).one()
        for row, field, value in ((line, 'unit_cost', 4), (receipt, 'number', 'GR-TAMPERED'),
                                  (batch, 'unit_cost', 4), (invoice, 'supplier_reference', 'SUP-TAMPERED')):
            setattr(row, field, value)
            with self.assertRaises(AccountingError):
                self.db.session.commit()
            self.db.session.rollback()
        payable = self.service.Payable.query.filter_by(source_key=f'purchase-invoice:{po.id}').one()
        payload = {'key': 'return-1', 'date': self.today.isoformat(), 'payable_id': payable.id,
                   'items': [{'warehouse_inventory_id': batch.id, 'quantity': 1}], 'reason': 'Damaged goods'}
        result = self.post('/purchase-returns', payload)
        self.assertEqual(result['total'], '3.25')
        self.assertEqual(payable.balance_minor, 325)
        self.post(f'/payables/{payable.id}/pay', {
            'key': 'pay-purchase', 'date': self.today.isoformat(), 'amount': '3.25', 'paid_from': 'CASH'})
        self.post('/purchase-returns', dict(payload, key='return-2', paid_to='BANK'))
        self.assertEqual(payable.balance_minor, 0)
        self.assertEqual(batch.quantity, 0)
        self.post('/purchase-returns', dict(payload, key='return-3'), status=409)
        batch.unit_cost = 9
        with self.assertRaises(AccountingError):
            self.db.session.commit()
        self.db.session.rollback()
        trial = self.client.get('/api/accounting/statements', query_string={
            'kind': 'trial-balance', 'start': self.today.isoformat(), 'end': self.today.isoformat()}).get_json()
        self.assertEqual(trial['totals']['difference'], '0.00')


if __name__ == '__main__':
    unittest.main()
