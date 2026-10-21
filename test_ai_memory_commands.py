"""Contracts for the user-facing memory layer.

Two promises are tested here:
  * ``#remember <detail>`` stores a pinned (always-on) memory deterministically,
    without spending an LLM call and without letting the model invent the result.
  * A pinned memory reaches the system prompt on every later turn, including
    turns whose text shares no keywords with it.
"""

import unittest
from unittest import mock

import agent_orchestrator
from agent_orchestrator import AgentOrchestrator
from ai_memory_service import MemoryService


class MemoryCommandTests(unittest.TestCase):
    def setUp(self):
        agent_orchestrator.reset_orchestrator()
        self.orchestrator = AgentOrchestrator(None, {})
        self.orchestrator.set_request_context(
            {"branch_id": 7, "user_id": 3, "role": "manager"})
        self.service = mock.Mock()
        self.service.db = None
        self.service.registry_model = None
        self.service.audit_model = None
        self.orchestrator.memory_service = self.service

    def tearDown(self):
        agent_orchestrator.reset_orchestrator()

    def test_remember_saves_a_pinned_memory_without_calling_the_model(self):
        self.service.remember.return_value = {
            "saved": True, "memory_id": "m-1", "summary": "Reports use MMK"}

        with mock.patch.object(self.orchestrator.agent, "chat") as chat:
            result = self.orchestrator.process_command(
                "#remember Reports use MMK", user_id=3)

        chat.assert_not_called()
        self.assertTrue(result["success"])
        self.assertIn("Reports use MMK", result["message"])
        kwargs = self.service.remember.call_args.kwargs
        self.assertTrue(kwargs["pinned"])
        self.assertEqual(kwargs["user_id"], 3)
        self.assertEqual(kwargs["branch_id"], 7)
        self.assertEqual(kwargs["scope"], "private")

    def test_plus_and_that_forms_are_accepted(self):
        self.service.remember.return_value = {
            "saved": True, "memory_id": "m-2", "summary": "I price in MMK"}
        self.orchestrator.process_command("#remember+ I price in MMK", user_id=3)
        self.assertEqual(self.service.remember.call_args.args[0], "I price in MMK")

        self.service.remember.reset_mock()
        self.orchestrator.process_command("#remember that I price in MMK", user_id=3)
        self.assertEqual(self.service.remember.call_args.args[0], "I price in MMK")

    def test_empty_remember_asks_for_the_detail(self):
        result = self.orchestrator.process_command("#remember", user_id=3)
        self.assertFalse(result["success"])
        self.assertIn("#remember", result["message"])
        self.service.remember.assert_not_called()

    def test_sensitive_content_is_refused_with_the_service_reason(self):
        self.service.remember.side_effect = ValueError(
            "sensitive data must not be stored in AI memory")
        result = self.orchestrator.process_command(
            "#remember my password is hunter2", user_id=3)
        self.assertFalse(result["success"])
        self.assertIn("sensitive", result["message"])

    def test_full_always_on_list_is_reported_honestly(self):
        self.service.remember.return_value = {
            "saved": False, "reason": "always-on memory limit reached",
            "memory_id": None}
        result = self.orchestrator.process_command("#remember another detail", user_id=3)
        self.assertFalse(result["success"])
        self.assertIn("#forget", result["message"])

    def test_memory_lists_ids_and_forget_removes_one(self):
        self.service.always_on.return_value = [
            {"id": "m-1", "memory": "Reports use MMK", "pinned": True}]
        self.service.forget.return_value = {"deleted": True}

        listed = self.orchestrator.process_command("#memory", user_id=3)
        self.assertTrue(listed["success"])
        self.assertIn("Reports use MMK", listed["message"])
        self.assertIn("m-1", listed["message"])

        forgotten = self.orchestrator.process_command("#forget m-1", user_id=3)
        self.assertTrue(forgotten["success"])
        self.assertEqual(self.service.forget.call_args.kwargs["memory_id"], "m-1")
        self.assertEqual(self.service.forget.call_args.kwargs["user_id"], 3)

    def test_forget_of_an_unknown_id_does_not_claim_success(self):
        self.service.forget.return_value = {"deleted": False, "reason": "memory not found"}
        result = self.orchestrator.process_command("#forget missing-id", user_id=3)
        self.assertFalse(result["success"])
        self.assertIn("missing-id", result["message"])


class AlwaysOnInjectionTests(unittest.TestCase):
    def setUp(self):
        agent_orchestrator.reset_orchestrator()
        self.orchestrator = AgentOrchestrator(None, {})
        self.orchestrator.set_request_context(
            {"branch_id": 7, "user_id": 3, "role": "manager"})
        self.service = mock.Mock()
        self.service.db = None
        self.service.registry_model = None
        self.service.audit_model = None
        self.orchestrator.memory_service = self.service

    def tearDown(self):
        agent_orchestrator.reset_orchestrator()

    def test_always_on_memory_lands_in_the_system_prompt_of_an_unrelated_turn(self):
        self.service.always_on.return_value = [
            {"id": "m-1", "memory": "Reports use MMK", "pinned": True}]
        self.service.build_context.return_value = ""

        context = self.orchestrator._build_memory_context("who created you?", 3)
        self.assertIn("Reports use MMK", context)

        self.orchestrator._set_prompt_with_memory(context)
        system_prompt = next(
            message.content for message in self.orchestrator.agent.conversation_history
            if message.role == "system")
        self.assertIn("Reports use MMK", system_prompt)
        self.assertIn("User-approved memory", system_prompt)

        # Relevant recall is fetched separately so the pinned entry is not
        # duplicated in the prompt.
        self.assertFalse(
            self.service.build_context.call_args.kwargs["include_always"])


class PinnedMemoryServiceTests(unittest.TestCase):
    """The SQLite registry fallback is the always-on source of truth."""

    def _service(self):
        rows = []

        class FakeQuery:
            def filter_by(self, **kwargs):
                return self

            def order_by(self, *_args):
                return self

            def all(self):
                return rows

            def first(self):
                return rows[0] if rows else None

        class Registry:
            pinned = False  # class-level flag mirrors the SQLAlchemy column
            query = FakeQuery()

            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)
                rows.append(self)

        class Session:
            def __init__(self):
                self.added = []

            def add(self, value):
                self.added.append(value)

            def commit(self):
                pass

        class Db:
            session = Session()

        return MemoryService(enabled=False, db=Db(), registry_model=Registry), Db.session

    def test_remember_pinned_then_recall_without_keyword_match(self):
        service, session = self._service()
        saved = service.remember("Reports use MMK", user_id=1, branch_id=7, pinned=True)

        self.assertTrue(saved["saved"])
        self.assertTrue(saved["pinned"])
        self.assertTrue(session.added[0].pinned)

        always = service.always_on(user_id=1, branch_id=7)
        self.assertEqual([item["memory"] for item in always], ["Reports use MMK"])

        context = service.build_context("who created you", user_id=1, branch_id=7)
        self.assertIn("Reports use MMK", context)

    def test_unpinned_memories_are_not_injected_into_every_turn(self):
        service, _ = self._service()
        service.remember("A passing note", user_id=1, branch_id=7)
        self.assertEqual(service.always_on(user_id=1, branch_id=7), [])

    def test_always_on_list_is_bounded(self):
        service, _ = self._service()
        for index in range(3):
            service.remember(f"detail {index}", user_id=1, branch_id=7, pinned=True)
        self.assertEqual(len(service.always_on(user_id=1, branch_id=7, limit=2)), 2)


if __name__ == "__main__":
    unittest.main()

