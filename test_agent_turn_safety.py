"""Replay hallucinated plans without touching a database or sending email."""
from unittest import mock

from test_agent_plan_execute import PlanExecuteTestBase, plan_response


class TurnSafetyTests(PlanExecuteTestBase):
    def test_barcode_request_cannot_also_change_price(self):
        plan = {"original_query": "Change its barcode to 900099", "steps": [
            {"step": 1, "tool": "update_product", "args": {
                "product_id": 22, "barcode": "900099", "price": 53000}}]}
        with mock.patch.object(self.orchestrator, "_execute_tools_with_context") as execute:
            results, writes = self.orchestrator._execute_plan(plan)
        execute.assert_not_called()
        self.assertEqual(results[0]["status"], "failed")
        self.assertEqual(writes, [])

    def test_pronoun_cannot_switch_from_beef_to_stale_zeno(self):
        self.orchestrator.session_context["last_results"] = {
            "inventory": [{"product_id": 22, "name": "Beef Curry Set"}]}
        steps = [{"step": 1, "tool": "update_product", "args": {
            "product_id": 85, "barcode": "900099"}}]
        with self.script(plan_response("wrong product", steps)), mock.patch.object(
                self.orchestrator, "_execute_tools_with_context") as execute:
            result = self.orchestrator.process_command("Change its barcode to 900099")
        execute.assert_not_called()
        self.assertFalse(result["success"])

    def test_planned_turn_keeps_user_request_for_next_followup(self):
        command = "show inventory report"
        steps = [{"step": 1, "tool": "get_inventory_status", "args": {}}]
        with self.script(plan_response("read inventory", steps)), mock.patch.object(
                self.orchestrator, "_execute_tools_with_context", return_value=[
                    {"result": {"inventory": [], "total_products": 0}}]):
            self.orchestrator.process_command(command)
        users = [m.content for m in self.orchestrator.agent.conversation_history if m.role == "user"]
        self.assertEqual(users, [command])

    def test_custom_message_request_cannot_silently_send_inventory(self):
        steps = [{"step": 1, "tool": "email_report", "args": {"report": "inventory"}}]
        with self.script(plan_response("send inventory", steps)), mock.patch.object(
                self.orchestrator, "_execute_tools_with_context") as execute:
            result = self.orchestrator.process_command(
                "Please send email to hwyyy22@gmail.com. Tell him about it.")
        execute.assert_not_called()
        self.assertFalse(result["success"])
        self.assertIn("cannot send a custom message", result["message"])

    def test_delivery_followup_allows_actual_report_delivery(self):
        steps = [{"step": 1, "tool": "email_report", "args": {"report": "inventory"}}]
        with self.script(plan_response("send inventory", steps)), mock.patch.object(
                self.orchestrator, "_execute_tools_with_context", return_value=[
                    {"result": {"status": "sent", "message": "Report sent."}}]) as execute:
            result = self.orchestrator.process_command("Send it to me too. me@example.com")
        execute.assert_called_once()
        self.assertTrue(result["success"])

    def test_email_followup_cannot_execute_stale_product_edits(self):
        steps = [{"step": 1, "tool": "update_product_price",
                  "args": {"product_id": 85, "price": 53000}}]
        with self.script(plan_response("stale edit", steps)), mock.patch.object(
                self.orchestrator, "_execute_tools_with_context") as execute:
            result = self.orchestrator.process_command(
                "Sent it to me too. minthuta2612@gmail.com")
        execute.assert_not_called()
        self.assertFalse(result["success"])

    def test_write_summary_never_invents_a_different_intended_product(self):
        results = [
            {"step": 1, "tool": "search_products", "status": "ok",
             "result": {"inventory": [{"product_id": 22, "name": "Beef Curry Set"}]}},
            {"step": 2, "tool": "update_product", "status": "ok",
             "result": {"success": True, "product_id": 22,
                        "product_name": "Beef Curry Set", "changed_fields": ["barcode"]}},
        ]
        with mock.patch.object(self.orchestrator, "_planner_chat") as model:
            message = self.orchestrator._generate_plan_summary(
                "Change its barcode to 900099", {}, results)
        model.assert_not_called()
        self.assertIn("Beef Curry Set", message)
        self.assertNotIn("Zeno", message)

    def test_embedded_business_error_aborts_later_writes(self):
        plan = {"steps": [
            {"step": 1, "tool": "update_product", "args": {"product_id": 22, "barcode": "900099"}},
            {"step": 2, "tool": "update_product_price", "args": {"product_id": 85, "price": 53000}},
        ]}
        with mock.patch.object(self.orchestrator, "_execute_tools_with_context",
                               return_value=[{"result": {"error": "Barcode already used"}}]) as execute:
            results, writes = self.orchestrator._execute_plan(plan)
        self.assertEqual([r["status"] for r in results], ["failed", "skipped"])
        self.assertEqual(writes, [])
        self.assertEqual(execute.call_count, 1)

    def test_wrong_product_id_is_blocked_after_search(self):
        plan = {"original_query": "Change its barcode to 900099", "steps": [
            {"step": 1, "tool": "search_products", "args": {"query": "beef"}},
            {"step": 2, "tool": "update_product", "args": {"product_id": 85, "barcode": "900099"}},
        ]}
        with mock.patch.object(self.orchestrator, "_execute_tools_with_context",
                               return_value=[{"result": {"inventory": [
                                   {"product_id": 22, "name": "Beef Curry Set"}]}}]) as execute:
            results, writes = self.orchestrator._execute_plan(plan)
        self.assertEqual([r["status"] for r in results], ["ok", "failed"])
        self.assertEqual(writes, [])
        self.assertEqual(execute.call_count, 1)