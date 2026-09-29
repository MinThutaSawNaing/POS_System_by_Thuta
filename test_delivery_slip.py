"""Tests for the driver delivery slip: view builder, template and print route."""

import unittest
import uuid
from pathlib import Path

from flask import Flask, render_template

from receipt import (
    DEFAULT_RECEIPT_BRAND_NAME,
    RECEIPT_PAPER_58MM,
    RECEIPT_PAPER_80MM,
    build_delivery_slip_view,
)


def _slip_data(**overrides):
    data = {
        "currency_suffix": "MMK",
        "branch": {"name": "Main Branch", "address": "Yangon"},
        "delivery_number": "DLV-000123",
        "stage_label": "Packaged",
        "priority": "urgent",
        "created_at": "2026-09-29T08:30:00",
        "sale_transaction_id": "txn-abc-123",
        "payment_method": "split_payment",
        "recipient_name": "Aung Aung",
        "recipient_phone": "09123456789",
        "delivery_address": "No 12, Road 5\nKamayut",
        "township": "Kamayut",
        "instructions": "Call before arrival",
        "courier_name": "Kyaw Kyaw",
        "courier_phone": "09988877766",
        "tracking_code": "TRK-77",
        "items": [
            {"name": "Product A", "quantity": 2},
            {"name": "Product B", "quantity": 1},
        ],
        "order_total": 3150,
        "delivery_fee": 500,
        "collect_total": 3650,
    }
    data.update(overrides)
    return data


class DeliverySlipViewTests(unittest.TestCase):
    def test_view_formats_money_priority_and_labels(self):
        view = build_delivery_slip_view(_slip_data(), RECEIPT_PAPER_80MM)
        self.assertEqual(view["order_total_display"], "3,150.00 MMK")
        self.assertEqual(view["delivery_fee_display"], "500.00 MMK")
        self.assertEqual(view["collect_total_display"], "3,650.00 MMK")
        self.assertEqual(view["priority"], "Urgent")
        self.assertEqual(view["payment_method"], "Split Payment")
        self.assertEqual(view["delivery_address_lines"], ["No 12, Road 5", "Kamayut"])
        self.assertEqual(view["created_at"], "2026-09-29 08:30")
        self.assertEqual(len(view["items"]), 2)
        self.assertFalse(view["is_narrow"])

    def test_view_defaults_are_safe_for_missing_optional_data(self):
        view = build_delivery_slip_view(
            {
                "delivery_number": "DLV-1",
                "items": [{"name": None, "quantity": "bad"}],
            },
            RECEIPT_PAPER_58MM,
        )
        self.assertEqual(view["brand_name"], DEFAULT_RECEIPT_BRAND_NAME)
        self.assertEqual(view["priority"], "Normal")
        self.assertEqual(view["order_total_display"], "0.00 $")
        self.assertEqual(view["collect_total_display"], "0.00 $")
        self.assertEqual(view["items"], [{"name": "Item", "quantity": 0}])
        self.assertEqual(view["delivery_address_lines"], [])
        self.assertTrue(view["is_narrow"])

    def test_view_uses_stored_receipt_identity(self):
        view = build_delivery_slip_view(
            _slip_data(receipt_identity={
                "brand_name": "Custom Brand",
                "logo_filename": "logo.png",
                "email": "hello@example.com",
                "phone": "+95 1 234 567",
                "address": "Street 1",
                "footer_message": "Thanks",
            }),
            RECEIPT_PAPER_80MM,
        )
        self.assertEqual(view["brand_name"], "Custom Brand")
        self.assertEqual(view["logo_filename"], "logo.png")

    def test_template_renders_both_paper_widths_and_escapes_text(self):
        template_folder = Path(__file__).resolve().parent / "templates"
        flask_app = Flask(__name__, template_folder=str(template_folder))
        slip = build_delivery_slip_view(
            _slip_data(recipient_name="<script>alert(1)</script>"),
            RECEIPT_PAPER_58MM,
        )
        with flask_app.app_context():
            narrow_html = render_template("delivery_slip.html", slip=slip)
            wide_html = render_template(
                "delivery_slip.html",
                slip=build_delivery_slip_view(_slip_data(), RECEIPT_PAPER_80MM),
            )
        self.assertIn("size: 58mm 20mm", narrow_html)
        self.assertIn("width: 58mm", narrow_html)
        self.assertIn("size: 80mm 20mm", wide_html)
        self.assertIn("width: 80mm", wide_html)
        self.assertIn("DELIVERY SLIP", wide_html)
        self.assertIn("Aung Aung", wide_html)
        self.assertIn("Product A", wide_html)
        self.assertIn("3,650.00 MMK", wide_html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", narrow_html)
        self.assertNotIn("<script>alert(1)</script>", narrow_html)


class DeliverySlipRouteTests(unittest.TestCase):
    def setUp(self):
        from app import Branch, Delivery, Product, Sale, SaleItem, User, app, db
        from app import get_default_branch_id
        self.app = app
        self.db = db
        app.config.update(TESTING=True)
        self.context = app.app_context()
        self.context.push()
        self.user = User.query.filter_by(username='admin').first()
        self.branch_id = get_default_branch_id()
        self.assertIsNotNone(self.branch_id)
        self.branch = db.session.get(Branch, self.branch_id)
        self.product = Product(
            name='Delivery slip test product', price=1500, stock=0,
            tax_rate=0, branch_id=self.branch_id,
            barcode='SLIP-' + uuid.uuid4().hex[:10],
        )
        db.session.add(self.product)
        db.session.commit()
        self.sale = Sale(
            transaction_id='slip-test-' + uuid.uuid4().hex,
            total=3150, tax=150, payment_method='cash',
            user_id=self.user.id, branch_id=self.branch_id,
        )
        db.session.add(self.sale)
        db.session.commit()
        self.sale_item = SaleItem(
            sale_id=self.sale.id, product_id=self.product.id,
            quantity=2, price=1500, tax=150,
        )
        db.session.add(self.sale_item)
        self.delivery = Delivery(
            delivery_number='DLV-TEST-' + uuid.uuid4().hex[:8],
            sale_id=self.sale.id, stage='packaged', priority='high',
            recipient_name='Slip Recipient', recipient_phone='09111111111',
            delivery_address='Slip Street 1', township='SlipTown',
            delivery_fee=500, branch_id=self.branch_id,
            created_by=self.user.id,
        )
        db.session.add(self.delivery)
        db.session.commit()

    def tearDown(self):
        from app import Product, Sale, SaleItem
        db = self.db
        db.session.delete(db.session.get(type(self.delivery), self.delivery.id))
        SaleItem.query.filter_by(sale_id=self.sale.id).delete()
        db.session.delete(db.session.get(Sale, self.sale.id))
        db.session.delete(db.session.get(Product, self.product.id))
        db.session.commit()
        self.context.pop()

    def client(self, authenticated=True):
        client = self.app.test_client()
        if authenticated:
            with client.session_transaction() as session:
                session['user_id'] = self.user.id
                session['role'] = 'manager'
                session['branch_id'] = self.branch_id
        return client

    def test_print_requires_login(self):
        response = self.client(authenticated=False).get(f'/api/deliveries/{self.delivery.id}/print')
        self.assertEqual(response.status_code, 401)

    def test_print_returns_404_for_unknown_delivery(self):
        response = self.client().get('/api/deliveries/99999999/print')
        self.assertEqual(response.status_code, 404)

    def test_print_rejects_non_integer_id(self):
        response = self.client().get('/api/deliveries/abc/print')
        self.assertEqual(response.status_code, 404)

    def test_print_renders_slip_with_items_and_collect_total(self):
        response = self.client().get(f'/api/deliveries/{self.delivery.id}/print')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get('Cache-Control'), 'private, no-store, max-age=0')
        html = response.get_data(as_text=True)
        self.assertIn('DELIVERY SLIP', html)
        self.assertIn(self.delivery.delivery_number, html)
        self.assertIn('Slip Recipient', html)
        self.assertIn('Slip Street 1', html)
        self.assertIn('Delivery slip test product', html)
        self.assertIn('3,650.00', html)  # 3150 sale total + 500 delivery fee

if __name__ == '__main__':
    unittest.main()
