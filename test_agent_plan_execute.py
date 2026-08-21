"""Contract tests for the PLAN-THEN-EXECUTE AgentOrchestrator refactor.

Contract under test (see docs/AGENT_EXTENSION.md):
  * process_command returns {"success", "message", "plan", "step_results",
    "pending_approvals"}; each step_result has {step, tool,
    status in {"ok", "failed", "proposal", "skipped"}, result|error}.
  * Plans are capped at 5 steps; invalid plans retry once, then fall back to
    single-shot execution.
  * "$from": "stepN.path" argument references are resolved between steps.
  * Execution fails fast: after a failed step every later step is "skipped".
  * Mutating tools produce "proposal" step results + pending_approvals and
    never touch the database directly from the agent loop.
  * The final summary chat call receives truncated (compacted) results and
    must report an incomplete task when any step failed.

No real API calls and no real database writes: ai_agent.requests.post is
mocked with scripted fake responses (style of test_ai_no_mock_data.py) and
tool execution is stubbed at the orchestrator/ai_tools boundary.

NOTE: these tests target code being written concurrently. Tests for the not-
yet-merged plan loop are skipped with "pending core-loop implementation" and
a TODO; assertions are NOT weakened — they activate once the code lands.
"""

import inspect
import json
import unittest
from unittest import mock

import ai_agent
import ai_tools
import agent_orchestrator
from agent_orchestrator import AgentOrchestrator

# Feature detection: which parts of the contract are merged already?
ORCH_SRC = inspect.getsource(AgentOrchestrator)
HAS_PLAN_LOOP = "pending_approvals" in ORCH_SRC          # TODO: remove gate when core loop merges
HAS_COMPACT = hasattr(AgentOrchestrator, "_compact_result")  # TODO: remove gate
HAS_METADATA = hasattr(ai_tools, "TOOL_METADATA")        # TODO: remove gate
RESOLVER_NAME = next(
    (n for n in dir(AgentOrchestrator)
     if "resolve" in n.lower() and ("from" in n.lower() or "ref" in n.lower())),
    None,
)
HAS_RESOLVER = RESOLVER_NAME is not None                 # TODO: remove gate

PENDING = "pending core-loop implementation"


class _FakeBody:
    """Minimal requests.Response stand-in for scripted API bodies."""

    status_code = 200

    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def api_response(content="", tool_calls=None, finish_reason=None):
    """Build a fake OpenAI-style /chat/completions JSON body."""
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [{
            "id": tc["id"],
            "type": "function",
            "function": {
                "name": tc["name"],
                "arguments": (
                    tc["arguments"]
                    if isinstance(tc["arguments"], str)
                    else json.dumps(tc["arguments"])
                ),
            },
        } for tc in tool_calls]
        finish_reason = finish_reason or "tool_calls"
    return {
        "choices": [{"message": message, "finish_reason": finish_reason or "stop"}],
        "usage": {},
    }


def plan_response(description, steps, call_id="c1"):
    """A scripted propose_plan tool-call response."""
    return api_response(tool_calls=[{
        "id": call_id,
        "name": "propose_plan",
        "arguments": {"description": description, "steps": steps},
    }])


class PlanExecuteTestBase(unittest.TestCase):
    def setUp(self):
        agent_orchestrator.reset_orchestrator()
        self.orchestrator = AgentOrchestrator(None, {})
        self.orchestrator.set_request_context(
            {"branch_id": 1, "user_id": 1, "role": "manager"}
        )
        self.chat_payloads = []

    def tearDown(self):
        agent_orchestrator.reset_orchestrator()

    def script(self, *responses):
        """Patch ai_agent.requests.post with a scripted sequence of bodies."""
        queue = list(responses)

        def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
            self.chat_payloads.append(json)
            return _FakeBody(queue.pop(0))

        return mock.patch.object(ai_agent.requests, "post", side_effect=fake_post)

    def fake_execute_ok(self, tool_calls):
        tc = tool_calls[0]
        return [{
            "tool_call_id": tc.id,
            "function_name": tc.function_name,
            "result": {"ok": True},
            "error": None,
        }]


class PlanValidationTests(PlanExecuteTestBase):
    """Plan validation: size cap, unknown tools, malformed JSON fallback."""

    @unittest.skipUnless(HAS_PLAN_LOOP, PENDING)
    # TODO(agent-team): delete skipUnless once process_command returns plans.
    def test_plan_with_more_than_five_steps_is_rejected(self):
        steps = [{"step": f"s{i}", "tool": "get_inventory_status", "args": {}}
                 for i in range(6)]
        fallback = api_response(content="Done in one shot.")
        with self.script(plan_response("big plan", steps), fallback), \
             mock.patch.object(self.orchestrator, "_execute_tools_with_context",
                               side_effect=self.fake_execute_ok):
            result = self.orchestrator.process_command("do six things", user_id=1)

        self.assertTrue(result["success"])
        self.assertIn("plan", result)
        self.assertLessEqual(len(result.get("plan") or []), 5)

    @unittest.skipUnless(HAS_PLAN_LOOP, PENDING)
    # TODO(agent-team): delete skipUnless once process_command returns plans.
    def test_unknown_tool_name_in_plan_is_rejected(self):
        steps = [{"step": "s1", "tool": "delete_everything", "args": {}}]
        fallback = api_response(content="Falling back.")
        with self.script(plan_response("bad tool", steps), fallback), \
             mock.patch.object(self.orchestrator, "_execute_tools_with_context",
                               side_effect=self.fake_execute_ok):
            result = self.orchestrator.process_command("use a fake tool", user_id=1)

        self.assertTrue(result["success"])  # graceful fallback, not a crash
        statuses = [sr.get("status") for sr in result.get("step_results", [])]
        self.assertNotIn(None, statuses)  # no unvalidated step was executed

    @unittest.skipUnless(HAS_PLAN_LOOP, PENDING)
    # TODO(agent-team): delete skipUnless once process_command returns plans.
    def test_malformed_plan_json_retries_once_then_falls_back(self):
        malformed = api_response(tool_calls=[{
            "id": "c1", "name": "propose_plan", "arguments": "{not valid json",
        }])
        retry = plan_response("still broken", "not-a-list", call_id="c2")
        fallback = api_response(content="Single-shot answer instead.")

        with self.script(malformed, retry, fallback), \
             mock.patch.object(self.orchestrator, "_execute_tools_with_context",
                               side_effect=self.fake_execute_ok):
            result = self.orchestrator.process_command(
                "garbage plan please", user_id=1)

        self.assertTrue(result["success"])
        self.assertIn("Single-shot answer instead.", result["message"])
        # Exactly two planning attempts before the fallback chat call.
        self.assertEqual(len(self.chat_payloads), 3)


class FromReferenceResolutionTests(PlanExecuteTestBase):
    """$from references must be resolved between plan steps."""

    @unittest.skipUnless(HAS_RESOLVER, PENDING)
    # TODO(agent-team): point RESOLVER_NAME at the real helper name once merged.
    def test_resolver_direct_unit_test(self):
        resolver = getattr(self.orchestrator, RESOLVER_NAME)
        step_outputs = {
            1: {"supplier": {"supplier_id": 7}, "items": [{"product_id": 3}]},
        }
        args = {
            "supplier_id": {"$from": "step1.supplier.supplier_id"},
            "note": "static value stays",
        }
        try:
            resolved = resolver(args, step_outputs)
        except TypeError:
            resolved = resolver(args)
        self.assertEqual(resolved["supplier_id"], 7)
        self.assertEqual(resolved["note"], "static value stays")

    @unittest.skipUnless(HAS_PLAN_LOOP, PENDING)
    # TODO(agent-team): delete skipUnless once $from resolution is wired in.
    def test_step2_arg_referencing_step1_output_via_scripted_plan(self):
        steps = [
            {"step": "find supplier", "tool": "get_supplier_list", "args": {}},
            {"step": "supplier details", "tool": "get_supplier_details",
             "args": {"supplier_id": {"$from": "step1.suppliers.0.supplier_id"}}},
        ]
        summary = api_response(content="Supplier details retrieved.")
        executed = []

        def fake_execute(tool_calls):
            tc = tool_calls[0]
            executed.append((tc.function_name, dict(tc.arguments)))
            if tc.function_name == "get_supplier_list":
                tc_result = {"suppliers": [{"supplier_id": 42, "name": "Acme"}]}
            else:
                tc_result = {"supplier_id": 42}
            return [{
                "tool_call_id": tc.id,
                "function_name": tc.function_name,
                "result": tc_result,
                "error": None,
            }]

        with self.script(plan_response("lookup then detail", steps), summary), \
             mock.patch.object(self.orchestrator, "_execute_tools_with_context",
                               side_effect=fake_execute):
            result = self.orchestrator.process_command(
                "show details for the first supplier", user_id=1)

        self.assertTrue(result["success"])
        detail_calls = [c for c in executed if c[0] == "get_supplier_details"]
        self.assertTrue(detail_calls)
        # The literal $from marker must never reach the tool layer.
        self.assertEqual(detail_calls[0][1].get("supplier_id"), 42)
        self.assertNotIn("$from", json.dumps(executed))


class FailStopTests(PlanExecuteTestBase):
    @unittest.skipUnless(HAS_PLAN_LOOP, PENDING)
    # TODO(agent-team): delete skipUnless once fail-stop semantics merge.
    def test_step_failure_stops_later_steps_and_marks_skipped(self):
        steps = [
            {"step": "one", "tool": "get_inventory_status", "args": {}},
            {"step": "two", "tool": "get_supplier_list", "args": {}},
            {"step": "three", "tool": "search_products", "args": {"query": "cola"}},
        ]
        incomplete_summary = api_response(
            content="The task could not be completed: step two failed.")
        calls = {"n": 0}

        def fake_execute(tool_calls):
            calls["n"] += 1
            tc = tool_calls[0]
            if calls["n"] == 2:
                raise RuntimeError("boom on step two")
            return [{
                "tool_call_id": tc.id,
                "function_name": tc.function_name,
                "result": {"ok": True},
                "error": None,
            }]

        with self.script(plan_response("three steps", steps),
                         incomplete_summary), \
             mock.patch.object(self.orchestrator, "_execute_tools_with_context",
                               side_effect=fake_execute):
            result = self.orchestrator.process_command("run three steps", user_id=1)

        step_results = result["step_results"]
        self.assertEqual(len(step_results), 3)
        self.assertEqual(step_results[0]["status"], "ok")
        self.assertEqual(step_results[1]["status"], "failed")
        self.assertIn("boom on step two", step_results[1].get("error", ""))
        self.assertEqual(step_results[2]["status"], "skipped")
        self.assertEqual(calls["n"], 2)  # step three never executed


class WriteAsProposalTests(PlanExecuteTestBase):
    @unittest.skipUnless(HAS_PLAN_LOOP, PENDING)
    # TODO(agent-team): delete skipUnless once write-proposals merge.
    def test_create_purchase_order_yields_proposal_and_no_db_write(self):
        steps = [{
            "step": "create PO",
            "tool": "create_purchase_order",
            "args": {"supplier_id": 1,
                     "items": [{"product_id": 5, "quantity": 10}]},
        }]
        proposal_summary = api_response(
            content="A purchase order draft is ready for your approval.")
        po_mock = mock.Mock(name="create_purchase_order",
                            return_value={"po_id": 999})

        with self.script(plan_response("restock cola", steps),
                         proposal_summary), \
             mock.patch.object(self.orchestrator.ai_tools, "create_purchase_order",
                               po_mock):
            result = self.orchestrator.process_command(
                "order 10 cola from supplier 1", user_id=1)

        self.assertTrue(result["success"])
        step_results = result["step_results"]
        self.assertEqual(len(step_results), 1)
        self.assertEqual(step_results[0]["status"], "proposal")
        self.assertTrue(result["pending_approvals"])
        # The mutating tool must never have been invoked by the agent loop.
        po_mock.assert_not_called()


class AntiHallucinationSummaryTests(PlanExecuteTestBase):
    @unittest.skipUnless(HAS_PLAN_LOOP, PENDING)
    # TODO(agent-team): delete skipUnless once summary generation merges.
    def test_summary_receives_truncated_results_and_reports_incomplete_task(self):
        steps = [
            {"step": "inventory", "tool": "get_inventory_status", "args": {}},
            {"step": "suppliers", "tool": "get_supplier_list", "args": {}},
        ]
        huge_rows = [{"product_id": i, "name": f"p{i}", "x": "y" * 200}
                     for i in range(500)]
        summary_capture = {}
        plan_body = plan_response("two steps", steps)
        final_body = api_response(
            content="Task incomplete: the suppliers step failed.")

        def post_side_effect(url, headers=None, json=None, timeout=None, **kw):
            summary_capture["payload"] = json
            return _FakeBody(plan_body if len(summary_capture) == 1 else final_body)

        calls = {"n": 0}

        def fake_execute(tool_calls):
            calls["n"] += 1
            tc = tool_calls[0]
            if calls["n"] == 2:
                raise RuntimeError("supplier lookup exploded")
            return [{
                "tool_call_id": tc.id,
                "function_name": tc.function_name,
                "result": {"total_products": 500, "inventory": huge_rows},
                "error": None,
            }]

        with mock.patch.object(ai_agent.requests, "post",
                               side_effect=post_side_effect), \
             mock.patch.object(self.orchestrator, "_execute_tools_with_context",
                               side_effect=fake_execute):
            result = self.orchestrator.process_command("big report", user_id=1)

        # The final (summary) chat call must receive compacted results...
        serialized = json.dumps(summary_capture.get("payload") or {})
        self.assertLess(len(serialized), 60000,
                        "summary prompt must contain truncated results")
        self.assertNotIn("y" * 200, serialized)
        # ...and when a step failed, the returned message indicates incompleteness.
        self.assertFalse(result["success"])
        self.assertTrue(
            any(w in result["message"].lower()
                for w in ("incomplete", "failed", "could not", "not completed")),
            f"message must indicate incomplete task: {result['message']!r}")


class TokenCompactionTests(unittest.TestCase):
    @unittest.skipUnless(HAS_COMPACT, PENDING)
    # TODO(agent-team): delete skipUnless once _compact_result merges.
    def test_large_lists_and_strings_truncated_but_counts_preserved(self):
        orch = AgentOrchestrator.__new__(AgentOrchestrator)  # pure helper, no init
        big = {
            "total_products": 500,
            "inventory": [{"i": i} for i in range(500)],
            "blob": "z" * 10000,
            "totals": {"value": 1234.5},
        }
        compact = AgentOrchestrator._compact_result(orch, big)
        self.assertEqual(compact["total_products"], 500)
        self.assertEqual(compact["totals"], {"value": 1234.5})
        self.assertLess(len(compact["inventory"]), 500)
        self.assertLess(len(compact["blob"]), 10000)
        self.assertLessEqual(len(json.dumps(compact)), len(json.dumps(big)))


class RegistryCompletenessTests(unittest.TestCase):
    """Registry contract. Base checks run against TOOL_SCHEMAS today; the
    richer TOOL_METADATA checks activate once that registry lands."""

    def test_tool_schemas_importable_and_well_formed(self):
        from ai_tools import TOOL_SCHEMAS, get_all_tools

        self.assertTrue(TOOL_SCHEMAS)
        for name, schema in TOOL_SCHEMAS.items():
            with self.subTest(tool=name):
                self.assertEqual(schema.get("name"), name)
                self.assertTrue(schema.get("description"))
                params = schema.get("parameters")
                self.assertIsInstance(params, dict)
                self.assertEqual(params.get("type"), "object")
                self.assertIsInstance(params.get("properties", {}), dict)
        # get_all_tools() stays consistent with TOOL_SCHEMAS keys.
        self.assertEqual(set(get_all_tools()), set(TOOL_SCHEMAS))

    @unittest.skipUnless(HAS_METADATA, PENDING)
    # TODO(ai-tools team): delete skipUnless once TOOL_METADATA lands.
    def test_every_metadata_entry_has_required_keys(self):
        required = {"name", "description", "parameters", "category",
                    "mutates", "requires_role", "description_one_line"}
        for name, meta in ai_tools.TOOL_METADATA.items():
            with self.subTest(tool=name):
                missing = required - set(meta)
                self.assertFalse(missing, f"missing keys: {missing}")

    @unittest.skipUnless(HAS_METADATA, PENDING)
    # TODO(ai-tools team): delete skipUnless once TOOL_METADATA lands.
    def test_mutating_tools_require_a_role(self):
        for name, meta in ai_tools.TOOL_METADATA.items():
            with self.subTest(tool=name):
                if meta.get("mutates"):
                    self.assertTrue(meta.get("requires_role"),
                                    f"{name} mutates but has no requires_role")

    @unittest.skipUnless(HAS_METADATA, PENDING)
    # TODO(ai-tools team): delete skipUnless once TOOL_METADATA lands.
    def test_registry_and_schemas_stay_consistent(self):
        for name in ai_tools.TOOL_METADATA:
            self.assertIn(name, ai_tools.TOOL_SCHEMAS,
                          f"{name} in TOOL_METADATA but missing from TOOL_SCHEMAS")
        # New write tools must exist in both registries.
        for write_tool in ("upsert_product", "adjust_product_stock",
                           "create_promotion", "register_customer",
                           "record_debt_payment", "update_delivery_stage"):
            self.assertIn(write_tool, ai_tools.TOOL_METADATA)
            self.assertIn(write_tool, ai_tools.TOOL_SCHEMAS)
            self.assertTrue(ai_tools.TOOL_METADATA[write_tool]["mutates"])


if __name__ == "__main__":
    unittest.main()
