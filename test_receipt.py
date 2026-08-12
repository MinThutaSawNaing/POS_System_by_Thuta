import unittest
from pathlib import Path

from flask import Flask, render_template

from receipt import (
    DEFAULT_RECEIPT_FOOTER,
    DEFAULT_RECEIPT_PAPER_SIZE,
    RECEIPT_PAPER_58MM,
    RECEIPT_PAPER_80MM,
    build_receipt_snapshot,
    build_receipt_view,
    calculate_thermal_page_height_mm,
    detect_receipt_logo_extension,
    get_paper_profile,
    normalize_receipt_paper_size,
    normalize_receipt_identity,
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

    def test_receipt_identity_uses_overrides_and_branch_fallbacks(self):
        identity = normalize_receipt_identity(
            {
                "brand_name": "  WinterArc Store  ",
                "email": "sales@example.com",
                "phone": "",
                "address": "",
                "footer_message": "  Come again  ",
            },
            {
                "email": "branch@example.com",
                "phone": "+95 9 123 456",
                "address": "Yangon",
            },
        )
        self.assertEqual(identity["brand_name"], "WinterArc Store")
        self.assertEqual(identity["email"], "sales@example.com")
        self.assertEqual(identity["phone"], "+95 9 123 456")
        self.assertEqual(identity["address"], "Yangon")
        self.assertEqual(identity["footer_message"], "Come again")

    def test_receipt_identity_validation(self):
        with self.assertRaisesRegex(ValueError, "Invalid receipt email"):
            normalize_receipt_identity({"brand_name": "Store", "email": "invalid"})
        with self.assertRaisesRegex(ValueError, "100 characters"):
            normalize_receipt_identity({"brand_name": "x" * 101})
        with self.assertRaisesRegex(ValueError, "200 characters"):
            normalize_receipt_identity({"brand_name": "Store", "footer_message": "x" * 201})

    def test_blank_footer_is_allowed(self):
        identity = normalize_receipt_identity({"brand_name": "Store", "footer_message": ""})
        self.assertEqual(identity["footer_message"], "")

    def test_logo_format_is_detected_from_file_signature(self):
        self.assertEqual(detect_receipt_logo_extension(b"\x89PNG\r\n\x1a\nrest"), "png")
        self.assertEqual(detect_receipt_logo_extension(b"\xff\xd8\xffrest"), "jpg")
        self.assertEqual(detect_receipt_logo_extension(b"RIFF1234WEBPrest"), "webp")
        self.assertIsNone(detect_receipt_logo_extension(b"<svg></svg>"))

    def test_version_two_snapshot_preserves_custom_identity(self):
        snapshot = self._snapshot()
        snapshot["receipt_identity"] = {
            "brand_name": "Custom Brand",
            "logo_filename": "historical-logo.png",
            "email": "hello@example.com",
            "phone": "+95 1 234 567",
            "address": "Line one\nLine two",
            "footer_message": "Thanks\nVisit again",
        }
        view = build_receipt_view(snapshot, RECEIPT_PAPER_80MM)
        self.assertEqual(view["brand_name"], "Custom Brand")
        self.assertEqual(view["logo_filename"], "historical-logo.png")
        self.assertEqual(view["address_lines"], ["Line one", "Line two"])
        self.assertEqual(view["footer_lines"], ["Thanks", "Visit again"])

    def test_version_one_snapshot_remains_compatible(self):
        snapshot = self._snapshot()
        snapshot.pop("receipt_identity", None)
        snapshot["version"] = 1
        snapshot["pos_name"] = "Legacy Brand"
        snapshot["branch"].update({"email": "old@example.com", "phone": "123", "address": "Old address"})
        view = build_receipt_view(snapshot, RECEIPT_PAPER_58MM)
        self.assertEqual(view["brand_name"], "Legacy Brand")
        self.assertEqual(view["email"], "old@example.com")
        self.assertEqual(view["footer_lines"], DEFAULT_RECEIPT_FOOTER.splitlines())

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

    def test_template_renders_logo_and_escapes_custom_text(self):
        snapshot = self._snapshot()
        snapshot["receipt_identity"] = {
            "brand_name": "<script>alert(1)</script>",
            "logo_filename": "logo.png",
            "email": "safe@example.com",
            "phone": "123",
            "address": "Main <Street>",
            "footer_message": "Thanks <b>friend</b>",
        }
        receipt = build_receipt_view(snapshot, RECEIPT_PAPER_58MM)
        receipt["logo_url"] = "/uploads/receipts/logo.png"
        template_folder = Path(__file__).resolve().parent / "templates"
        flask_app = Flask(__name__, template_folder=str(template_folder))
        with flask_app.app_context():
            html = render_template("receipt.html", receipt=receipt)
        self.assertIn('class="receipt-logo"', html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("Thanks &lt;b&gt;friend&lt;/b&gt;", html)


if __name__ == "__main__":
    unittest.main()