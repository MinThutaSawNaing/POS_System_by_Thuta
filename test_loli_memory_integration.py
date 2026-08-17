"""Focused contract coverage for Loli's optional persistent-memory integration."""

import unittest
from pathlib import Path

import agent_orchestrator


class LoliMemoryIntegrationTests(unittest.TestCase):
    def tearDown(self):
        agent_orchestrator.reset_orchestrator()

    def test_scoped_recall_is_bounded_and_backend_failure_is_inert(self):
        class FakeMemory:
            def __init__(self):
                self.calls = []

            def build_context(self, query, **kwargs):
                self.calls.append((query, kwargs))
                return "- User prefers concise stock reports"

        orchestrator = agent_orchestrator.AgentOrchestrator(object(), {})
        service = FakeMemory()
        orchestrator.memory_service = service
        orchestrator.set_request_context({"user_id": 9, "branch_id": 4})

        self.assertEqual(
            orchestrator._build_memory_context("show stock", user_id=999),
            "- User prefers concise stock reports",
        )
        self.assertEqual(service.calls, [("show stock", {
            "user_id": 9, "branch_id": 4, "limit": 5, "max_characters": 1500,
        })])

        class FailingMemory:
            def build_context(self, *_args, **_kwargs):
                raise RuntimeError("unavailable")

        orchestrator.memory_service = FailingMemory()
        self.assertEqual(orchestrator._build_memory_context("show stock", user_id=9), "")

    def test_widget_uses_guarded_memory_management_endpoints(self):
        template = Path("templates/ai_agent_widget.html").read_text(encoding="utf-8")
        self.assertIn("/api/agent/memories", template)
        self.assertIn("method:'DELETE'", template)
        self.assertIn("loliManageMemory", template)
        self.assertIn("Only save details you want Loli to recall later.", template)


if __name__ == "__main__":
    unittest.main()