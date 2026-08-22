"""Contract tests for the human-approval execution flow.

Contracts under test:
  * run_approved_plan executes ONLY the approved mutating steps of a
    persisted plan, tagged "executed_by": "approved"; every other mutating
    step stays a proposal and never touches the database.
  * Approved arguments come from the persisted plan, never from the caller.
  * Unknown step numbers, empty plans, and malformed approvals are refused
    without executing anything.
  * App endpoints: /approve records approval, /advance executes approved
    steps via run_approved_plan, stale proposals expire, /reject records
    rejection.

Orchestrator tests are pure-Python (no LLM, no DB): tool execution is
stubbed at _execute_tools_with_context. Endpoint tests use the Flask test
client with a mocked orchestrator (style of test_memory_api.py).
"""

import json
import unittest
from datetime import datetime, timedelta
from unittest import mock

_UNSET = object()

import agent_orchestrator
from agent_orchestrator import AgentOrchestrator
from ai_agent import ToolCall


PLAN = {
    "description": "Create two purchase orders",
    "steps": [
        {"step": 1, "tool": "create_purchase_order",
         "args": {"supplier_id": 7, "items": [{"product_id": 1, "quantity": 10}]},
         "reason": "restock cola"},
        {"step": 2, "tool": "create_purchase_order",
         "args": {"supplier_id": 8, "items": [{"product_id": 2, "quantity": 5}]},
         "reason": "restock fanta"},
    ],
}


class ApprovalTestBase(unittest.TestCase):
    def setUp(self):
        agent_orchestrator.reset_orchestrator()
        self.orchestrator = AgentOrchestrator(None, {})
        self.orchestrator.set_request_context(
            {"branch_id": 1, "user_id": 1, "role": "manager"})
        self.executed_calls = []

    def tearDown(self):
        agent_orchestrator.reset_orchestrator()

    def _stub_execution(self, ok_result=None):
        """Stub tool execution, recording every ToolCall; autonomy gate OFF."""
        result = ok_result if ok_result is not None else {"success": True, "po_number": "PO-1"}

        def fake_execute(tool_calls):
            recorded = []
            for tc in tool_calls:
                self.executed_calls.append(tc)
                recorded.append({"tool_call_id": tc.id, "function_name": tc.function_name,
                                 "result": dict(result, _step=tc.id), "error": None})
            return recorded

        return (mock.patch.object(self.orchestrator, "_autonomy_allowed", return_value=False),
                mock.patch.object(self.orchestrator, "_execute_tools_with_context",
                                  side_effect=fake_execute),
                mock.patch.dict(self.orchestrator.agent.tool_functions,
                                {"create_purchase_order": lambda **kw: result}))


class RunApprovedPlanTests(ApprovalTestBase):
    def test_only_approved_steps_execute_others_stay_proposals(self):
        gate, executor, tools = self._stub_execution()
        with gate, executor, tools:
            result = self.orchestrator.run_approved_plan("create POs", PLAN, [2])

        self.assertTrue(result["success"])
        executed = {tc.id for tc in self.executed_calls}
        self.assertEqual(executed, {"plan-step-2"})  # ONLY the approved step ran

        by_step = {sr["step"]: sr for sr in result["step_results"]}
        self.assertEqual(by_step[1]["status"], "proposal")   # unapproved -> proposal
        self.assertEqual(by_step[2]["status"], "ok")
        self.assertEqual(by_step[2]["executed_by"], "approved")

    def test_approved_args_come_from_persisted_plan_not_caller(self):
        gate, executor, tools = self._stub_execution()
        with gate, executor, tools:
            self.orchestrator.run_approved_plan("create POs", PLAN, [1])

        self.assertEqual(len(self.executed_calls), 1)
        call = self.executed_calls[0]
        self.assertIsInstance(call, ToolCall)
        self.assertEqual(call.function_name, "create_purchase_order")
        # Exact args from the stored plan — caller sent only a step number.
        self.assertEqual(call.arguments,
                         PLAN["steps"][0]["args"])

    def test_unknown_step_number_is_refused_without_executing(self):
        gate, executor, tools = self._stub_execution()
        with gate, executor, tools:
            result = self.orchestrator.run_approved_plan("create POs", PLAN, [9])

        self.assertFalse(result["success"])
        self.assertIn("do not exist", result["message"])
        self.assertEqual(self.executed_calls, [])

    def test_malformed_approval_list_is_refused(self):
        gate, executor, tools = self._stub_execution()
        with gate, executor, tools:
            result = self.orchestrator.run_approved_plan("create POs", PLAN, ["not-a-number"])

        self.assertFalse(result["success"])
        self.assertEqual(self.executed_calls, [])

    def test_empty_plan_is_refused(self):
        gate, executor, tools = self._stub_execution()
        with gate, executor, tools:
            for bad_plan in ({}, {"steps": []}, None):
                result = self.orchestrator.run_approved_plan("x", bad_plan, [1])
                self.assertFalse(result["success"], msg=bad_plan)
        self.assertEqual(self.executed_calls, [])

# === PART 2: endpoint tests ===

from app import AgentTask, app, db, Branch, User  # noqa: E402


class _FakeOrchestrator:
    """Stands in for get_ai_orchestrator(); records advance calls."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def run_approved_plan(self, command, plan, approved_step_nos):
        self.calls.append({"command": command, "plan": plan,
                           "approved": list(approved_step_nos)})
        return self.result


def _task_payload(status="pending_approval"):
    plan = json.dumps(PLAN)
    steps = json.dumps([
        {"step": 1, "tool": "create_purchase_order", "status": "proposal"},
        {"step": 2, "tool": "create_purchase_order", "status": "proposal"},
    ])
    return {"command": "create POs", "plan_json": plan,
            "step_results_json": steps, "status": status}


class AgentApprovalEndpointTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self._created_task_ids = []
        with app.app_context():
            self.user_id = User.query.filter_by(username='admin').first().id
            self.branch_id = Branch.query.filter_by(is_active=True).first().id
            task = AgentTask(user_id=self.user_id, **_task_payload())
            db.session.add(task)
            db.session.commit()
            self.task_id = task.id
            self._created_task_ids.append(task.id)

    def tearDown(self):
        # Delete ONLY the rows this test created — never wipe shared tables.
        if not self._created_task_ids:
            return
        with app.app_context():
            AgentTask.query.filter(AgentTask.id.in_(self._created_task_ids)).delete(
                synchronize_session=False)
            db.session.commit()

    def _make_task(self, user_id=_UNSET):
        with app.app_context():
            task = AgentTask(
                user_id=self.user_id if user_id is _UNSET else user_id,
                **_task_payload())
            db.session.add(task)
            db.session.commit()
            self._created_task_ids.append(task.id)
            return task.id

    def _client(self):
        client = app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.user_id
            session['role'] = 'manager'
            session['branch_id'] = self.branch_id
        return client

    def _fake_orchestrator_result(self):
        return {
            "success": True,
            "message": "Done: 1 approved change(s) applied.",
            "plan": PLAN,
            "step_results": [
                {"step": 1, "tool": "create_purchase_order", "status": "proposal"},
                {"step": 2, "tool": "create_purchase_order", "status": "ok",
                 "result": {"success": True}, "executed_by": "approved"},
            ],
            "pending_approvals": [],
        }

    def test_advance_without_approval_is_refused(self):
        response = self._client().post(f'/api/agent/task/{self.task_id}/advance')
        self.assertEqual(response.status_code, 409)

    def test_approve_then_advance_executes_only_approved_steps(self):
        client = self._client()
        fake = _FakeOrchestrator(self._fake_orchestrator_result())
        with mock.patch('app.get_ai_orchestrator', return_value=fake):
            approved = client.post(f'/api/agent/approve/{self.task_id}/1')
            self.assertEqual(approved.status_code, 200)

            advanced = client.post(f'/api/agent/task/{self.task_id}/advance')
        self.assertEqual(advanced.status_code, 200)
        data = advanced.get_json()
        self.assertTrue(data["success"])
        # Approving list-index 1 approves persisted plan STEP 2; the advance
        # must pass step NUMBERS (not indices) to the orchestrator.
        self.assertEqual(fake.calls[0]["approved"], [2])
        self.assertEqual(fake.calls[0]["plan"]["steps"][0]["args"],
                         PLAN["steps"][0]["args"])
        with app.app_context():
            task = db.session.get(AgentTask, self.task_id)
            self.assertEqual(task.status, 'pending_approval')  # step 2 still pending

    def test_stale_proposal_expires_and_is_refused(self):
        with app.app_context():
            task = db.session.get(AgentTask, self.task_id)
            task.created_at = datetime.utcnow() - timedelta(hours=25)
            db.session.commit()
        client = self._client()
        # Approving a stale proposal is refused outright...
        approve_resp = client.post(f'/api/agent/approve/{self.task_id}/0')
        self.assertEqual(approve_resp.status_code, 410)
        # ...and advancing it is impossible too.
        response = client.post(f'/api/agent/task/{self.task_id}/advance')
        self.assertEqual(response.status_code, 409)
        with app.app_context():
            self.assertEqual(db.session.get(AgentTask, self.task_id).status, 'expired')

    def test_reject_records_rejection_and_resolves_task(self):
        client = self._client()
        response = client.post(f'/api/agent/reject/{self.task_id}/0')
        self.assertEqual(response.status_code, 200)
        with app.app_context():
            task = db.session.get(AgentTask, self.task_id)
            steps = json.loads(task.step_results_json)
            self.assertTrue(steps[0].get('rejected'))
            # Step 2 is still undecided, so the task stays pending.
            self.assertEqual(task.status, 'pending_approval')
        # Rejecting the last undecided proposal resolves the whole task.
        response = client.post(f'/api/agent/reject/{self.task_id}/1')
        self.assertEqual(response.status_code, 200)
        with app.app_context():
            self.assertEqual(db.session.get(AgentTask, self.task_id).status, 'rejected')

    def test_reject_of_non_proposal_is_conflict(self):
        with app.app_context():
            task = db.session.get(AgentTask, self.task_id)
            steps = json.loads(task.step_results_json)
            steps[0]['status'] = 'ok'
            task.step_results_json = json.dumps(steps)
            db.session.commit()
        response = self._client().post(f'/api/agent/reject/{self.task_id}/0')
        self.assertEqual(response.status_code, 409)

    def test_other_users_task_is_forbidden(self):
        other_id = self._make_task(user_id=self.user_id + 99999)
        response = self._client().post(f'/api/agent/reject/{other_id}/0')
        self.assertEqual(response.status_code, 403)

    def test_null_user_task_is_forbidden_fail_closed(self):
        # A task with no owner must never be usable by any authenticated user.
        null_id = self._make_task(user_id=None)
        client = self._client()
        self.assertEqual(client.post(f'/api/agent/approve/{null_id}/0').status_code, 403)
        self.assertEqual(client.get(f'/api/agent/task/{null_id}').status_code, 403)


if __name__ == "__main__":
    unittest.main()
