"""Regression tests: Loli must never answer data questions with model-invented
numbers. Every data answer must come from real database tool results."""

import unittest
from unittest import mock

import agent_orchestrator
from agent_orchestrator import AgentOrchestrator
from ai_agent import ChatResponse, ToolCall


class NoMockDataContractTests(unittest.TestCase):
    def setUp(self):
        agent_orchestrator.reset_orchestrator()
        self.orchestrator = AgentOrchestrator(None, {})
        self.orchestrator.set_request_context({"branch_id": 1, "user_id": 1, "role": "manager"})

    def tearDown(self):
        agent_orchestrator.reset_orchestrator()

    def test_data_query_without_tool_call_never_returns_model_text(self):
        # The model answers a data question with invented numbers and NO tool call.
        hallucinated = "You have 42,000 units of Cola in stock."
        tool_call_message = [{
            "id": "c1", "type": "function",
            "function": {"name": "get_inventory_status", "arguments": "{}"},
        }]
        responses = [
            ChatResponse(content=hallucinated, tool_calls=[], finish_reason="stop"),
            ChatResponse(content="", tool_calls=[ToolCall(id="c1", function_name="get_inventory_status", arguments={})],
                         finish_reason="tool_calls"),
        ]
        chat_calls = []

        def fake_chat(message=None, tools_override=None, force_tool_call=False, **kwargs):
            chat_calls.append({"message": message, "force_tool_call": force_tool_call})
            if message:
                self.orchestrator.agent.add_user_message(message)
            response = responses.pop(0)
            raw_calls = tool_call_message if response.tool_calls else None
            self.orchestrator.agent.add_assistant_message(response.content, raw_calls)
            return response

        real_results = [{
            "tool_call_id": "c1",
            "function_name": "get_inventory_status",
            "result": {"total_products": 1, "inventory": [
                {"product_id": 1, "name": "Cola", "barcode": "x", "category": "drink",
                 "current_stock": 47, "reorder_point": 10, "reorder_enabled": False,
                 "status": "ok", "price": 1.5, "cost": 0.9}]},
            "error": None,
        }]

        with mock.patch.object(self.orchestrator.agent, "chat", side_effect=fake_chat), \
             mock.patch.object(self.orchestrator, "_execute_tools_with_context", return_value=real_results):
            result = self.orchestrator.process_command("how much Cola stock do we have", user_id=1)

        self.assertTrue(result["success"])
        # The invented figure must never reach the user.
        self.assertNotIn(hallucinated, result["message"])
        # The answer must be built from the real database result.
        self.assertIn("Cola", result["message"])
        self.assertIn("47", result["message"])
        # The forced tool-call retry must have been requested.
        self.assertEqual(len(chat_calls), 2)
        self.assertFalse(chat_calls[0]["force_tool_call"])
        self.assertTrue(chat_calls[1]["force_tool_call"])
        self.assertIsNone(chat_calls[1]["message"])  # no duplicate user message

    def test_non_data_chat_response_is_still_passed_through(self):
        # Pure chat / identity questions should work exactly as before.
        identity = "I am Loli, the AI assistant created by Min Thuta Saw Naing."
        chat_calls = []

        def fake_chat(message=None, tools_override=None, force_tool_call=False, **kwargs):
            chat_calls.append({"message": message, "force_tool_call": force_tool_call})
            if message:
                self.orchestrator.agent.add_user_message(message)
            response = ChatResponse(content=identity, tool_calls=[], finish_reason="stop")
            self.orchestrator.agent.add_assistant_message(response.content, None)
            return response

        with mock.patch.object(self.orchestrator.agent, "chat", side_effect=fake_chat):
            result = self.orchestrator.process_command("who created you?", user_id=1)

        self.assertTrue(result["success"])
        self.assertIn(identity, result["message"])
        self.assertEqual(len(chat_calls), 1)
        self.assertFalse(chat_calls[0]["force_tool_call"])

    def test_fallback_intent_detection_returns_real_data(self):
        with mock.patch.object(self.orchestrator.ai_tools, "get_supplier_list", return_value={
            "total_suppliers": 2,
            "suppliers": [
                {"name": "Acme Wholesale", "phone": "555-0100", "quality_rating": 4.5},
                {"name": "Beta Traders", "phone": None, "quality_rating": 0},
            ],
        }):
            message = self.orchestrator._fallback_intent_detection("show me the suppliers")

        self.assertIsNotNone(message)
        self.assertIn("Acme Wholesale", message)
        self.assertIn("Beta Traders", message)

    def test_short_keywords_need_word_boundaries(self):
        contains_any = AgentOrchestrator._contains_any
        self.assertTrue(contains_any("show my po", ["po"]))
        self.assertFalse(contains_any("suppose we check", ["po"]))
        self.assertTrue(contains_any("show purchase orders", ["purchase order"]))


class ChatPayloadTests(unittest.TestCase):
    def test_chat_payload_sends_required_tool_choice_when_forced(self):
        from ai_agent import AIAgent

        agent = AIAgent(api_key="test-key")
        agent.register_tool("get_inventory_status", "Get inventory", {"type": "object", "properties": {}},
                            lambda: {})
        captured = {}

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
                        "usage": {}}

        def fake_post(url, headers, json, timeout):
            captured.update(url=url, json=json)
            return FakeResponse()

        with mock.patch("ai_agent.requests.post", side_effect=fake_post):
            agent.chat("hello", force_tool_call=True)
        self.assertEqual(captured["json"]["tool_choice"], "required")

        with mock.patch("ai_agent.requests.post", side_effect=fake_post):
            agent.chat("hello", force_tool_call=False)
        self.assertEqual(captured["json"]["tool_choice"], "auto")


if __name__ == "__main__":
    unittest.main()

