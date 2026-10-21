"""Contracts that stop the agent from calling APIs that do not exist.

The failure this guards against: the model invents a tool name, nothing is
executed, and the user either sees a raw error or a plausible answer built from
nothing. Every test here is LLM-free and database-free.
"""

import json
import unittest
from unittest import mock

import agent_orchestrator
from agent_orchestrator import AgentOrchestrator
from ai_agent import AIAgent, ChatResponse, ToolCall
from ai_tool_guard import guard_tool_call, schema_parameters


SCHEMAS = [{
    "type": "function",
    "function": {
        "name": "get_inventory_status",
        "description": "Current stock levels.",
        "parameters": {"type": "object",
                       "properties": {"product_id": {"type": "integer"}}},
    },
}]

INVENTORY = {
    "total_products": 1,
    "inventory": [{"product_id": 1, "name": "Cola", "barcode": "x",
                   "category": "drink", "current_stock": 47,
                   "reorder_point": 10, "reorder_enabled": False,
                   "status": "ok", "price": 1.5, "cost": 0.9}],
}


class GuardUnitTests(unittest.TestCase):
    def test_unknown_tool_is_rejected_with_the_real_catalog(self):
        result = guard_tool_call("get_sales_forecast", {},
                                 ["get_inventory_status", "get_sales_summary"])
        self.assertFalse(result.ok)
        self.assertIn("Unknown tool 'get_sales_forecast'", result.error)
        self.assertIn("get_inventory_status", result.error)
        self.assertIn("get_sales_summary", result.error)

    def test_close_match_is_suggested(self):
        result = guard_tool_call("get_inventory_statuss", {}, ["get_inventory_status"])
        self.assertFalse(result.ok)
        self.assertIn("Did you mean 'get_inventory_status'?", result.error)

    def test_undocumented_arguments_are_rejected_before_execution(self):
        result = guard_tool_call(
            "get_inventory_status",
            {"product_id": 4, "date_range": "last year"},
            ["get_inventory_status"], schema_parameters(SCHEMAS))
        self.assertFalse(result.ok)
        self.assertIn('date_range', result.error)
        self.assertEqual(result.arguments, {})

    def test_required_types_enums_and_bounds_are_validated(self):
        params = {'report': {'properties': {
            'days': {'type': 'integer', 'minimum': 1, 'maximum': 365},
            'all_time': {'type': 'boolean'},
            'mode': {'type': 'string', 'enum': ['gross', 'net']},
        }, 'required': ['days']}}
        for args in ({}, {'days': True}, {'days': '30'}, {'days': 0},
                     {'days': 366}, {'days': 30, 'all_time': 'true'},
                     {'days': 30, 'mode': 'invented'}):
            with self.subTest(args=args):
                self.assertFalse(guard_tool_call('report', args, ['report'], params).ok)
        self.assertTrue(guard_tool_call('report', {'days': 30, 'all_time': True}, ['report'], params).ok)

    def test_arguments_must_be_a_json_object(self):
        self.assertFalse(guard_tool_call(
            "get_inventory_status", "not-json", ["get_inventory_status"]).ok)
        self.assertFalse(guard_tool_call(
            "get_inventory_status", [1, 2], ["get_inventory_status"]).ok)

    def test_unparseable_arguments_are_rejected(self):
        result = guard_tool_call("get_inventory_status", {}, ["get_inventory_status"],
                                 schema_parameters(SCHEMAS),
                                 parse_error="Expecting value: line 1 column 1")
        self.assertFalse(result.ok)
        self.assertIn("not valid JSON", result.error)


class OrchestratorHallucinationTests(unittest.TestCase):
    def setUp(self):
        agent_orchestrator.reset_orchestrator()
        self.orchestrator = AgentOrchestrator(None, {})
        self.orchestrator.set_request_context(
            {"branch_id": 1, "user_id": 1, "role": "manager"})

    def tearDown(self):
        agent_orchestrator.reset_orchestrator()

    def _script(self, responses):
        """Answer agent.chat with the given responses, recording each message."""
        calls = []

        def fake_chat(message=None, tools_override=None, force_tool_call=False, **kwargs):
            calls.append({"message": message, "force_tool_call": force_tool_call})
            if message:
                self.orchestrator.agent.add_user_message(message)
            response = responses.pop(0)
            raw = [{"id": tc.id, "type": "function",
                    "function": {"name": tc.function_name,
                                 "arguments": json.dumps(tc.arguments)}}
                   for tc in response.tool_calls]
            self.orchestrator.agent.add_assistant_message(response.content, raw or None)
            return response

        return calls, fake_chat

    def test_invented_tool_is_rejected_then_repaired_with_a_real_tool(self):
        invented = ChatResponse(
            content="",
            tool_calls=[ToolCall(id="bad-1", function_name="get_sales_forecast",
                                 arguments={"days": 30})],
            finish_reason="tool_calls")
        corrected = ChatResponse(
            content="",
            tool_calls=[ToolCall(id="good-1", function_name="get_inventory_status",
                                 arguments={})],
            finish_reason="tool_calls")
        calls, fake_chat = self._script([invented, corrected])

        with mock.patch.object(self.orchestrator.agent, "chat", side_effect=fake_chat), \
             mock.patch.dict(self.orchestrator.agent.tool_functions,
                             {"get_inventory_status": lambda **kwargs: INVENTORY}):
            result = self.orchestrator.process_command(
                "how much cola stock do we have", user_id=1)

        self.assertTrue(result["success"])
        self.assertIn("Cola", result["message"])
        self.assertIn("47", result["message"])
        self.assertNotIn("get_sales_forecast", result["message"])
        # One original turn plus exactly one corrective retry.
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[1]["message"])

        # The invented call still received a tool result, so the conversation
        # sent upstream stays valid (no dangling tool call).
        tool_messages = [m for m in self.orchestrator.agent.conversation_history
                         if m.role == "tool"]
        self.assertTrue(any(m.tool_call_id == "bad-1" and "Unknown tool" in m.content
                            for m in tool_messages))

    def test_unrepaired_hallucination_never_returns_model_numbers(self):
        invented = ChatResponse(
            content="You have 42,000 units of Cola in stock.",
            tool_calls=[ToolCall(id="bad-1", function_name="get_sales_forecast",
                                 arguments={})],
            finish_reason="tool_calls")
        repair = ChatResponse(content="You have 42,000 units.", tool_calls=[],
                              finish_reason="stop")
        calls, fake_chat = self._script([invented, repair])

        with mock.patch.object(self.orchestrator.agent, "chat", side_effect=fake_chat):
            result = self.orchestrator.process_command(
                "how much cola stock do we have", user_id=1)

        self.assertNotIn("42,000", result["message"])
        self.assertIn("does not", result["message"])
        self.assertEqual(len(calls), 2)

    def test_tool_not_offered_for_this_turn_is_never_executed(self):
        called = []
        self.orchestrator.agent.tool_functions["delete_product"] = (
            lambda **kwargs: called.append(kwargs) or {"success": True})

        results = self.orchestrator._execute_tools_with_context(
            [ToolCall(id="x1", function_name="delete_product", arguments={"product_id": 7})],
            allowed_names={"get_inventory_status"})

        self.assertEqual(called, [])
        self.assertTrue(results[0]["error"])
        self.assertTrue(results[0]["rejected"])
        self.assertIn("not offered", results[0]["error"])

    def test_undocumented_arguments_are_rejected_at_execution(self):
        received = {}
        self.orchestrator.agent.tool_functions["get_inventory_status"] = (
            lambda **kwargs: received.update(kwargs) or {"total_products": 0})

        results = self.orchestrator._execute_tools_with_context(
            [ToolCall(id="a1", function_name="get_inventory_status",
                      arguments={"product_id": 1, "date_range": "last year"})],
            allowed_names={"get_inventory_status"})

        self.assertIn('date_range', results[0]["error"])
        self.assertEqual(received, {})

    def test_plan_validation_rejects_invented_tool_names(self):
        registry = {"get_inventory_status": {"mutates": False, "one_line": "",
                                             "params": {}}}
        steps, error, fatal = self.orchestrator._validate_plan(
            [{"tool": "get_sales_forecast", "args": {}}], registry)
        self.assertIsNone(steps)
        self.assertIn("unknown tool", error)
        self.assertTrue(fatal)

    def test_planner_catalog_includes_actual_parameter_semantics(self):
        catalog = self.orchestrator._build_planning_catalog(self.orchestrator._get_tool_registry())
        self.assertIn('all_time', catalog)
        self.assertIn('overrides days', catalog)
        self.assertIn('"type":"boolean"', catalog)
        self.assertIn('"maximum":100', catalog)

    def test_all_time_best_seller_fallback_preserves_scope(self):
        with mock.patch.object(self.orchestrator, '_run_fallback_tool', return_value='sales') as run:
            self.orchestrator._fallback_intent_detection('Which product sells best of all time?')
        run.assert_called_once_with('get_sales_trends', all_time=True)

    def test_plan_validation_rejects_undocumented_arguments(self):
        registry = {"get_inventory_status": {
            "mutates": False, "one_line": "",
            "params": {"properties": {"product_id": {"type": "integer"}}}}}
        steps, error, fatal = self.orchestrator._validate_plan(
            [{"tool": "get_inventory_status", "args": {"days": 30}}], registry)
        self.assertIsNone(steps)
        self.assertIn("days", error)
        self.assertFalse(fatal)


class AgentExecutionGuardTests(unittest.TestCase):
    def test_unparseable_arguments_never_run_the_tool(self):
        agent = AIAgent(api_key="test-key")
        calls = []
        agent.register_tool("get_inventory_status", "Current stock.",
                            {"type": "object", "properties": {}},
                            lambda **kwargs: calls.append(kwargs) or {})

        results = agent.execute_tool_calls([
            ToolCall(id="bad-json", function_name="get_inventory_status",
                     arguments={}, parse_error="Expecting value")])

        self.assertTrue(results[0]["rejected"])
        self.assertIn("not valid JSON", results[0]["error"])
        self.assertEqual(calls, [])

    def test_unknown_tool_adds_a_tool_result_and_reports_rejection(self):
        agent = AIAgent(api_key="test-key")
        agent.register_tool("get_inventory_status", "Current stock.",
                            {"type": "object", "properties": {}}, lambda: {})

        results = agent.execute_tool_calls(
            [ToolCall(id="nope-1", function_name="get_sales_forecast", arguments={})])

        self.assertTrue(results[0]["rejected"])
        self.assertIn("Unknown tool", results[0]["error"])
        tool_messages = [m for m in agent.conversation_history if m.role == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(tool_messages[0].tool_call_id, "nope-1")
        self.assertIn("Unknown tool", tool_messages[0].content)


if __name__ == "__main__":
    unittest.main()

