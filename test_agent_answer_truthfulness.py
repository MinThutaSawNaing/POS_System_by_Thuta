"""An approval decision must be described exactly, never narrated by a model.

The transcript that motivated this: after searching four iPhone 18 Pro rows,
"add extra 50000 MMK to all of their selling prices" was answered with "The
price values shown are in USD ($) format, not MMK. The request says to 50.00
add,000 MMK. Should I 50.00 add,000 ..." — a model narrating an approval,
inventing a currency and mangling the amount.

Contracts under test:
  * A plan that ends in pending writes or a failed step never reaches the model
    for its summary.
  * The message names each pending change with the real product and the amount
    in the store currency, and says plainly that nothing was applied.
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

PENDING = [
    {"step": 2, "tool": "update_product_price",
     "args": {"product_id": 101, "price": 5350000}},
]


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


class DeterministicApprovalTests(SummaryTestCase):
    def test_pending_approval_is_described_without_the_model(self):
        step_results = [{"step": 1, "tool": "search_products", "status": "ok",
                         "result": SEARCH_ROWS}]
        with self._no_model():
            message = self.orchestrator._generate_plan_summary(
                "add extra 50000 MMK to all of their selling prices",
                PLAN, step_results, PENDING)
        self.assertIn("Iphone 18 pro Glacier", message)
        self.assertIn("5,350,000.00 MMK", message)
        self.assertIn("nothing has been applied yet", message)

    def test_approval_shows_the_current_price_too(self):
        step_results = [{"step": 1, "tool": "search_products", "status": "ok",
                         "result": SEARCH_ROWS}]
        with self._no_model():
            message = self.orchestrator._generate_plan_summary(
                "add 50000", PLAN, step_results, PENDING)
        self.assertIn("currently 5,300,000.00 MMK", message)

    def test_the_data_the_user_asked_about_is_still_shown(self):
        step_results = [{"step": 1, "tool": "search_products", "status": "ok",
                         "result": SEARCH_ROWS}]
        with self._no_model():
            message = self.orchestrator._generate_plan_summary(
                "add 50000", PLAN, step_results, PENDING)
        self.assertIn("Product Search", message)
        self.assertIn("Barcode: 7583949", message)

    def test_message_never_invents_a_currency(self):
        step_results = [{"step": 1, "tool": "search_products", "status": "ok",
                         "result": SEARCH_ROWS}]
        with self._no_model():
            message = self.orchestrator._generate_plan_summary(
                "add extra 50000 MMK", PLAN, step_results, PENDING)
        self.assertNotIn("USD", message)
        self.assertNotIn("$", message)
        # The mangled amount from the reported transcript must never appear.
        self.assertNotIn("50.00 add,000", message)

    def test_amount_is_quoted_once_and_exactly(self):
        step_results = [{"step": 1, "tool": "search_products", "status": "ok",
                         "result": SEARCH_ROWS}]
        with self._no_model():
            message = self.orchestrator._generate_plan_summary(
                "add extra 50000 MMK", PLAN, step_results, PENDING)
        self.assertEqual(message.count("5,350,000.00 MMK"), 1)


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
                "add 50000", PLAN, step_results, [])
        self.assertIn("2 step(s) failed or were skipped", message)
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
                "compare stock", PLAN, step_results, [])
        self.assertIn("Both look healthy.", message)


class NumericArgumentRecoveryTests(SummaryTestCase):
    """The reported 4-step price plan must produce proposals, not failures.

    The planner copied prices straight out of the search result, where they are
    formatted strings, so every write was rejected as "must have JSON type
    number" before any proposal could reach the user.
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

    def test_reported_plan_now_ends_in_proposals(self):
        plan = self._plan()
        search = [{"tool_call_id": "plan-step-1", "function_name": "search_products",
                   "result": SEARCH_ROWS, "error": None}]
        with mock.patch.object(self.orchestrator, "_execute_tools_with_context",
                               return_value=search), \
             mock.patch.object(self.orchestrator, "_autonomy_allowed", return_value=False):
            step_results, pending = self.orchestrator._execute_plan(plan)
        self.assertEqual([sr["status"] for sr in step_results],
                         ["ok", "proposal", "proposal"])
        self.assertEqual([item["args"]["price"] for item in pending], [5350000, 5050000])

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
                "add 50000 to each", plan, results, [])
        self.assertIn("failed or were skipped", message)
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
