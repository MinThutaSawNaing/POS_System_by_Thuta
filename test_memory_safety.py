import unittest

from ai_agent import AIAgent
import agent_orchestrator


class AgentMemorySafetyTests(unittest.TestCase):
    def tearDown(self):
        agent_orchestrator.reset_orchestrator()

    def test_history_is_bounded_and_keeps_system_prompt(self):
        agent = AIAgent(api_key="test-key")
        agent.set_system_prompt("system")
        for index in range(30):
            agent.add_user_message(f"user-{index}")
            agent.add_assistant_message(f"assistant-{index}")

        agent.trim_history(10)

        self.assertEqual(agent.conversation_history[0].role, "system")
        self.assertLessEqual(len(agent.conversation_history), 11)
        self.assertEqual(agent.conversation_history[-1].content, "assistant-29")

    def test_orchestrators_are_isolated_by_conversation_owner(self):
        class FakeDb:
            pass

        first = agent_orchestrator.get_orchestrator(FakeDb(), {}, conversation_id=1)
        second = agent_orchestrator.get_orchestrator(FakeDb(), {}, conversation_id=2)
        first.agent.add_user_message("private conversation")

        self.assertIs(first, agent_orchestrator.get_orchestrator(conversation_id=1))
        self.assertIsNot(first, second)
        self.assertNotIn(
            "private conversation",
            [message.content for message in second.agent.conversation_history],
        )


if __name__ == "__main__":
    unittest.main()