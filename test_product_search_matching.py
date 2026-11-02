"""search_products must be able to answer counting questions honestly.

Contracts under test:
  * ``total_products`` is the real number of matches, not the page size, because
    "how many X do we have" is answered straight off it.
  * A query whose wording is not in the catalogue ("how many IOS devices") still
    comes back with the branch's real vocabulary (size, categories, product
    names) instead of a shrug.
  * Extra or reordered words fall back to token matching across name, barcode
    and category rather than reporting an empty result.
  * Money helpers follow the store currency and tolerate payloads that already
    carry a currency label.

Fixtures live in a throwaway branch (same convention as
test_full_coverage_tools.py) and are deleted in tearDown.
"""

import unittest
import uuid
from decimal import Decimal

from app import AI_MODELS, app, db, Branch, Product, User
from ai_tools import (AITools, currency_label, money_dec, money_display,
                      money_str, product_search_terms)


class ProductSearchTestBase(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self._ctx = app.app_context()
        self._ctx.push()
        self.admin_id = User.query.filter_by(username='admin').first().id
        suffix = uuid.uuid4().hex[:6]
        self.branch = Branch(name=f"Search Test {suffix}", code=f"SRCH{suffix}",
                             is_default=False, is_active=True)
        db.session.add(self.branch)
        db.session.commit()
        self.branch_id = self.branch.id

        self.tools = AITools(db, AI_MODELS)
        self.tools.set_context({"branch_id": self.branch_id,
                                "user_id": self.admin_id, "role": "manager"})
        self.product_ids = []
        for name, barcode, category, price in (
            ("Iphone 13 pro max (512 GB) Green", f"7{suffix}01", "512 GB", 2400000),
            ("Iphone 18 pro Glacier", f"7{suffix}02", "512 GB", 5300000),
            ("Iphone 18 pro max Glacier", f"7{suffix}03", "256 GB", 5000000),
        ):
            product = Product(name=name, barcode=barcode, category=category,
                              price=price, cost=price * 0.8, stock=3,
                              reorder_point=10, reorder_enabled=True,
                              branch_id=self.branch_id)
            db.session.add(product)
            db.session.flush()
            self.product_ids.append(product.id)
        db.session.commit()

    def tearDown(self):
        try:
            Product.query.filter(Product.id.in_(self.product_ids)).delete(
                synchronize_session=False)
            db.session.commit()
            row = db.session.get(Branch, self.branch_id)
            if row is not None:
                db.session.delete(row)
                db.session.commit()
        finally:
            self._ctx.pop()


class CountQuestionTests(ProductSearchTestBase):
    def test_total_is_the_match_count_not_the_page_size(self):
        result = self.tools.search_products("iphone", limit=1)
        self.assertEqual(result["total_products"], 3)
        self.assertEqual(len(result["inventory"]), 1)

    def test_platform_word_returns_the_real_vocabulary(self):
        result = self.tools.search_products("how many IOS devices did we have")
        self.assertEqual(result["total_products"], 0)
        self.assertEqual(result["branch_total"], 3)
        self.assertIn("512 GB", result["categories"])
        self.assertIn("Iphone 18 pro Glacier", result["suggestions"])

    def test_names_are_searchable_once_the_user_uses_one(self):
        result = self.tools.search_products("iphone 18 pro max")
        self.assertEqual(result["total_products"], 1)
        self.assertEqual(result["inventory"][0]["name"], "Iphone 18 pro max Glacier")

    def test_nothing_related_returns_no_rows_but_keeps_vocabulary(self):
        result = self.tools.search_products("refrigerator")
        self.assertEqual(result["total_products"], 0)
        self.assertTrue(result["suggestions"])


class TolerantMatchingTests(ProductSearchTestBase):
    def test_reordered_words_still_match(self):
        result = self.tools.search_products("glacier iphone")
        self.assertEqual(result["total_products"], 2)
        self.assertEqual(result["matched_by"], "terms")

    def test_barcode_is_searchable(self):
        barcode = self.tools.search_products("Iphone 18 pro Glacier")["inventory"][0]["barcode"]
        result = self.tools.search_products(barcode)
        self.assertEqual(result["total_products"], 1)

    def test_category_is_searchable(self):
        result = self.tools.search_products("256 GB")
        self.assertGreaterEqual(result["total_products"], 1)

    def test_empty_query_is_safe(self):
        result = self.tools.search_products("   ")
        self.assertEqual(result["total_products"], 0)
        self.assertEqual(result["inventory"], [])


class QuestionTermTests(unittest.TestCase):
    def test_filler_words_are_dropped(self):
        self.assertEqual(product_search_terms("how many IOS devices did we have"),
                         ["ios"])

    def test_product_words_are_kept_longest_first(self):
        self.assertEqual(product_search_terms("iphone 18 pro"),
                         ["iphone", "pro", "18"])

    def test_short_noise_is_dropped(self):
        self.assertEqual(product_search_terms("a 1 in of x"), [])


class CurrencyHelperTests(unittest.TestCase):
    def test_mmk_amounts_are_suffixed(self):
        self.assertEqual(money_display(2400000, "MMK"), "2,400,000.00 MMK")

    def test_usd_amounts_are_prefixed(self):
        self.assertEqual(money_display(2400000, "USD"), "$2,400,000.00")

    def test_default_currency_is_usd(self):
        self.assertEqual(currency_label(None), "$")

    def test_unknown_code_is_kept_visible(self):
        self.assertEqual(money_display(10, "SGD"), "10.00 SGD")

    def test_payload_with_currency_label_still_parses(self):
        self.assertEqual(money_dec("2,400,000.00 MMK"), Decimal("2400000.00"))

    def test_payload_with_symbol_still_parses(self):
        self.assertEqual(money_str("$1,234.50"), "1,234.50")

    def test_garbage_still_falls_back_to_zero(self):
        self.assertEqual(money_dec("not a number"), Decimal("0"))


if __name__ == "__main__":
    unittest.main()
