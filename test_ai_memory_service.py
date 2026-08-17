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

    def test_forget_checks_namespace_before_deleting(self):
        self.assertTrue(self.service.forget("m-1", user_id=1, branch_id=7)["deleted"])
        self.assertEqual(self.client.deleted, ["m-1"])
        self.assertFalse(self.service.forget("m-2", user_id=2, branch_id=7)["deleted"])


if __name__ == "__main__":
    unittest.main()