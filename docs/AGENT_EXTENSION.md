# Agent Extension Guide

How to add a new AI feature/tool to the POS agent (`ai_tools.py` +
`agent_orchestrator.py`) safely, and the contracts every contributor must
keep green (`test_agent_plan_execute.py`).

## Adding a new tool in 3 steps

1. **Registry entry** — add the tool to `TOOL_METADATA` in `ai_tools.py`
   with all required keys:
   `name`, `description`, `parameters` (JSON-schema dict, `type: object`),
   `category`, `mutates` (bool), `requires_role` (e.g. `"manager"` or
   `None` for read-only), `autonomy` (`"auto"` or `"approval"` — use
   `"auto"` only for low-risk writes on the `_AUTO_EXECUTE_TOOLS` list;
   `"approval"` is the default), and `description_one_line`.
   Then add a matching entry to `TOOL_SCHEMAS` (same `name`, `description`,
   `parameters`) — this legacy registry must stay backward-compatible;
   `get_all_tools()` keys must equal `TOOL_SCHEMAS` keys.
2. **Method** — implement the tool method on the `AITools` class in
   `ai_tools.py`. It receives plain keyword arguments matching the schema,
   runs inside the request context set by the orchestrator, and returns a
   JSON-serializable dict. Never print model-invented data: return real
   database results only.
3. **Optional UI** — if the feature needs a surface beyond chat, add an
   endpoint/template in `app.py`; the agent result contract
   (`success/message/step_results/pending_approvals`) is designed to be
   rendered directly.

## Safety rules

- **mutates / requires_role**: any tool with `mutates=True` MUST set
  `requires_role` (currently `"manager"`). Read-only tools use
  `requires_role=None`. Enforced by
  `RegistryCompletenessTests.test_mutating_tools_require_a_role`.
- **Validation**: validate all arguments against the schema before calling
  the method; unknown tool names are rejected at plan-validation time.
- **Proposals by default, autonomy as the narrow exception**: write tools
  never run directly from the agent loop unless they are explicitly marked
  autonomous AND every gate below passes. A blocked or non-autonomous write
  produces a step_result with `status="proposal"` plus an entry in
  `pending_approvals`; the actual mutation happens only after explicit human
  approval.
- **No mock data**: answers must come from real tool results; the summary
  prompt receives compacted results so the model cannot invent numbers.

## Smart autonomy

Every `TOOL_METADATA` entry carries an **`autonomy`** field:

- `"auto"` — low-risk writes that may execute immediately (see conditions).
- `"approval"` — everything else; these are always proposals. This is the
  **default**: `_AUTO_EXECUTE_TOOLS` in `ai_tools.py` is the explicit opt-in
  list, and any mutating tool not on it gets `"approval"`. Enforced by
  `AutonomyTests.test_registry_contract_for_autonomy_fields`.

### Auto-execute conditions

A mutating plan step executes immediately (`status="ok"`,
`executed_by="agent-auto"`) only when ALL of the following hold, checked once
per plan via `AgentOrchestrator._autonomy_allowed()`:

1. the step's tool has `autonomy == "auto"` in `TOOL_METADATA`;
2. the `agent_autonomy_enabled` kill-switch setting parses truthy
   (`"true"/"1"/"on"/"yes"`, case-insensitive);
3. the current user resolves and their role is exactly `"manager"`
   (cashiers/admins/anonymous never get autonomy).

If any condition fails, the same step becomes a proposal and the tool function
is never invoked.

### Approval execution (human-approved proposals)

A proposal becomes a real database write ONLY after a human approves that
exact persisted step:

1. `POST /api/agent/approve/<task_id>/<step_no>` records `approved: true` on
   the proposal inside the task's persisted `step_results_json`.
2. `POST /api/agent/task/<task_id>/advance` calls
   `AgentOrchestrator.run_approved_plan(command, plan, approved_step_nos)`:
   the persisted plan is re-run deterministically (fresh read data, ZERO LLM
   calls) and approved mutating steps execute tagged
   `"executed_by": "approved"`. Arguments come only from the persisted plan —
   never from the request.
3. Proposals expire: `/advance` refuses tasks older than
   `AI_APPROVAL_TTL_HOURS` (default 24; `<= 0` disables expiry) and marks
   them `expired`.
4. `POST /api/agent/reject/<task_id>/<step_no>` records rejection; when every
   proposal is decided and none are approved, the task status becomes
   `rejected`. The widget's Reject button is wired to this endpoint.

Contracts live in `test_agent_approval.py`.

### Kill switch endpoints (`app.py`)

- `GET /api/agent/autonomy` — `{success, enabled}`; backed by
  `get_agent_autonomy_enabled()` which defaults to **OFF** when the setting
  is missing or unparsable.
- `POST /api/agent/autonomy` — manager-only (`@manager_required`); toggles
  the setting. The dashboard toggle consumes both.

The orchestrator-side truthy check is unit-tested directly on
`_autonomy_allowed` (`AutonomyTests`) without importing `app.py`, keeping
these tests free of Flask/app-import side effects.

### Current auto vs approval tools

- **auto** (`_AUTO_EXECUTE_TOOLS`): `register_customer`, `create_supplier`,
  `update_supplier`, `update_customer`, `create_category`.
- **approval** (default for all other writes), notably:
  `delete_product`, `delete_supplier`, `delete_customer`,
  `update_product_price`, `write_off_debt`, `create_purchase_order`,
  `adjust_product_stock`, `create_promotion`, `record_debt_payment`,
  `update_delivery_stage`.

### Delete guards

Destructive tools are approval-tier AND refuse in the tool layer itself,
independent of the agent:

- `delete_product` — refuses if the product has sales history, purchase-order
  items, warehouse stock, or an active promotion.
- `delete_supplier` — refuses while non-terminal purchase orders exist.
- `delete_customer` — refuses while outstanding debt balances exist.

Never downgrade a guarded/destructive tool to `"auto"`: the registry test
pins `delete_*`, `update_product_price`, and `write_off_debt` to
`"approval"`.

## Plan-then-execute contract

`AgentOrchestrator.process_command(command, user_id)` returns:

```python
{
    "success": bool,
    "message": str,          # final user-facing answer
    "plan": [...],           # validated plan steps (max 5)
    "step_results": [
        {"step": str, "tool": str,
         "status": "ok" | "failed" | "proposal" | "skipped",
         "result": dict | None, "error": str | None}
    ],
    "pending_approvals": [...]  # non-empty when proposals exist
}
```

Rules:
- Plans longer than 5 steps are rejected.
- An invalid plan is retried once with the model; on second failure the
  orchestrator falls back to single-shot execution and still returns a
  success-shaped dict.
- `$from`: `"stepN.path"` argument references are resolved from earlier
  step outputs between steps (e.g. `{"supplier_id": {"$from":
  "step1.suppliers.0.supplier_id"}}`). Raw `$from` markers must never
  reach the tool layer.
- Fail-stop: when a step raises, its status is `"failed"` and every later
  step is `"skipped"`; later steps are never executed.
- When any step fails, `success` is false and `message` must indicate the
  task is incomplete.

## Token-budget guidelines

- Compact tool results before they enter the summary prompt
  (`_compact_result`): truncate long lists and strings, but preserve
  aggregate keys such as counts/totals (`total_products`, `totals`, ...).
- Keep the serialized summary payload small (target well under ~60 KB);
  tests assert large payloads (`500 rows x 200-char fields`) are truncated.
- Prefer `description_one_line` for per-tool prompt text; full
  descriptions stay in the registry for humans and approvals.
