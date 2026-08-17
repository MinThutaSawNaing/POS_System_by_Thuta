import unittest

from ai_memory_service import MemoryService


class FakeMem0:
    def __init__(self):
        self.deleted = []

    def add(self, messages, user_id, metadata):
        return {"results": [{"id": "m-1", "memory": messages[0]["content"], "metadata": metadata}]}

    def search(self, query, user_id, limit):
        return {"results": [
            {"id": "m-1", "memory": "private preference", "metadata": {"user_id": user_id, "branch_id": "7", "scope": "private"}},
            {"id": "m-2", "memory": "other branch", "metadata": {"user_id": user_id, "branch_id": "8", "scope": "private"}},
        ]}

    def delete(self, memory_id):
        self.deleted.append(memory_id)

    def get(self, memory_id):
        return {"id": memory_id, "metadata": {"user_id": "1", "branch_id": "7", "scope": "private"}}


class MemoryServiceTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeMem0()
        self.service = MemoryService(client=self.client)

    def test_rejects_sensitive_and_unapproved_auto_save(self):
        with self.assertRaisesRegex(ValueError, "sensitive"):
            self.service.remember("my password is hunter2", user_id=1, branch_id=7)
        outcome = self.service.remember("A sales note", user_id=1, branch_id=7, explicit=False)
        self.assertFalse(outcome["saved"])

    def test_scopes_retrieval_and_bounds_context(self):
        result = self.service.remember("I prefer concise reports", user_id=1, branch_id=7)
        self.assertTrue(result["saved"])
        memories = self.service.retrieve("report preference", user_id=1, branch_id=7, limit=20)
        self.assertEqual([memory["id"] for memory in memories], ["m-1"])
        self.assertEqual(self.service.build_context("report", user_id=1, branch_id=7, max_characters=20), "- private preference")

    def test_unavailable_backend_is_a_safe_noop(self):
        service = MemoryService(enabled=False)
        self.assertEqual(service.retrieve("preference", user_id=1, branch_id=7), [])
        self.assertFalse(service.remember("I prefer reports", user_id=1, branch_id=7)["saved"])

    def test_sqlite_registry_fallback_saves_and_retrieves_without_mem0(self):
        class FakeQuery:
            def __init__(self, rows): self.rows = rows
            def filter_by(self, **kwargs): return self
            def order_by(self, *_args): return self
            def all(self): return self.rows
            def first(self): return self.rows[0] if self.rows else None

        class Row:
            def __init__(self):
                self.memory_id = "sqlite-memory"
                self.user_id = 1
                self.branch_id = 7
                self.scope = "private"
                self.summary = "I prefer concise reports"
                self.updated_at = 1

        class Registry:
            query = FakeQuery([Row()])
            def __init__(self, **kwargs): self.__dict__.update(kwargs)

        class Session:
            def __init__(self): self.added = []
            def add(self, value): self.added.append(value)

        class Db:
            session = Session()

        service = MemoryService(enabled=False, db=Db(), registry_model=Registry)
        saved = service.remember("I prefer concise reports", user_id=1, branch_id=7)
        self.assertTrue(saved["saved"])
        self.assertEqual(saved["backend"], "sqlite")
        recalled = service.retrieve("concise report", user_id=1, branch_id=7)
        self.assertEqual(recalled[0]["memory"], "I prefer concise reports")

    def test_forget_checks_namespace_before_deleting(self):
        self.assertTrue(self.service.forget("m-1", user_id=1, branch_id=7)["deleted"])
        self.assertEqual(self.client.deleted, ["m-1"])
        self.assertFalse(self.service.forget("m-2", user_id=2, branch_id=7)["deleted"])


if __name__ == "__main__":
    unittest.main()