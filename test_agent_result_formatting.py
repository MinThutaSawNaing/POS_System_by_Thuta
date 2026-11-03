"""The user-facing formatter must answer the question and must never raise.

Contracts under test:
  * A barcode question gets the barcode. A product search used to be rendered
    as a stock-only "Inventory Status" summary, so "what is the barcode of
    riding gloves?" answered with stock counts and no identifier at all.
  * Money arrives from the tools as a formatted string ("68,000.00"), never as
    a float. Formatting it with ":.2f" raised ValueError inside the reply
    builder, which reached the user as the generic "I'm sorry, something went
    wrong. Please try again." -- so every money field goes through money_str.
  * One unreadable payload degrades to a sentence; it cannot take the other
    results, or the whole turn, down with it.
"""

import unittest
from unittest import mock

import agent_orchestrator
from agent_orchestrator import AgentOrchestrator
from ai_agent import ChatResponse, ToolCall


SEARCH_RESULT = {
    "total_products": 1,
    "inventory": [{"product_id": 85, "name": "Riding Gloves", "barcode": "900028",
                   "category": "Gloves", "current_stock": 9,
                   "price": "95,000.00", "cost": "68,000.00",
                   "status": "low_stock"}],
    "branch_id": 1,
}

EMPTY_SEARCH_RESULT = {"total_products": 0, "inventory": [], "branch_id": 1}

PRODUCT_DETAILS = {
    "product_id": 85, "name": "Riding Gloves", "barcode": "900028",
    "category": "Gloves", "price": "68,000.00", "cost": "50,000.00",
    "stock": 9, "reorder_enabled": True, "reorder_point": 10,
}

PURCHASE_ORDERS = {
    "total_orders": 1,
    "orders": [{"po_number": "PO-2026-11", "supplier_name": "Kyaw Tun Store",
                "status": "pending", "total_amount": "1,050,000.00"}],
}


class FormatterTestCase(unittest.TestCase):
    """Shared harness: a keyless orchestrator formats payloads deterministically."""

    def setUp(self):
        agent_orchestrator.reset_orchestrator()
        self.orchestrator = AgentOrchestrator(None, {})

    def tearDown(self):
        agent_orchestrator.reset_orchestrator()

    def render(self, func_name, result, command="test"):
        return self.orchestrator._format_tool_results_for_user(
            [{"function_name": func_name, "result": result, "error": None}], command)


class PoliteSupplierSelectionTests(FormatterTestCase):
    SUPPLIERS = {"total_suppliers": 1, "suppliers": [
        {"name": "C & K", "phone": "09251246518", "quality_rating": 5.0}]}

    def test_purchase_order_supplier_list_invites_selection(self):
        text = self.render("get_supplier_list", self.SUPPLIERS,
                           "Please make a PO for 10 Motorcycle Batteries")
        self.assertIn("Please choose a supplier for your purchase order.", text)
        self.assertIn("C & K", text)
        self.assertIn("09251246518", text)
        self.assertIn("5.0/5", text)

    def test_short_followup_uses_recent_po_context_for_wording(self):
        self.orchestrator.agent.add_user_message("Can you make a PO for 10 batteries?")
        self.orchestrator.agent.add_assistant_message("Motorcycle Battery: 2 units in stock.")
        text = self.render("get_supplier_list", self.SUPPLIERS, "Ok do it")
        self.assertIn("Please choose a supplier for your purchase order.", text)

    def test_ordinary_supplier_directory_does_not_assume_order(self):
        for command in ("Show suppliers", "List suppliers in alphabetical order"):
            with self.subTest(command=command):
                text = self.render("get_supplier_list", self.SUPPLIERS, command)
                self.assertNotIn("Please choose", text)

    def test_empty_supplier_list_does_not_ask_for_impossible_selection(self):
        text = self.render("get_supplier_list", {"suppliers": []}, "Make a PO")
        self.assertNotIn("Please choose", text)
        self.assertIn("No suppliers found", text)


class ProductIdentifierTests(FormatterTestCase):
    """A barcode question must be answered with the barcode."""

    def test_search_products_shows_barcode(self):
        text = self.render("search_products", SEARCH_RESULT,
                           "What is the barcode of riding gloves?")
        self.assertIn("Riding Gloves", text)
        self.assertIn("Barcode: 900028", text)

    def test_search_products_shows_price_and_stock(self):
        text = self.render("search_products", SEARCH_RESULT)
        self.assertIn("$95,000.00", text)
        self.assertIn("9 units", text)
        self.assertIn("low stock", text)

    def test_search_products_is_not_labeled_inventory_status(self):
        text = self.render("search_products", SEARCH_RESULT)
        self.assertNotIn("Inventory Status", text)
        self.assertIn("Product Search", text)

    def test_search_products_without_matches_says_so(self):
        text = self.render("search_products", EMPTY_SEARCH_RESULT)
        self.assertIn("nothing in this branch matches", text)

    def test_empty_search_explains_what_does_exist(self):
        payload = {"total_products": 0, "inventory": [], "query": "IOS devices",
                   "branch_total": 44, "categories": ["Drinks", "Electronic"],
                   "suggestions": ["Iphone 18 pro Glacier", "Iphone 13 pro max"]}
        text = self.render("search_products", payload,
                           "How many IOS devices did we have?")
        self.assertIn("IOS devices", text)
        self.assertIn("44 product(s)", text)
        self.assertIn("Drinks, Electronic", text)
        self.assertIn("Iphone 18 pro Glacier", text)

    def test_search_products_missing_barcode_is_stated_not_blank(self):
        payload = {"total_products": 1, "inventory": [
            {"name": "Unlabelled Item", "barcode": None, "current_stock": 2,
             "status": "ok"}]}
        text = self.render("search_products", payload)
        self.assertIn("Barcode: not set", text)

    def test_product_details_renders_string_money(self):
        """This exact payload used to raise ValueError and lose the turn."""
        text = self.render("get_product_details", PRODUCT_DETAILS,
                           "I want to know the exact barcode number")
        self.assertIn("Barcode: 900028", text)
        self.assertIn("Price: $68,000.00", text)
        self.assertIn("Cost: $50,000.00", text)

    def test_product_details_accepts_numeric_money(self):
        payload = dict(PRODUCT_DETAILS, price=2500, cost=2000.5)
        text = self.render("get_product_details", payload)
        self.assertIn("Price: $2,500.00", text)
        self.assertIn("Cost: $2,000.50", text)

    def test_product_details_without_barcode_is_stated(self):
        payload = dict(PRODUCT_DETAILS, barcode=None)
        text = self.render("get_product_details", payload)
        self.assertIn("Barcode: not set", text)
        self.assertNotIn("None", text)

    def test_inventory_status_shows_barcode_when_asked(self):
        payload = {"total_products": 1, "inventory": SEARCH_RESULT["inventory"]}
        text = self.render("get_inventory_status", payload,
                           "what's the barcode of that product")
        self.assertIn("Barcode: 900028", text)

    def test_inventory_status_stays_stock_focused_otherwise(self):
        payload = {"total_products": 1, "inventory": SEARCH_RESULT["inventory"]}
        text = self.render("get_inventory_status", payload, "how is stock?")
        self.assertIn("Inventory Status", text)
        self.assertNotIn("Barcode:", text)

    def test_low_stock_shows_barcode_when_asked(self):
        payload = {"items": [{"name": "Riding Gloves", "barcode": "900028",
                              "current_stock": 9, "reorder_point": 10,
                              "suggested_reorder_qty": 41}],
                   "summary": {"low_stock_count": 1, "out_of_stock_count": 0}}
        text = self.render("get_low_stock_items", payload, "barcode of low stock items")
        self.assertIn("Barcode: 900028", text)


class MoneyFormattingTests(FormatterTestCase):
    """Tool payloads carry pre-formatted money strings; rendering must cope."""

    def test_purchase_orders_accepts_string_total(self):
        text = self.render("get_purchase_orders", PURCHASE_ORDERS)
        self.assertIn("PO-2026-11", text)
        self.assertIn("$1,050,000.00", text)

    def test_purchase_orders_accepts_numeric_total(self):
        payload = {"total_orders": 1, "orders": [
            dict(PURCHASE_ORDERS["orders"][0], total_amount=1500.5)]}
        text = self.render("get_purchase_orders", payload)
        self.assertIn("$1,500.50", text)

    def test_created_purchase_order_accepts_string_total(self):
        payload = {"success": True, "po_number": "PO-2026-12",
                   "supplier_name": "Kyaw Tun Store", "total_amount": "1,050,000.00",
                   "items_count": 2, "status": "draft"}
        text = self.render("create_purchase_order", payload)
        self.assertIn("$1,050,000.00", text)

    def test_missing_money_reads_as_zero_not_an_error(self):
        payload = {"total_orders": 1, "orders": [
            {"po_number": "PO-1", "supplier_name": "Shop", "status": "draft"}]}
        text = self.render("get_purchase_orders", payload)
        self.assertIn("$0.00", text)


class FormattingResilienceTests(FormatterTestCase):
    """A payload the formatter cannot read must not cost the user the answer."""

    def test_inventory_rows_without_status_do_not_raise(self):
        payload = {"total_products": 1, "inventory": [
            {"name": "Mystery Item", "current_stock": 3, "barcode": "111"}]}
        text = self.render("get_inventory_status", payload)
        self.assertIn("Mystery Item", text)
        self.assertIn("unknown", text)

    def test_low_stock_rows_without_optional_keys_do_not_raise(self):
        payload = {"items": [{"name": "Mystery Item", "current_stock": 0}],
                   "summary": {}}
        text = self.render("get_low_stock_items", payload)
        self.assertIn("Mystery Item", text)

    def test_unreadable_payload_degrades_to_a_sentence(self):
        # "inventory" is a string here, so row-level rendering cannot work.
        text = self.render("search_products", {"total_products": 3,
                                               "inventory": "not a list"})
        self.assertIsInstance(text, str)
        self.assertTrue(text.strip())

    def test_one_bad_result_does_not_hide_the_good_one(self):
        text = self.orchestrator._format_tool_results_for_user(
            [{"function_name": "search_products",
              "result": {"total_products": 1, "inventory": "not a list"},
              "error": None},
             {"function_name": "get_product_details", "result": PRODUCT_DETAILS,
              "error": None}], "barcode please")
        self.assertIn("Barcode: 900028", text)
        self.assertIn("Riding Gloves", text)

    def test_tool_error_lines_are_still_reported(self):
        text = self.orchestrator._format_tool_results_for_user(
            [{"function_name": "search_products", "result": None,
              "error": "database is locked"}], "barcode please")
        self.assertIn("database is locked", text)


class CurrencyRenderingTests(unittest.TestCase):
    """Answers must use the store's currency, never a hardcoded dollar sign."""

    def _render(self, currency_code, func_name, result, command="test"):
        orchestrator = AgentOrchestrator(
            None, {},
            get_setting_func=lambda key, default=None: (
                currency_code if key == 'currency_code' else default))
        return orchestrator._format_tool_results_for_user(
            [{"function_name": func_name, "result": result, "error": None}], command)

    def test_mmk_store_sees_mmk_prices(self):
        text = self._render("MMK", "search_products", SEARCH_RESULT)
        self.assertIn("95,000.00 MMK", text)
        self.assertNotIn("$", text)

    def test_usd_store_still_sees_dollar_prices(self):
        text = self._render("USD", "search_products", SEARCH_RESULT)
        self.assertIn("$95,000.00", text)

    def test_product_details_follows_store_currency(self):
        text = self._render("MMK", "get_product_details", PRODUCT_DETAILS)
        self.assertIn("Price: 68,000.00 MMK", text)
        self.assertNotIn("$", text)

    def test_unknown_currency_code_is_shown_not_dropped(self):
        text = self._render("SGD", "get_product_details", PRODUCT_DETAILS)
        self.assertIn("68,000.00 SGD", text)


class BarcodeTurnTests(FormatterTestCase):
    """The reported transcript, replayed end to end (chat + reply building)."""

    def _run_turn(self, command, payload):
        tool_call = ToolCall(id="call_1", function_name=payload["function_name"],
                             arguments={})
        reply = ChatResponse(content="", tool_calls=[tool_call], finish_reason="tool_calls")
        with mock.patch.object(self.orchestrator.agent, "chat", return_value=reply), \
             mock.patch.object(self.orchestrator, "_execute_tools_with_context",
                               return_value=[payload]), \
             mock.patch.object(self.orchestrator, "_fallback_intent_detection",
                               return_value=None):
            return self.orchestrator.process_command(command, user_id=1)

    def test_barcode_question_answers_with_the_barcode(self):
        result = self._run_turn(
            "I want to know the exact barcode number of Riding Gloves.",
            {"tool_call_id": "call_1", "function_name": "get_product_details",
             "result": PRODUCT_DETAILS, "error": None})
        self.assertTrue(result["success"])
        self.assertIn("Barcode: 900028", result["message"])
        self.assertNotIn("something went wrong", result["message"])

    def test_search_turn_answers_with_the_barcode(self):
        result = self._run_turn(
            "What is the barcode of riding gloves?",
            {"tool_call_id": "call_1", "function_name": "search_products",
             "result": SEARCH_RESULT, "error": None})
        self.assertTrue(result["success"])
        self.assertIn("Barcode: 900028", result["message"])

    def test_unreadable_payload_still_returns_an_answer(self):
        result = self._run_turn(
            "What is the barcode of riding gloves?",
            {"tool_call_id": "call_1", "function_name": "search_products",
             "result": {"total_products": 1, "inventory": "not a list"},
             "error": None})
        self.assertTrue(result["success"])
        self.assertTrue(result["message"].strip())
        self.assertNotIn("something went wrong", result["message"])


if __name__ == "__main__":
    unittest.main()
