import unittest
from pathlib import Path

from flask import Flask, render_template

from receipt import (
    DEFAULT_RECEIPT_PAPER_SIZE,
    RECEIPT_PAPER_58MM,
    RECEIPT_PAPER_80MM,
    build_receipt_snapshot,
    build_receipt_view,
    calculate_thermal_page_height_mm,
    get_paper_profile,
    normalize_receipt_paper_size,
)


class ReceiptTests(unittest.TestCase):
    @staticmethod
    def _snapshot():
        return build_receipt_snapshot(
            transaction_id="00000000-0000-0000-0000-abcdef123456",
            sale_date="2026-08-12T12:00:00+06:30",
            pos_name="Parrot POS",
            currency_code="MMK",
            currency_suffix="MMK",
            branch={"name": "Main Branch", "address": "Yangon"},
            cashier_name="admin",
            payment_method="cash",
            cash_received=4000,
            change_given=850,
            items=[{
                "product_id": 7,
                "name": "Original name",
                "quantity": 2,
                "unit_price": 1500,
                "tax_rate": 5,
                "tax_amount": 150,
            }],
            subtotal=3000,
            tax=150,
            total=3150,
        )

    def test_paper_size_normalization_and_physical_widths(self):
        self.assertEqual(normalize_receipt_paper_size("THERMAL_58MM"), RECEIPT_PAPER_58MM)
        self.assertEqual(normalize_receipt_paper_size("THERMAL_80MM"), RECEIPT_PAPER_80MM)
        self.assertEqual(normalize_receipt_paper_size("letter"), DEFAULT_RECEIPT_PAPER_SIZE)
        self.assertEqual(get_paper_profile(RECEIPT_PAPER_58MM)["width_mm"], 58)
        self.assertEqual(get_paper_profile(RECEIPT_PAPER_80MM)["width_mm"], 80)

    def test_page_height_uses_content_and_cutter_safety(self):
        self.assertEqual(calculate_thermal_page_height_mm(960), 257)
        self.assertEqual(calculate_thermal_page_height_mm(float("nan")), 20)
        self.assertEqual(calculate_thermal_page_height_mm(float("inf")), 20)

    def test_snapshot_is_immutable_input_data(self):
        source_item = {
            "product_id": 7,
            "name": "Original name",
            "quantity": 2,
            "unit_price": 1500,
            "tax_rate": 5,
            "tax_amount": 150,
        }
        snapshot = build_receipt_snapshot(
            transaction_id="00000000-0000-0000-0000-abcdef123456",
            sale_date="2026-08-12T12:00:00+06:30",
            pos_name="Parrot POS",
            currency_code="MMK",
            currency_suffix="MMK",
            branch={"name": "Main Branch", "address": "Yangon"},
            cashier_name="admin",
            payment_method="cash",
            cash_received=4000,
            change_given=850,
            items=[source_item],
            subtotal=3000,
            tax=150,
            total=3150,
        )
        source_item["name"] = "Renamed later"

        self.assertEqual(snapshot["items"][0]["name"], "Original name")
        self.assertEqual(snapshot["items"][0]["line_total"], 3150)
        self.assertEqual(snapshot["currency_suffix"], "MMK")
        self.assertEqual(snapshot["branch"]["name"], "Main Branch")

        view = build_receipt_view(snapshot, RECEIPT_PAPER_58MM)
        self.assertEqual(view["receipt_number"], "EF123456")
        self.assertEqual(view["total_display"], "3,150.00 MMK")
        self.assertTrue(view["is_narrow"])

    def test_template_renders_exact_physical_width_for_both_profiles(self):
        template_folder = Path(__file__).resolve().parent / "templates"
        flask_app = Flask(__name__, template_folder=str(template_folder))

        with flask_app.app_context():
            narrow_html = render_template(
                "receipt.html",
                receipt=build_receipt_view(self._snapshot(), RECEIPT_PAPER_58MM),
            )
            wide_html = render_template(
                "receipt.html",
                receipt=build_receipt_view(self._snapshot(), RECEIPT_PAPER_80MM),
            )

        self.assertIn("size: 58mm 20mm", narrow_html)
        self.assertIn("width: 58mm", narrow_html)
        self.assertIn("55/58 mm thermal", narrow_html)
        self.assertIn("size: 80mm 20mm", wide_html)
        self.assertIn("width: 80mm", wide_html)
        self.assertIn("Original name", wide_html)


if __name__ == "__main__":
    unittest.main()