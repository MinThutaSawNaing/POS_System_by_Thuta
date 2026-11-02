"""An execution outcome must be described exactly, never narrated by a model.

The transcript that motivated this: after searching four iPhone 18 Pro rows,
"add extra 50000 MMK to all of their selling prices" was answered with "The
price values shown are in USD ($) format, not MMK. The request says to 50.00
add,000 MMK. Should I 50.00 add,000 ..." — a model narrating an execution,
inventing a currency and mangling the amount.

Contracts under test:
  * A plan that ends in a failed step never reaches the model for its summary.
  * The message names every failed step with the real reason.
  * Read-only multi-step plans may still use the model to explain the data.
  * A product search that matched nothing is retried once with real names.
"""

import unittest
from unittest import mock

import agent_orchestrator
from agent_orchestrator import AgentOrchestrator
from ai_agent import ChatResponse, ToolCall


SEARCH_ROWS = {
    "total_products": 4,
    "inventory": [
        {"product_id": 101, "name": "Iphone 18 pro Glacier", "barcode": "7583949",
         "category": "512 GB", "current_stock": 10, "price": "5300000.00",
         "cost": "4800000.00", "status": "low_stock"},
        {"product_id": 102, "name": "Iphone 18 pro max Glacier", "barcode": "7583945",
         "category": "256 GB", "current_stock": 10, "price": "5000000.00",
         "cost": "4600000.00", "status": "low_stock"},
    ],
}

PLAN = {
    "description": "add 50000 to the price of each iphone 18 pro",
    "steps": [
        {"step": 1, "tool": "search_products", "args": {"query": "iphone 18 pro"}},
        {"step": 2, "tool": "update_product_price",
         "args": {"product_id": 101, "price": 5350000}},
    ],
}

class SummaryTestCase(unittest.TestCase):
    """Orchestrator wired to an MMK store, with the model stubbed out."""

    def setUp(self):
        agent_orchestrator.reset_orchestrator()
        self.orchestrator = AgentOrchestrator(
            None, {},
            get_setting_func=lambda key, default=None: (
                "MMK" if key == 'currency_code' else default))

    def tearDown(self):
        agent_orchestrator.reset_orchestrator()

    def _no_model(self):
        return mock.patch.object(
            self.orchestrator, "_planner_chat",
            side_effect=AssertionError("the model must not narrate this turn"))


class DeterministicFailureTests(SummaryTestCase):
    def test_failed_step_reports_the_real_reason_without_the_model(self):
        step_results = [
            {"step": 1, "tool": "search_products", "status": "ok", "result": SEARCH_ROWS},
            {"step": 2, "tool": "update_product_price", "status": "failed",
             "error": "Product with ID 9 not found in the active branch"},
            {"step": 3, "tool": "update_product_price", "status": "skipped"},
        ]
        with self._no_model():
            message = self.orchestrator._generate_plan_summary(
                "add 50000", PLAN, step_results)
        self.assertIn("2 step(s) could not be completed", message)
        self.assertIn("Product with ID 9 not found in the active branch", message)

    def test_read_only_multi_step_plan_may_use_the_model(self):
        step_results = [
            {"step": 1, "tool": "search_products", "status": "ok", "result": SEARCH_ROWS},
            {"step": 2, "tool": "get_low_stock_items", "status": "ok",
             "result": {"items": [], "summary": {}}},
        ]
        reply = ChatResponse(content="Both look healthy.", tool_calls=[])
        with mock.patch.object(self.orchestrator, "_planner_chat", return_value=reply):
            message = self.orchestrator._generate_plan_summary(
                "compare stock", PLAN, step_results)
        self.assertIn("Both look healthy.", message)


class NumericArgumentRecoveryTests(SummaryTestCase):
    """The reported 4-step price plan must execute, not fail.

    The planner copied prices straight out of the search result, where they are
    formatted strings, so every write used to be rejected as "must have JSON
    type number" before any write could run.
    """

    def _plan(self):
        return {
            "description": "add 50000 to the price of each iphone 18 pro",
            "steps": [
                {"step": 1, "tool": "search_products", "args": {"query": "iphone 18 pro"}},
                {"step": 2, "tool": "update_product_price",
                 "args": {"product_id": 101, "price": "5350000"}},
                {"step": 3, "tool": "update_product_price",
                 "args": {"product_id": 102, "price": "5,050,000 MMK"}},
            ],
        }

    def _prepare(self, step):
        registry = self.orchestrator._get_tool_registry()
        parameters = {name: meta["params"] for name, meta in registry.items()}
        return self.orchestrator._prepare_step_arguments(
            self._plan(), step, {1: SEARCH_ROWS}, registry, parameters)

    def test_text_amount_from_the_search_result_is_accepted(self):
        args, source, error = self._prepare(self._plan()["steps"][1])
        self.assertIsNone(error)
        self.assertEqual(source, "plan")
        self.assertEqual(args["price"], 5350000)

    def test_formatted_amount_with_currency_is_accepted(self):
        args, source, error = self._prepare(self._plan()["steps"][2])
        self.assertIsNone(error)
        self.assertEqual(args["price"], 5050000)

    def test_arithmetic_text_is_reported_not_guessed(self):
        step = {"step": 2, "tool": "update_product_price",
                "args": {"product_id": 101, "price": "5300000 + 50000"}}
        args, source, error = self._prepare(step)
        self.assertIsNone(args)
        self.assertIn("must have JSON type number", error)
        self.assertIn("5300000 + 50000", error)

    def test_reported_plan_now_applies_every_price(self):
        plan = self._plan()
        calls = []

        def fake_execute(tool_calls):
            recorded = []
            for tc in tool_calls:
                calls.append((tc.function_name, dict(tc.arguments)))
                payload = SEARCH_ROWS if tc.function_name == "search_products" else {"success": True}
                recorded.append({"tool_call_id": tc.id, "function_name": tc.function_name,
                                 "result": payload, "error": None})
            return recorded

        with mock.patch.object(self.orchestrator, "_execute_tools_with_context",
                               side_effect=fake_execute):
            step_results, executed = self.orchestrator._execute_plan(plan)
        self.assertEqual([sr["status"] for sr in step_results], ["ok", "ok", "ok"])
        self.assertEqual(executed, [2, 3])
        prices = [args["price"] for name, args in calls
                  if name == "update_product_price"]
        self.assertEqual(prices, [5350000, 5050000])

    def test_failed_step_message_is_still_deterministic(self):
        plan = self._plan()
        plan["steps"][1]["args"]["price"] = "5300000 + 50000"
        results = [
            {"step": 1, "tool": "search_products", "status": "ok", "result": SEARCH_ROWS},
            {"step": 2, "tool": "update_product_price", "status": "failed",
             "error": "Invalid argument 'price' for 'update_product_price': "
                      "must have JSON type number (received the text "
                      "'5300000 + 50000')."},
            {"step": 3, "tool": "update_product_price", "status": "skipped"},
        ]
        with self._no_model():
            message = self.orchestrator._generate_plan_summary(
                "add 50000 to each", plan, results)
        self.assertIn("could not be completed", message)
        self.assertIn("5300000 + 50000", message)


class EmptySearchRetryTests(SummaryTestCase):
    EMPTY = {"function_name": "search_products", "error": None, "result": {
        "total_products": 0, "inventory": [], "query": "IOS devices",
        "branch_total": 44, "categories": ["Drinks"],
        "suggestions": ["Iphone 18 pro Glacier", "Iphone 13 pro max"]}}

    def test_retry_searches_again_with_real_names(self):
        call = ToolCall(id="retry1", function_name="search_products",
                        arguments={"query": "iphone"})
        reply = ChatResponse(content="", tool_calls=[call])
        found = [{"tool_call_id": "retry1", "function_name": "search_products",
                  "result": SEARCH_ROWS, "error": None}]
        with mock.patch.object(self.orchestrator.agent, "chat",
                               return_value=reply) as chat, \
             mock.patch.object(self.orchestrator, "_execute_tools_with_context",
                               return_value=found):
            results = self.orchestrator._retry_empty_product_search(
                [dict(self.EMPTY)], [], 0.2)
        self.assertEqual(results, found)
        prompt = chat.call_args.kwargs["message"]
        self.assertIn("Iphone 18 pro Glacier", prompt)
        self.assertIn("IOS devices", prompt)

    def test_retry_is_inert_when_no_vocabulary_is_available(self):
        empty = {"function_name": "search_products", "error": None,
                 "result": {"total_products": 0, "inventory": []}}
        original = [dict(empty)]
        with mock.patch.object(self.orchestrator.agent, "chat") as chat:
            results = self.orchestrator._retry_empty_product_search(original, [], 0.2)
        self.assertEqual(results, original)
        chat.assert_not_called()

    def test_non_empty_results_are_left_alone(self):
        found = [{"function_name": "search_products", "error": None,
                  "result": SEARCH_ROWS}]
        with mock.patch.object(self.orchestrator.agent, "chat") as chat:
            results = self.orchestrator._retry_empty_product_search(found, [], 0.2)
        self.assertEqual(results, found)
        chat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
