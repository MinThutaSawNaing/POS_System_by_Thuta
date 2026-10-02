"""Tests for the Returns & Exchanges tab: receipt view, API, KPIs and exports."""

import unittest
import uuid
from pathlib import Path

from flask import Flask, render_template

from receipt import (
    DEFAULT_RECEIPT_BRAND_NAME,
    RECEIPT_PAPER_58MM,
    RECEIPT_PAPER_80MM,
    build_return_exchange_view,
)


def _record(**overrides):
    record = {
        "currency_suffix": "MMK",
        "branch": {"name": "Main Branch", "address": "Yangon"},
        "workflow_id": "11111111-2222-3333-4444-555566667777",
        "mode": "exchange",
        "original_transaction_id": "txn-original-1",
        "adjustment_transaction_id": "txn-adjust-1",
        "created_at": "2026-10-01T09:15:00",
        "processed_by": "cashier1",
        "settlement_method": "cash",
        "return_items": [
            {"name": "Old Shirt", "quantity": 1, "unit_price": 1000, "tax_rate": 5,
             "line_total": 1000, "line_tax": 50},
        ],
        "exchange_items": [
            {"name": "New Shirt", "quantity": 2, "unit_price": 1200, "tax_rate": 5,
             "line_total": 2400, "line_tax": 120},
        ],
        "return_total": 1050,
        "exchange_total": 2520,
        "net_total": 1470,
        "refund_amount": 0,
        "collected_amount": 1470,
    }
    record.update(overrides)
    return record


class ReturnExchangeViewTests(unittest.TestCase):
    def test_exchange_view_formats_money_titles_and_flags(self):
        view = build_return_exchange_view(_record(), RECEIPT_PAPER_80MM)
        self.assertEqual(view["document_title"], "RETURN & EXCHANGE")
        self.assertEqual(view["workflow_number"], "555566667777"[-8:].upper())
        self.assertEqual(view["return_total_display"], "1,050.00 MMK")
        self.assertEqual(view["exchange_total_display"], "2,520.00 MMK")
        self.assertEqual(view["net_total_display"], "1,470.00 MMK")
        self.assertEqual(view["collected_amount_display"], "1,470.00 MMK")
        self.assertEqual(view["settlement_label"], "Cash")
        self.assertTrue(view["is_collect"])
        self.assertFalse(view["is_refund"])
        self.assertEqual(len(view["return_items"]), 1)
        self.assertEqual(len(view["exchange_items"]), 1)
        self.assertFalse(view["is_narrow"])

    def test_pure_return_title_and_refund_flag(self):
        view = build_return_exchange_view(
            _record(mode="return", exchange_items=[], exchange_total=0,
                    net_total=-1050, collected_amount=0, refund_amount=1050),
            RECEIPT_PAPER_58MM,
        )
        self.assertEqual(view["document_title"], "RETURN")
        self.assertTrue(view["is_refund"])
        self.assertFalse(view["is_collect"])
        self.assertTrue(view["is_narrow"])
        self.assertFalse(view["has_exchange"])

    def test_defaults_are_safe_for_missing_data(self):
        view = build_return_exchange_view({"workflow_id": "abc"}, RECEIPT_PAPER_80MM)
        self.assertEqual(view["brand_name"], DEFAULT_RECEIPT_BRAND_NAME)
        self.assertEqual(view["document_title"], "RETURN")
        self.assertEqual(view["return_total_display"], "0.00 $")
        self.assertEqual(view["return_items"], [])

    def test_template_renders_and_escapes_hostile_text(self):
        template_folder = Path(__file__).resolve().parent / "templates"
        flask_app = Flask(__name__, template_folder=str(template_folder))
        view = build_return_exchange_view(
            _record(
                return_items=[{"name": "<script>alert(1)</script>", "quantity": 1,
                               "unit_price": 100, "tax_rate": 0,
                               "line_total": 100, "line_tax": 0}],
            ),
            RECEIPT_PAPER_58MM,
        )
        with flask_app.app_context():
            html = render_template("exchange_receipt.html", exchange=view)
        self.assertIn("size: 58mm 20mm", html)
        self.assertIn("width: 58mm", html)
        self.assertIn("RETURN &amp; EXCHANGE", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>alert(1)</script>", html)


class ReturnExchangeRouteTests(unittest.TestCase):
    def setUp(self):
        from app import User, app, db, get_default_branch_id
        self.app = app
        self.db = db
        app.config.update(TESTING=True)
        self.context = app.app_context()
        self.context.push()
        self.user = User.query.filter_by(username='admin').first()
        self.branch_id = get_default_branch_id()
        self.assertIsNotNone(self.branch_id)
        self.created_workflows = []
        self.created_sales = []
        self.created_products = []
        self._build_fixtures()

    def _product(self, name):
        from app import Product
        product = Product(name=name, price=1000, stock=50, tax_rate=0,
                          branch_id=self.branch_id, barcode='RX-' + uuid.uuid4().hex[:10])
        self.db.session.add(product)
        self.db.session.commit()
        self.created_products.append(product)
        return product

    def _sale(self, branch_id=None):
        from app import Sale
        sale = Sale(transaction_id='rx-sale-' + uuid.uuid4().hex, total=1000, tax=0,
                    payment_method='cash', user_id=self.user.id,
                    branch_id=branch_id or self.branch_id)
        self.db.session.add(sale)
        self.db.session.commit()
        self.created_sales.append(sale)
        return sale

    def _workflow(self, mode, sale, *, return_total, refund_amount, exchange_total=0,
                  collected_amount=0, adjustment_sale=None, product=None):
        from app import ReturnExchange, ReturnExchangeItem
        workflow = ReturnExchange(
            workflow_id=str(uuid.uuid4()), mode=mode, original_sale_id=sale.id,
            adjustment_sale_id=adjustment_sale.id if adjustment_sale else None,
            return_total=return_total, exchange_total=exchange_total,
            net_total=exchange_total - return_total, refund_amount=refund_amount,
            collected_amount=collected_amount, settlement_method='cash',
            notes='test', user_id=self.user.id,
        )
        self.db.session.add(workflow)
        self.db.session.flush()
        self.db.session.add(ReturnExchangeItem(
            return_exchange_id=workflow.id, original_sale_item_id=None,
            product_id=product.id if product else None,
            movement='return' if mode == 'return' else 'exchange',
            quantity=1, unit_price=1000, tax_rate=0, line_total=1000, line_tax=0))
        self.db.session.commit()
        self.created_workflows.append(workflow)
        return workflow

    def _build_fixtures(self):
        self.product = self._product('RX Product')
        self.return_sale = self._sale()
        self.return_workflow = self._workflow(
            'return', self.return_sale, return_total=1000, refund_amount=1000,
            product=self.product)
        self.exchange_sale = self._sale()
        self.adjustment_sale = self._sale()
        self.exchange_workflow = self._workflow(
            'exchange', self.exchange_sale, return_total=400, refund_amount=0,
            exchange_total=1200, collected_amount=800,
            adjustment_sale=self.adjustment_sale, product=self.product)

    def tearDown(self):
        from app import Product, ReturnExchange, ReturnExchangeItem, Sale, SaleItem
        db = self.db
        workflow_ids = [w.id for w in self.created_workflows] or [0]
        ReturnExchangeItem.query.filter(
            ReturnExchangeItem.return_exchange_id.in_(workflow_ids)).delete(synchronize_session=False)
        ReturnExchange.query.filter(ReturnExchange.id.in_(workflow_ids)).delete(synchronize_session=False)
        sale_ids = [s.id for s in self.created_sales] or [0]
        SaleItem.query.filter(SaleItem.sale_id.in_(sale_ids)).delete(synchronize_session=False)
        Sale.query.filter(Sale.id.in_(sale_ids)).delete(synchronize_session=False)
        product_ids = [p.id for p in self.created_products] or [0]
        Product.query.filter(Product.id.in_(product_ids)).delete(synchronize_session=False)
        db.session.commit()
        self.context.pop()

    def client(self, authenticated=True, role='manager'):
        client = self.app.test_client()
        if authenticated:
            with client.session_transaction() as session:
                session['user_id'] = self.user.id
                session['role'] = role
                session['branch_id'] = self.branch_id
        return client

    def test_endpoints_require_login(self):
        anon = self.client(authenticated=False)
        self.assertEqual(anon.get('/api/returns_exchanges').status_code, 401)
        self.assertEqual(anon.get('/api/returns_exchanges/export').status_code, 401)
        self.assertEqual(
            anon.get(f'/api/returns_exchanges/{self.return_workflow.workflow_id}/print').status_code,
            401)

    def test_paginated_list_has_summary_and_both_modes(self):
        response = self.client().get('/api/returns_exchanges?page=1&per_page=20')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIn('items', data)
        self.assertIn('summary', data)
        workflow_ids = {item['workflow_id'] for item in data['items']}
        self.assertIn(self.return_workflow.workflow_id, workflow_ids)
        self.assertIn(self.exchange_workflow.workflow_id, workflow_ids)
        modes = {item['mode'] for item in data['items']}
        self.assertIn('return', modes)
        self.assertIn('exchange', modes)
        self.assertGreaterEqual(data['summary']['workflows'], 2)
        self.assertEqual(data['summary']['returns'] + data['summary']['exchanges'],
                         data['summary']['workflows'])

    def test_plain_list_stays_a_backward_compatible_array(self):
        data = self.client().get('/api/returns_exchanges').get_json()
        self.assertIsInstance(data, list)
        self.assertTrue(any(item['workflow_id'] == self.return_workflow.workflow_id
                            for item in data))

    def test_mode_filter_returns_only_exchanges(self):
        data = self.client().get('/api/returns_exchanges?mode=exchange&page=1&per_page=50').get_json()
        self.assertTrue(all(item['mode'] == 'exchange' for item in data['items']))
        ids = {item['workflow_id'] for item in data['items']}
        self.assertIn(self.exchange_workflow.workflow_id, ids)
        self.assertNotIn(self.return_workflow.workflow_id, ids)

    def test_search_matches_original_and_adjustment_transactions(self):
        by_original = self.client().get(
            f'/api/returns_exchanges?q={self.return_sale.transaction_id}&page=1&per_page=50').get_json()
        self.assertIn(self.return_workflow.workflow_id,
                      {item['workflow_id'] for item in by_original['items']})
        by_adjustment = self.client().get(
            f'/api/returns_exchanges?q={self.adjustment_sale.transaction_id}&page=1&per_page=50').get_json()
        self.assertIn(self.exchange_workflow.workflow_id,
                      {item['workflow_id'] for item in by_adjustment['items']})

    def test_created_at_is_filtered_and_labelled_in_business_timezone(self):
        from datetime import datetime
        workflow = self._workflow('return', self._sale(), return_total=100,
                                  refund_amount=100, product=self.product)
        # 18:00 UTC is 00:30 the next day in Asia/Yangon.
        workflow.created_at = datetime(2026, 10, 1, 18, 0, 0)
        self.db.session.commit()

        listing = self.client().get(
            '/api/returns_exchanges?start=2026-10-02&end=2026-10-02&page=1&per_page=50').get_json()
        row = next(item for item in listing['items']
                   if item['workflow_id'] == workflow.workflow_id)
        self.assertEqual(row['created_at'], '2026-10-02T00:30:00+06:30')

        # The previous (UTC) day must not match the same row.
        previous = self.client().get(
            '/api/returns_exchanges?start=2026-10-01&end=2026-10-01&page=1&per_page=50').get_json()
        self.assertNotIn(workflow.workflow_id, {i['workflow_id'] for i in previous['items']})

    def test_future_date_window_returns_nothing(self):
        data = self.client().get(
            '/api/returns_exchanges?start=2999-01-01&end=2999-01-02&page=1&per_page=50').get_json()
        self.assertEqual(data['items'], [])
        self.assertEqual(data['summary']['workflows'], 0)

    def test_branch_isolation(self):
        from app import Branch, ReturnExchange, ReturnExchangeItem, Sale
        other = Branch(name='RX Branch ' + uuid.uuid4().hex[:6],
                       code='RX' + uuid.uuid4().hex[:4].upper(),
                       is_active=True, is_default=False)
        self.db.session.add(other)
        self.db.session.commit()
        other_sale = self._sale(branch_id=other.id)
        other_workflow = self._workflow('return', other_sale, return_total=500,
                                        refund_amount=500, product=self.product)
        data = self.client().get('/api/returns_exchanges?page=1&per_page=100').get_json()
        ids = {item['workflow_id'] for item in data['items']}
        self.assertNotIn(other_workflow.workflow_id, ids)
        self.assertIn(self.return_workflow.workflow_id, ids)

        # A receipt for another branch's workflow must not be printable from here.
        self.assertEqual(
            self.client().get(f'/api/returns_exchanges/{other_workflow.workflow_id}/print').status_code,
            404)
        self.assertEqual(
            self.client().get(f'/api/returns_exchanges/{self.return_workflow.workflow_id}/print').status_code,
            200)

        ReturnExchangeItem.query.filter_by(return_exchange_id=other_workflow.id).delete()
        ReturnExchange.query.filter_by(id=other_workflow.id).delete()
        self.created_workflows = [w for w in self.created_workflows if w.id != other_workflow.id]
        Sale.query.filter_by(id=other_sale.id).delete()
        self.created_sales = [s for s in self.created_sales if s.id != other_sale.id]
        Branch.query.filter_by(id=other.id).delete()
        self.db.session.commit()

    def test_export_downloads_pdf_and_xlsx(self):
        pdf = self.client().get('/api/returns_exchanges/export?format=pdf')
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.headers['Content-Type'], 'application/pdf')
        xlsx = self.client().get('/api/returns_exchanges/export?format=xlsx')
        self.assertEqual(xlsx.status_code, 200)
        self.assertIn('spreadsheetml', xlsx.headers['Content-Type'])

    def test_print_renders_receipt_for_both_documents(self):
        response = self.client().get(
            f'/api/returns_exchanges/{self.return_workflow.workflow_id}/print')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get('Cache-Control'), 'private, no-store, max-age=0')
        html = response.get_data(as_text=True)
        self.assertIn('RETURN', html)
        self.assertIn('RX Product', html)
        self.assertIn(self.return_workflow.workflow_id[-8:].upper(), html)

        exchange_html = self.client().get(
            f'/api/returns_exchanges/{self.exchange_workflow.workflow_id}/print').get_data(as_text=True)
        self.assertIn('EXCHANGE', exchange_html)
        self.assertIn('RX Product', exchange_html)

    def test_print_unknown_workflow_is_404(self):
        self.assertEqual(
            self.client().get('/api/returns_exchanges/does-not-exist/print').status_code, 404)

    def test_post_then_list_and_print_round_trip(self):
        from app import ReturnExchange, Sale, SaleItem
        sale = self._sale()
        sale_item = SaleItem(sale_id=sale.id, product_id=self.product.id,
                             quantity=2, price=1000, tax=0)
        self.db.session.add(sale_item)
        self.db.session.commit()

        response = self.client().post('/api/returns_exchanges', json={
            'original_transaction_id': sale.transaction_id,
            'return_items': [{'sale_item_id': sale_item.id, 'quantity': 1}],
            'exchange_items': [{'product_id': self.product.id, 'quantity': 1, 'price': 1000}],
            'settlement_method': 'cash',
        })
        self.assertEqual(response.status_code, 201)
        body = response.get_json()
        self.assertTrue(body['success'])
        workflow_id = body['workflow_id']

        listing = self.client().get('/api/returns_exchanges?page=1&per_page=100').get_json()
        self.assertIn(workflow_id, {item['workflow_id'] for item in listing['items']})

        printed = self.client().get(f'/api/returns_exchanges/{workflow_id}/print')
        self.assertEqual(printed.status_code, 200)
        self.assertIn('EXCHANGE', printed.get_data(as_text=True))

        # Track everything the POST created so tearDown removes it.
        workflow = ReturnExchange.query.filter_by(workflow_id=workflow_id).one()
        self.created_workflows.append(workflow)
        if workflow.adjustment_sale_id:
            self.created_sales.append(self.db.session.get(Sale, workflow.adjustment_sale_id))


if __name__ == '__main__':
    unittest.main()
