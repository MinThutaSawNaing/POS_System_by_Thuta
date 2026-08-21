# Agent Extension Guide

How to add a new AI feature/tool to the POS agent (`ai_tools.py` +
`agent_orchestrator.py`) safely, and the contracts every contributor must
keep green (`test_agent_plan_execute.py`).

## Adding a new tool in 3 steps

1. **Registry entry** — add the tool to `TOOL_METADATA` in `ai_tools.py`
   with all required keys:
   `name`, `description`, `parameters` (JSON-schema dict, `type: object`),
   `category`, `mutates` (bool), `requires_role` (e.g. `"manager"` or
   `None` for read-only), and `description_one_line`.
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
- **Proposals, not executions**: write tools never run directly from the
  agent loop. A plan step calling a mutating tool produces a step_result
  with `status="proposal"` plus an entry in `pending_approvals`; the actual
  mutation happens only after explicit human approval.
- **No mock data**: answers must come from real tool results; the summary
  prompt receives compacted results so the model cannot invent numbers.

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
