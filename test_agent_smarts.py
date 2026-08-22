"""Contract tests for the agent reliability improvements ("make Loli smarter").

Contracts under test:
  * SYSTEM_PROMPT contains an explicit tool-use policy section.
  * Category keyword filtering can never hide the core lookup tools, and
    mutating tools are still stripped from the single-shot tool list.
  * Data-query chat turns decode at low temperature (deterministic tool
    selection); pure chat keeps the default temperature.
  * The planner router sends multi-category and comparison requests through
    plan-then-execute while simple single-category questions stay single-shot.
  * AIAgent defaults to DeepSeek V4 Flash and honours the AI_MODEL override.

No real API calls and no database access: chat is stubbed at the
agent.chat boundary (style of test_ai_no_mock_data.py).
"""

import unittest
from unittest import mock

import ai_agent
import agent_orchestrator
from agent_orchestrator import AgentOrchestrator, CORE_TOOL_NAMES, SYSTEM_PROMPT
from ai_agent import AIAgent, ChatResponse


class SmartsTestBase(unittest.TestCase):
    def setUp(self):
        agent_orchestrator.reset_orchestrator()
        self.orchestrator = AgentOrchestrator(None, {})
        self.orchestrator.set_request_context({"branch_id": 1, "user_id": 1, "role": "manager"})

    def tearDown(self):
        agent_orchestrator.reset_orchestrator()


class SystemPromptPolicyTests(SmartsTestBase):
    def test_system_prompt_teaches_tool_usage(self):
        self.assertIn("Tool use policy", SYSTEM_PROMPT)
        # The policy must demand a tool call for live-data questions...
        self.assertIn("never from memory", SYSTEM_PROMPT)
        # ...and teach chaining plus honest failure handling.
        self.assertIn("Chain tools", SYSTEM_PROMPT)
        self.assertIn("Never substitute invented numbers", SYSTEM_PROMPT)

    def test_applied_system_prompt_keeps_policy(self):
        applied = next(
            m.content for m in self.orchestrator.agent.conversation_history
            if m.role == "system"
        )
        self.assertIn("Tool use policy", applied)
        self.assertIn("Current Date:", applied)


class ToolExposureTests(SmartsTestBase):
    def test_core_tools_survive_narrow_category_filter(self):
        # "cancel purchase order" matches only the purchase_order category,
        # which alone would hide every inventory/product lookup tool.
        schemas = self.orchestrator._filter_tools_for_query("cancel purchase order")
        names = {s["function"]["name"] for s in schemas}
        for core in CORE_TOOL_NAMES:
            self.assertIn(core, names)

    def test_filtering_still_never_exposes_mutating_tools(self):
        for command in ("cancel purchase order",
                        "how much Cola stock do we have",
                        "show me suppliers with overdue debts"):
            schemas = self.orchestrator._filter_tools_for_query(command)
            names = {s["function"]["name"] for s in schemas}
            self.assertNotIn("create_purchase_order", names, msg=command)
            self.assertNotIn("approve_purchase_order", names, msg=command)

    def test_debt_and_supplier_query_sees_both_domains(self):
        schemas = self.orchestrator._filter_tools_for_query(
            "how much do I owe supplier Acme")
        names = {s["function"]["name"] for s in schemas}
        self.assertIn("get_supplier_list", names)
        self.assertIn("get_debt_summary", names)


# === PART 2 ===

class TemperatureContractTests(SmartsTestBase):
    def _run_command(self, command, scripted):
        temperatures = []

        def fake_chat(message=None, tools_override=None, force_tool_call=False, **kwargs):
            temperatures.append(kwargs.get("temperature"))
            if message:
                self.orchestrator.agent.add_user_message(message)
            response = scripted.pop(0)
            self.orchestrator.agent.add_assistant_message(response.content, None)
            return response

        with mock.patch.object(self.orchestrator.agent, "chat", side_effect=fake_chat), \
             mock.patch.object(self.orchestrator, "_fallback_intent_detection", return_value=None):
            result = self.orchestrator.process_command(command, user_id=1)
        return result, temperatures

    def test_data_query_decodes_at_low_temperature(self):
        no_tool_call = ChatResponse(content="", tool_calls=[], finish_reason="stop")
        result, temperatures = self._run_command(
            "how much Cola stock do we have", [no_tool_call, no_tool_call])
        self.assertTrue(result["success"])
        # First call AND forced retry both run deterministically cold.
        self.assertEqual(temperatures[0], 0.2)
        self.assertEqual(temperatures[1], 0.2)

    def test_pure_chat_keeps_default_temperature(self):
        result, temperatures = self._run_command(
            "who created you?",
            [ChatResponse(content="I am Loli.", tool_calls=[], finish_reason="stop")])
        self.assertTrue(result["success"])
        self.assertEqual(temperatures[0], 0.7)


class RouterTests(SmartsTestBase):
    def test_multi_category_request_routes_to_planning(self):
        # supplier + debt categories in one request -> plan-then-execute.
        self.assertTrue(self.orchestrator._should_plan(
            "which supplier do we owe the most"))

    def test_comparison_word_routes_to_planning(self):
        self.assertTrue(self.orchestrator._should_plan(
            "compare cola stock with fanta stock"))

    def test_single_category_question_stays_single_shot(self):
        self.assertFalse(self.orchestrator._should_plan(
            "how much Cola stock do we have"))

    def test_pure_chat_never_plans(self):
        self.assertFalse(self.orchestrator._should_plan("who created you?"))
        self.assertFalse(self.orchestrator._should_plan(""))
        self.assertFalse(self.orchestrator._should_plan("   "))


class ModelConfigTests(unittest.TestCase):
    def test_default_model_is_deepseek_v4_flash(self):
        self.assertEqual(ai_agent.DEFAULT_MODEL, "deepseek/deepseek-v4-flash")
        agent = AIAgent(api_key="test-key")
        self.assertEqual(agent.model, "deepseek/deepseek-v4-flash")

    def test_ai_model_env_var_overrides_default(self):
        with mock.patch.dict("os.environ", {"AI_MODEL": "google/gemini-2.5-flash-lite"}):
            agent = AIAgent(api_key="test-key")
        self.assertEqual(agent.model, "google/gemini-2.5-flash-lite")

    def test_explicit_model_argument_wins(self):
        agent = AIAgent(api_key="test-key", model="vendor/custom-model")
        self.assertEqual(agent.model, "vendor/custom-model")


if __name__ == "__main__":
    unittest.main()
