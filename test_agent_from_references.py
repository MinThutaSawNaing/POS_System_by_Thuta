"""Reliability contract for plan argument references and the approval loop.

Contracts under test:
  * A "$from" path resolves against the real result shape, including the
    spellings models actually write ("step1.products.0.id" for a tool whose
    list is "inventory" and whose row id is "product_id").
  * An unresolvable reference raises FromReferenceError naming the keys that DO
    exist, instead of a bare KeyError with no repair signal.
  * Plan validation flags a reference to a key the earlier tool never returns
    and accepts the correct one.
  * Execution repairs a broken reference (bounded), reuses the arguments a human
    already approved, and never executes an unapproved write.
  * A plain "yes"/"no" in chat answers the proposal on the table instead of
    planning the same change again.

No network calls: the orchestrator is built without an API key (so repair is
inert unless a test stubs it) and tool execution is stubbed. App-level tests
touch only the AgentTask rows they create (style of test_agent_approval.py).
"""

import json
import unittest
from datetime import datetime, timedelta
from unittest import mock

import agent_orchestrator
from agent_orchestrator import AgentOrchestrator, FromReferenceError


SEARCH_RESULT = {
    "total_products": 1,
    "inventory": [{"product_id": 85, "name": "Zeno", "barcode": "7218294",
                   "category": "Drinks", "current_stock": 0,
                   "price": "2500.00", "cost": "2000.00",
                   "status": "out_of_stock"}],
    "branch_id": 1,
}

EMPTY_SEARCH_RESULT = {"total_products": 0, "inventory": [], "branch_id": 1}

PRICE_PLAN = {
    "description": "change the price of Zeno to 2600",
    "steps": [
        {"step": 1, "tool": "search_products", "args": {"query": "Zeno"}},
        {"step": 2, "tool": "update_product_price",
         "args": {"product_id": {"$from": "step1.products.0.id"}, "price": 2600},
         "reason": "owner approved a new selling price"},
    ],
}


class ReferenceResolutionTests(unittest.TestCase):
    """$from paths must match how models write them AND how tools reply."""

    def setUp(self):
        agent_orchestrator.reset_orchestrator()
        self.orchestrator = AgentOrchestrator(None, {})
        self.outputs = {1: dict(SEARCH_RESULT)}

    def tearDown(self):
        agent_orchestrator.reset_orchestrator()

    def resolve(self, path, outputs=None):
        return self.orchestrator._resolve_from_refs(
            {"value": {"$from": path}}, outputs or self.outputs)["value"]

    def test_exact_path_still_resolves(self):
        self.assertEqual(self.resolve("step1.inventory.0.product_id"), 85)

    def test_guessed_collection_and_id_names_resolve(self):
        # The reported failure: "1.00 step.products.0.id" against a tool that
        # returns inventory[].product_id.
        self.assertEqual(self.resolve("step1.products.0.id"), 85)

    def test_transparent_result_segment_is_ignored(self):
        self.assertEqual(self.resolve("step1.result.inventory.0.product_id"), 85)

    def test_step_token_variants_resolve(self):
        for path in ("step_1.inventory.0.product_id",
                     "1.inventory.0.product_id",
                     "steps.1.inventory.0.product_id",
                     "Step 1.inventory.0.product_id"):
            self.assertEqual(self.resolve(path), 85, msg=path)

    def test_key_spelling_variants_resolve(self):
        self.assertEqual(self.resolve("step1.inventory.0.productId"), 85)

    def test_plain_id_resolves_on_rows_that_use_it(self):
        outputs = {1: {"total_customers": 1,
                       "customers": [{"id": 9, "name": "Aung"}]}}
        self.assertEqual(self.resolve("step1.customers.0.id", outputs), 9)

    def test_ambiguous_plain_id_is_refused(self):
        outputs = {1: {"items": [{"purchase_order_item_id": 3, "product_id": 5}]}}
        with self.assertRaises(FromReferenceError):
            self.resolve("step1.items.0.id", outputs)

    def test_error_names_the_keys_that_exist(self):
        # Two collections make the guess ambiguous, so the error must teach the
        # real shape instead of silently picking one.
        outputs = {1: {"total_products": 2,
                       "inventory": [SEARCH_RESULT["inventory"][0]],
                       "promotions": [{"id": 1, "status": "active"}]}}
        with self.assertRaises(FromReferenceError) as caught:
            self.resolve("step1.vendors.0.id", outputs)
        message = str(caught.exception)
        self.assertIn("inventory", message)
        self.assertIn("product_id", message)

    def test_missing_list_index_is_reported(self):
        with self.assertRaises(FromReferenceError) as caught:
            self.resolve("step1.inventory.product_id")
        self.assertIn("list index", str(caught.exception))

    def test_out_of_range_index_is_reported(self):
        with self.assertRaises(FromReferenceError) as caught:
            self.resolve("step1.inventory.4.product_id")
        self.assertIn("item 4", str(caught.exception))

    def test_reference_to_a_step_without_output_is_reported(self):
        with self.assertRaises(FromReferenceError) as caught:
            self.resolve("step3.inventory.0.product_id")
        self.assertIn("step 3", str(caught.exception))


class PlanReferenceValidationTests(unittest.TestCase):
    """Plan-time validation catches a guessed key before anything runs."""

    def setUp(self):
        agent_orchestrator.reset_orchestrator()
        self.orchestrator = AgentOrchestrator(None, {})

    def tearDown(self):
        agent_orchestrator.reset_orchestrator()

    @staticmethod
    def _price_steps(path):
        return [
            {"step": 1, "tool": "search_products", "args": {"query": "Zeno"}},
            {"step": 2, "tool": "update_product_price",
             "args": {"product_id": {"$from": path}, "price": 2600}},
        ]

    def test_guessed_key_is_flagged_with_the_real_shape(self):
        warning = self.orchestrator._validate_from_references(
            self._price_steps("step1.products.0.id"))
        self.assertIn("inventory", warning)
        self.assertIn("product_id", warning)

    def test_correct_key_passes(self):
        self.assertIsNone(self.orchestrator._validate_from_references(
            self._price_steps("step1.inventory.0.product_id")))

    def test_forward_reference_is_flagged(self):
        steps = [
            {"step": 1, "tool": "search_products", "args": {"query": "Zeno"}},
            {"step": 2, "tool": "search_products",
             "args": {"query": {"$from": "step2.inventory.0.name"}}},
        ]
        self.assertIn("earlier steps",
                      self.orchestrator._validate_from_references(steps))

    def test_catalog_publishes_result_shapes_for_chained_tools(self):
        catalog = self.orchestrator._build_planning_catalog(
            self.orchestrator._get_tool_registry())
        line = next(row for row in catalog.splitlines()
                    if row.startswith("- search_products"))
        self.assertIn("returns:", line)
        self.assertIn("inventory[]", line)


class WriteIntentRoutingTests(unittest.TestCase):
    """A change request must reach the plan path, which is the only one with writes."""

    def setUp(self):
        agent_orchestrator.reset_orchestrator()
        self.orchestrator = AgentOrchestrator(None, {})

    def tearDown(self):
        agent_orchestrator.reset_orchestrator()

    def test_change_phrasing_is_routed_to_the_planner(self):
        for text in (
            "Hello can you change the sell price of product name Zeno into 2600MMK?",
            "change the selling price of Zeno to 2600",
            "raise the price of Zeno by 100",
            "update the barcode for Zeno",
            "set the cost of Zeno to 2000",
        ):
            self.assertTrue(self.orchestrator._should_plan(text), msg=text)

    def test_questions_are_not_treated_as_change_requests(self):
        for text in ("what is the price of Zeno",
                     "how many units of Zeno are left",
                     "was the price changed last week"):
            self.assertFalse(self.orchestrator._contains_write_intent(text), msg=text)


class EndToEndPriceChangeTests(unittest.TestCase):
    """The reported failure, start to finish, through process_command."""

    PLAN_MESSAGE = {
        "role": "assistant", "content": "",
        "tool_calls": [{
            "id": "plan-1", "type": "function",
            "function": {"name": "propose_plan",
                         "arguments": json.dumps(PRICE_PLAN)},
        }],
    }
    CORRECTED_PLAN_MESSAGE = {
        "role": "assistant", "content": "",
        "tool_calls": [{
            "id": "plan-2", "type": "function",
            "function": {"name": "propose_plan", "arguments": json.dumps({
                "description": PRICE_PLAN["description"],
                "steps": [
                    {"step": 1, "tool": "search_products",
                     "args": {"query": "Zeno"}},
                    {"step": 2, "tool": "update_product_price",
                     "args": {"product_id": {
                         "$from": "step1.inventory.0.product_id"},
                         "price": 2600},
                     "reason": "owner approved a new selling price"},
                ],
            })},
        }],
    }
    SUMMARY_MESSAGE = {
        "role": "assistant",
        "content": "Zeno is ready to be re-priced: 1 change awaits your approval.",
    }

    def setUp(self):
        agent_orchestrator.reset_orchestrator()
        self.orchestrator = AgentOrchestrator(None, {})
        self.orchestrator.set_request_context(
            {"branch_id": 1, "user_id": 1, "role": "manager"})
        self.orchestrator.agent.api_key = ""   # no network, no repair calls
        self.calls = []
        self.prompts = []

    def tearDown(self):
        agent_orchestrator.reset_orchestrator()

    def _script(self, messages):
        remaining = list(messages)

        def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
            payload = json if isinstance(json, dict) else {}
            self.prompts.extend(message.get("content", "")
                                for message in payload.get("messages", []))
            body = {"choices": [{"message": remaining.pop(0),
                                 "finish_reason": "stop"}]}
            response = mock.Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = body
            return response

        return mock.patch("requests.post", side_effect=fake_post)

    def _stub_tools(self):
        def fake_execute(tool_calls):
            recorded = []
            for tc in tool_calls:
                self.calls.append((tc.function_name, dict(tc.arguments)))
                payload = (SEARCH_RESULT if tc.function_name == "search_products"
                           else {"success": True})
                recorded.append({"tool_call_id": tc.id,
                                 "function_name": tc.function_name,
                                 "result": payload, "error": None})
            return recorded
        return mock.patch.object(self.orchestrator, "_execute_tools_with_context",
                                 side_effect=fake_execute)

    def test_price_change_request_reaches_an_approvable_proposal(self):
        request = ("Hello can you change the sell price of product name "
                   "Zeno into 2600MMK?")
        with self._script([self.PLAN_MESSAGE, self.CORRECTED_PLAN_MESSAGE,
                           self.SUMMARY_MESSAGE]), \
                self._stub_tools(), \
                mock.patch.object(self.orchestrator, "_autonomy_allowed",
                                  return_value=False):
            result = self.orchestrator.process_command(request, user_id=1)

        self.assertEqual([sr["status"] for sr in result["step_results"]],
                         ["ok", "proposal"])
        self.assertTrue(result["pending_approvals"])
        proposal = result["pending_approvals"][0]
        self.assertEqual(proposal["tool"], "update_product_price")
        self.assertEqual(proposal["args"]["product_id"], 85)
        self.assertEqual(proposal["args"]["price"], 2600)
        # The write itself still needs the human approval.
        self.assertNotIn("update_product_price", [name for name, _ in self.calls])
        self.assertIn("approval", result["message"].lower())

    def test_guessed_key_costs_one_retry_that_teaches_the_real_shape(self):
        request = ("change the sell price of product name Zeno into 2600MMK")
        with self._script([self.PLAN_MESSAGE, self.CORRECTED_PLAN_MESSAGE,
                           self.SUMMARY_MESSAGE]), \
                self._stub_tools(), \
                mock.patch.object(self.orchestrator, "_autonomy_allowed",
                                  return_value=False):
            self.orchestrator.process_command(request, user_id=1)

        retry_prompt = next((prompt for prompt in self.prompts
                             if "previous plan was invalid" in prompt), "")
        self.assertIn("inventory", retry_prompt)
        self.assertIn("product_id", retry_prompt)


class ExecutionRecoveryTests(unittest.TestCase):
    """A broken reference must not destroy a task the user already approved."""

    def setUp(self):
        agent_orchestrator.reset_orchestrator()
        self.orchestrator = AgentOrchestrator(None, {})
        self.orchestrator.set_request_context(
            {"branch_id": 1, "user_id": 1, "role": "manager"})
        # Never reach the network from a unit test.
        self.orchestrator.agent.api_key = ""
        self.calls = []

    def tearDown(self):
        agent_orchestrator.reset_orchestrator()

    def _stub_tools(self, search_result=SEARCH_RESULT):
        def fake_execute(tool_calls):
            recorded = []
            for tc in tool_calls:
                self.calls.append((tc.function_name, dict(tc.arguments)))
                payload = (search_result if tc.function_name == "search_products"
                           else {"success": True})
                recorded.append({"tool_call_id": tc.id,
                                 "function_name": tc.function_name,
                                 "result": payload, "error": None})
            return recorded
        return mock.patch.object(self.orchestrator, "_execute_tools_with_context",
                                 side_effect=fake_execute)

    @staticmethod
    def _plan():
        return {"description": PRICE_PLAN["description"],
                "steps": [dict(step) for step in PRICE_PLAN["steps"]]}

    def test_tolerant_resolution_turns_the_guess_into_a_proposal(self):
        with mock.patch.object(self.orchestrator, "_autonomy_allowed", return_value=False), \
                self._stub_tools():
            step_results, pending = self.orchestrator._execute_plan(self._plan())

        self.assertEqual([sr["status"] for sr in step_results], ["ok", "proposal"])
        self.assertEqual(pending[0]["args"]["product_id"], 85)
        self.assertEqual(pending[0]["args"]["price"], 2600)
        # The write really did not run before approval.
        self.assertNotIn("update_product_price", [name for name, _ in self.calls])

    def test_unresolvable_reference_fails_before_any_write(self):
        with mock.patch.object(self.orchestrator, "_autonomy_allowed", return_value=False), \
                self._stub_tools(EMPTY_SEARCH_RESULT):
            step_results, pending = self.orchestrator._execute_plan(self._plan())

        self.assertEqual([sr["status"] for sr in step_results], ["ok", "failed"])
        self.assertEqual(pending, [])
        self.assertIn("item 0", step_results[1]["error"])
        self.assertNotIn("update_product_price", [name for name, _ in self.calls])

    def test_repaired_arguments_execute_the_planned_step(self):
        def fake_repair(plan, step, step_outputs, error):
            return {"product_id": 85, "price": 2600}

        with mock.patch.object(self.orchestrator, "_autonomy_allowed", return_value=False), \
                self._stub_tools(EMPTY_SEARCH_RESULT), \
                mock.patch.object(self.orchestrator, "_repair_step_args",
                                  side_effect=fake_repair) as repair:
            step_results, pending = self.orchestrator._execute_plan(self._plan())

        repair.assert_called_once()
        self.assertEqual(step_results[1]["status"], "proposal")
        self.assertEqual(step_results[1]["args_source"], "repaired")
        self.assertEqual(pending[0]["args"]["product_id"], 85)

    def test_repair_is_bounded_per_plan(self):
        def bad_repair(plan, step, step_outputs, error):
            return {"product_id": {"$from": "step1.missing.0.id"}, "price": 2600}

        with mock.patch.object(self.orchestrator, "_autonomy_allowed", return_value=False), \
                self._stub_tools(EMPTY_SEARCH_RESULT), \
                mock.patch.object(self.orchestrator, "_repair_step_args",
                                  side_effect=bad_repair) as repair:
            step_results, _ = self.orchestrator._execute_plan(self._plan())

        self.assertEqual(step_results[1]["status"], "failed")
        self.assertLessEqual(repair.call_count, agent_orchestrator.MAX_PLAN_STEP_REPAIRS)


class ApprovedPlanFallbackTests(unittest.TestCase):
    """A re-run that cannot rebuild a reference reuses the approved arguments."""

    PLAN = {
        "description": "restock cola",
        "steps": [
            {"step": 1, "tool": "get_supplier_list", "args": {}},
            {"step": 2, "tool": "create_purchase_order",
             "args": {"supplier_id": {"$from": "step1.suppliers.0.owner_id"},
                      "items": [{"product_id": 5, "quantity": 2}]}},
        ],
    }
    PERSISTED = [
        {"step": 1, "tool": "get_supplier_list", "status": "ok",
         "result": {"total_suppliers": 1, "suppliers": [{"supplier_id": 7}]}},
        {"step": 2, "tool": "create_purchase_order", "status": "proposal",
         "result": {"step": 2, "tool": "create_purchase_order",
                    "args": {"supplier_id": 7,
                             "items": [{"product_id": 5, "quantity": 2}]}}},
    ]

    def setUp(self):
        agent_orchestrator.reset_orchestrator()
        self.orchestrator = AgentOrchestrator(None, {})
        self.orchestrator.set_request_context(
            {"branch_id": 1, "user_id": 1, "role": "manager"})
        self.orchestrator.agent.api_key = ""
        self.calls = []

    def tearDown(self):
        agent_orchestrator.reset_orchestrator()

    def _stub_execution(self):
        def fake_execute(tool_calls):
            recorded = []
            for tc in tool_calls:
                self.calls.append((tc.function_name, dict(tc.arguments)))
                payload = ({"total_suppliers": 1, "suppliers": [{"supplier_id": 7}]}
                           if tc.function_name == "get_supplier_list"
                           else {"success": True, "po_number": "PO-1"})
                recorded.append({"tool_call_id": tc.id,
                                 "function_name": tc.function_name,
                                 "result": payload, "error": None})
            return recorded

        patched_tools = {"get_supplier_list": lambda **kw: {"suppliers": []},
                         "create_purchase_order": lambda **kw: {"success": True}}
        return (mock.patch.object(self.orchestrator, "_autonomy_allowed", return_value=False),
                mock.patch.object(self.orchestrator, "_execute_tools_with_context",
                                  side_effect=fake_execute),
                mock.patch.dict(self.orchestrator.agent.tool_functions, patched_tools))

    def test_approved_arguments_are_reused_when_a_reference_breaks(self):
        gate, executor, tools = self._stub_execution()
        with gate, executor, tools:
            result = self.orchestrator.run_approved_plan(
                "restock cola", self.PLAN, [2], persisted_results=self.PERSISTED)

        self.assertTrue(result["success"])
        by_step = {sr["step"]: sr for sr in result["step_results"]}
        self.assertEqual(by_step[2]["executed_by"], "approved")
        self.assertEqual(by_step[2]["args_source"], "persisted-approval")
        po_args = [args for name, args in self.calls if name == "create_purchase_order"]
        self.assertEqual(po_args[0]["supplier_id"], 7)

    def test_without_approved_arguments_the_write_does_not_run(self):
        gate, executor, tools = self._stub_execution()
        with gate, executor, tools:
            result = self.orchestrator.run_approved_plan("restock cola", self.PLAN, [2])

        self.assertFalse(result["success"])
        self.assertEqual([name for name, _ in self.calls], ["get_supplier_list"])


# === App-level: a plain yes/no must answer the proposal on the table ===

from app import AgentTask, app, db, Branch, User  # noqa: E402
import app as app_module  # noqa: E402


def _task_payload(status="pending_approval"):
    plan = json.dumps({
        "description": "change the price of Zeno to 2600",
        "steps": [
            {"step": 1, "tool": "search_products", "args": {"query": "Zeno"}},
            {"step": 2, "tool": "update_product_price",
             "args": {"product_id": {"$from": "step1.inventory.0.product_id"},
                      "price": 2600}},
        ],
    })
    steps = json.dumps([
        {"step": 1, "tool": "search_products", "status": "ok",
         "result": {"total_products": 1, "inventory": [{"product_id": 85}]}},
        {"step": 2, "tool": "update_product_price", "status": "proposal",
         "result": {"step": 2, "tool": "update_product_price",
                    "args": {"product_id": 85, "price": 2600}}},
    ])
    return {"command": "change the price of Zeno to 2600", "plan_json": plan,
            "step_results_json": steps, "status": status}


class _FakeOrchestrator:
    """Records approval runs; fails loudly if a plain yes reaches the planner."""

    def __init__(self, result=None):
        self.calls = []
        self.process_calls = []
        self.result = result

    def run_approved_plan(self, command, plan, approved_step_nos, persisted_results=None):
        self.calls.append({"command": command, "plan": plan,
                           "approved": list(approved_step_nos),
                           "persisted_results": persisted_results})
        return self.result or {
            "success": True,
            "message": "Done: 1 approved change(s) applied.",
            "plan": plan,
            "step_results": [
                {"step": 1, "tool": "search_products", "status": "ok",
                 "result": {"total_products": 1}},
                {"step": 2, "tool": "update_product_price", "status": "ok",
                 "result": {"success": True, "new_price": "2600.00"},
                 "executed_by": "approved"},
            ],
            "pending_approvals": [],
        }

    def process_command(self, command, user_id=None):
        self.process_calls.append(command)
        return {"success": True, "message": f"Handled as chat: {command}"}


class ApprovalReplyParsingTests(unittest.TestCase):
    def test_short_affirmatives_are_recognised(self):
        for text in ("yes", "Yes", "YES!", "ok", "Okay.", "Ok I approve",
                     "go ahead", "Approve", "confirmed", "ဟုတ်ကဲ့"):
            self.assertTrue(app_module._is_approval_reply(text), msg=text)

    def test_short_rejections_are_recognised(self):
        for text in ("no", "No thanks", "cancel", "reject", "never mind"):
            self.assertTrue(app_module._is_rejection_reply(text), msg=text)

    def test_real_instructions_are_not_swallowed(self):
        for text in ("yes change the price to 2700", "ok show me stock",
                     "cancel the promotion", "no, make it 2400"):
            self.assertFalse(app_module._is_approval_reply(text), msg=text)
            self.assertFalse(app_module._is_rejection_reply(text), msg=text)


class ChatApprovalReplyEndpointTests(unittest.TestCase):
    """POST /api/agent/chat must answer a pending proposal, not re-plan."""

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

    def _client(self):
        client = app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.user_id
            session['role'] = 'manager'
            session['branch_id'] = self.branch_id
        return client

    def _post(self, client, command):
        return client.post('/api/agent/chat', json={'command': command})

    def test_plain_yes_approves_the_stored_plan_without_replanning(self):
        fake = _FakeOrchestrator()
        with mock.patch('app.get_ai_orchestrator', return_value=fake):
            response = self._post(self._client(), 'Yes')

        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['success'])
        self.assertEqual(data['approval_reply'], 'approved')
        self.assertEqual(data['task_id'], self.task_id)
        self.assertEqual(fake.process_calls, [])          # no second plan
        self.assertEqual(fake.calls[0]['approved'], [2])   # step NUMBER, not index
        self.assertTrue(fake.calls[0]['persisted_results'])
        with app.app_context():
            task = db.session.get(AgentTask, self.task_id)
            self.assertEqual(task.status, 'completed')
            steps = json.loads(task.step_results_json)
            self.assertEqual(steps[1]['executed_by'], 'approved')

    def test_plain_no_rejects_without_running_the_change(self):
        fake = _FakeOrchestrator()
        with mock.patch('app.get_ai_orchestrator', return_value=fake):
            response = self._post(self._client(), 'no')

        self.assertEqual(response.get_json()['approval_reply'], 'rejected')
        self.assertEqual(fake.calls, [])
        with app.app_context():
            self.assertEqual(db.session.get(AgentTask, self.task_id).status, 'rejected')

    def test_yes_without_a_pending_proposal_reaches_the_agent(self):
        with app.app_context():
            task = db.session.get(AgentTask, self.task_id)
            task.status = 'rejected'
            db.session.commit()

        fake = _FakeOrchestrator()
        with mock.patch('app.get_ai_orchestrator', return_value=fake):
            response = self._post(self._client(), 'yes')

        self.assertEqual(fake.process_calls, ['yes'])
        self.assertIsNone(response.get_json().get('approval_reply'))

    def test_new_instruction_while_a_proposal_is_pending_is_not_swallowed(self):
        fake = _FakeOrchestrator()
        with mock.patch('app.get_ai_orchestrator', return_value=fake):
            self._post(self._client(), 'yes but make it 2700 instead')

        self.assertEqual(fake.process_calls, ['yes but make it 2700 instead'])
        self.assertEqual(fake.calls, [])

    def test_expired_proposal_reports_the_expiry_instead_of_running(self):
        with app.app_context():
            task = db.session.get(AgentTask, self.task_id)
            task.created_at = datetime.utcnow() - timedelta(hours=25)
            db.session.commit()

        fake = _FakeOrchestrator()
        with mock.patch('app.get_ai_orchestrator', return_value=fake):
            response = self._post(self._client(), 'yes')

        data = response.get_json()
        self.assertFalse(data['success'])
        self.assertIn('expired', data['message'])
        self.assertEqual(fake.calls, [])
        with app.app_context():
            self.assertEqual(db.session.get(AgentTask, self.task_id).status, 'expired')


if __name__ == "__main__":
    unittest.main()
