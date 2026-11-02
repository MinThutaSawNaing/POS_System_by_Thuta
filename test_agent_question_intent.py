"""Question-aware answers must not collapse into keyword-selected reports."""

import unittest
from unittest import mock

from ai_agent import ChatResponse, ToolCall
from test_agent_smarts import SmartsTestBase


class QuestionIntentTests(SmartsTestBase):
    def inventory(self, rows, total=None):
        return [{"function_name": "get_inventory_status", "error": None,
                 "result": {"total_products": len(rows) if total is None else total,
                            "inventory": rows}}]

    def test_price_extreme_checks_every_row_and_reports_ties(self):
        rows = [{"name": f"Ordinary {i}", "price": "9,999.00"} for i in range(12)]
        rows += [{"name": name, "price": "5,300,000.00", "barcode": name,
                  "current_stock": 10} for name in ("Glacier", "Purple")]
        with mock.patch.object(self.orchestrator, "_planner_chat") as model:
            answer = self.orchestrator._answer_tool_results(
                self.inventory(rows), "Which product is the most expensive in my inventory?")
        self.assertIn("Glacier", answer)
        self.assertIn("Purple", answer)
        self.assertIn("2 products are tied", answer)
        self.assertNotIn("Ordinary", answer)
        self.assertNotIn("Inventory Status", answer)
        model.assert_not_called()

    def test_cheapest_uses_numeric_not_lexical_comparison(self):
        answer = self.orchestrator._answer_tool_results(self.inventory([
            {"name": "Cheap", "price": "9.00"}, {"name": "Dear", "price": "100.00"}]),
            "Which is cheapest?")
        self.assertIn("Cheap", answer)
        self.assertNotIn("Dear", answer)

    def test_ranked_or_cost_questions_are_not_reduced_to_one_selling_price(self):
        for query in ("top 5 most expensive products", "highest cost product",
                      "most expensive by cost"):
            self.assertIsNone(self.orchestrator._price_extreme(query))

    def test_followup_price_question_still_requires_live_data(self):
        self.orchestrator.agent.add_user_message("What is in my inventory?")
        self.orchestrator.agent.add_assistant_message("There are several products.")
        with mock.patch.object(self.orchestrator.agent, "chat",
                               return_value=ChatResponse(content="An invented product costs 9000.")) as chat:
            result = self.orchestrator.process_command("I want to know the most expensive one.")
        self.assertEqual(chat.call_count, 2)
        self.assertTrue(chat.call_args.kwargs["force_tool_call"])
        self.assertNotIn("invented product", result["message"])

    def test_partial_and_missing_prices_never_establish_extreme(self):
        for data in (self.inventory([{"name": "X", "price": "2"}], total=3),
                     self.inventory([{"name": "X"}]),
                     self.inventory([{"name": "X", "price": "NaN"}])):
            answer = self.orchestrator._answer_tool_results(data, "most expensive product")
            self.assertIn("can't reliably", answer)

    def test_empty_inventory_is_explicit(self):
        self.assertIn("No products matched", self.orchestrator._answer_tool_results(
            self.inventory([]), "least expensive product"))

    def test_planned_extreme_uses_full_results_before_summary_compaction(self):
        rows = [{"name": f"Ordinary {i}", "price": "1"} for i in range(80)]
        rows.append({"name": "Last and highest", "price": "9999"})
        steps = [{"step": 1, "tool": "get_inventory_status", "status": "ok",
                  "result": self.inventory(rows)[0]["result"]}]
        with mock.patch.object(self.orchestrator, "_planner_chat") as model:
            answer = self.orchestrator._generate_plan_summary(
                "show the most expensive product in inventory", {}, steps)
        self.assertIn("Last and highest", answer)
        model.assert_not_called()

    def test_analytical_answer_receives_original_question_and_full_evidence(self):
        data = self.inventory([{"name": f"Product {i}", "price": "3"} for i in range(20)])
        with mock.patch.object(self.orchestrator, "_planner_chat",
                               return_value=ChatResponse(content="The average selling price is 3.")) as model:
            answer = self.orchestrator._answer_tool_results(data, "What is the average price in inventory?")
        self.assertEqual(answer, "The average selling price is 3.")
        prompt = model.call_args.kwargs["message"]
        self.assertIn("Product 19", prompt)
        self.assertIn("What is the average price in inventory?", prompt)
        self.assertIsNone(model.call_args.kwargs["tools"])

    def test_analysis_failure_does_not_disguise_itself_as_overview(self):
        with mock.patch.object(self.orchestrator, "_planner_chat",
                               return_value=ChatResponse(content="", error="offline")):
            answer = self.orchestrator._answer_tool_results(
                self.inventory([{"name": "X", "price": "2"}]), "average inventory price?")
        self.assertIn("couldn't reliably", answer)
        self.assertNotIn("Inventory Status", answer)

    def test_fallback_does_not_guess_from_topic_mentions(self):
        queries = ["Which product is most expensive in inventory?",
                   "What does inventory mean?", "delete all inventory",
                   "show products in the helmets category", "customers with overdue debts",
                   "warehouse stock received yesterday", "inventory is confusing"]
        with mock.patch.object(self.orchestrator, "_run_fallback_tool") as run:
            for query in queries:
                self.assertIsNone(self.orchestrator._fallback_intent_detection(query), query)
        run.assert_not_called()

    def test_explicit_overview_uses_correct_tool(self):
        for query, name in (("show me the inventory status", "get_inventory_status"),
                            ("warehouse stock", "get_warehouse_inventory"),
                            ("show product categories", "get_category_summary")):
            with mock.patch.object(self.orchestrator, "_run_fallback_tool", return_value="real data") as run:
                self.assertEqual(self.orchestrator._fallback_intent_detection(query), "real data")
                run.assert_called_once_with(name)

    def test_reported_question_end_to_end_avoids_generic_stock_report(self):
        data = self.inventory([{"name": "Glacier", "price": "5300000.00"},
                               {"name": "Cheap", "price": "100.00"}])
        response = ChatResponse(content="", tool_calls=[ToolCall(
            id="read", function_name="get_inventory_status", arguments={})])
        with mock.patch.object(self.orchestrator.agent, "chat", return_value=response), \
             mock.patch.object(self.orchestrator, "_execute_tools_with_context", return_value=data):
            result = self.orchestrator.process_command("Which product is the most expensive in my inventory?")
        self.assertTrue(result["success"])
        self.assertIn("Glacier", result["message"])
        self.assertNotIn("Inventory Status", result["message"])


if __name__ == "__main__":
    unittest.main()