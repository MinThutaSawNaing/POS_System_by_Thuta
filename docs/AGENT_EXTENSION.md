# Agent Extension Guide

How to add a new AI feature/tool to the POS agent (`ai_tools.py` +
`agent_orchestrator.py`) safely, and the contracts every contributor must
keep green (`test_agent_plan_execute.py`).

## Adding a new tool in 3 steps

1. **Registry entry** — add the tool to `TOOL_METADATA` in `ai_tools.py`
   with all required keys:
   `name`, `description`, `parameters` (JSON-schema dict, `type: object`),
   `category`, `mutates` (bool, descriptive), `description_one_line`, and
   `result_size_hint`.
   `TOOL_SCHEMAS` is derived from the registry; do not maintain a separate copy.
   `get_all_tools()` keys must equal `TOOL_SCHEMAS` keys.
2. **Method** — implement the tool method on the `AITools` class in
   `ai_tools.py`. It receives plain keyword arguments matching the schema,
   runs inside the request context set by the orchestrator, and returns a
   JSON-serializable dict. Never print model-invented data: return real
   database results only.
3. **Optional UI** — if the feature needs a surface beyond chat, add an
   endpoint/template in `app.py`; the agent result contract
   (`success/message/step_results`) is designed to be rendered directly.

## Safety rules

The complete generated contract is in `AI_TOOL_REFERENCE.md`. After changing a
tool schema, run `python generate_ai_tool_docs.py`; signature and documentation
drift are checked by `test_ai_tool_contracts.py`. The planner receives parameter
types, descriptions, defaults and bounds from the same registry, not a separate
handwritten list. This is runtime grounding, not model fine-tuning.

- **mutates**: a descriptive flag only. It tells the planner and the widget
  which steps change data; it does not gate execution.
- **Validation**: validate all arguments against the schema before calling
  the method; unknown tool names are rejected at plan-validation time.
- **No permission or approval layer**: every registered tool runs when the
  agent calls it, including deletes and money changes. Access control lives at
  the HTTP boundary (`agent_required`: manager/owner only); never give a cashier
  account an AI endpoint, widget or memory tab.
- **No mock data**: answers must come from real tool results; the summary
  prompt receives compacted results so the model cannot invent numbers.

## Execution model: no permission layer

Every `TOOL_METADATA` entry records only descriptive facts
(`category`, `mutates`, `description_one_line`, `result_size_hint`). There is
**no** per-tool role check, approval gate, autonomy switch or proposal state:
a plan step that names a registered tool runs immediately, read or write.

What still protects the data:

- The **agent itself is manager/owner only.** `agent_required` in `app.py`
  rejects cashier accounts on every `/api/agent/*` route, the cashier
  allowlist (`CASHIER_API_METHODS`) no longer contains any agent endpoint, and
  the dashboard omits the widget and the AI Memory tab for cashiers.
- **Argument validation** (`ai_tool_guard.py`) runs before any database code:
  unknown tools, undocumented arguments, wrong JSON types, enums and numeric
  bounds are rejected with a corrective message the model can fix once.
- **Tool-layer business guards** still refuse impossible operations
  (`delete_product` cascade/confirm rules, `delete_supplier` with open POs,
  `delete_customer` with debt), independent of the agent.
- **Fail-stop execution**: a failed step aborts the rest and the answer must
  say the task is incomplete.
- **Turn safety**: the executor checks the original request after argument
  repair. Email-only follow-ups cannot mutate non-report records. Product
  changes cannot contradict the latest product search or the previous verified
  product selection for pronoun follow-ups. Multiple matches require a bulk
  request or clarification; an empty search cannot be repaired into a guessed ID.
  Product field edits must be explicit in the current request (otherwise ask
  the user to restate the field). These are conservative safeguards, not a
  complete natural-language authorization system.
- **Truthful outcomes**: nested tool errors and `success=false` fail the step.
  Mutation summaries are formatted from actual results, never narrated by the
  model. Planned user requests and replies both remain in conversation history.
- **Email scope**: `email_report` sends supported report attachments, optionally
  with a note. It is not a standalone custom-message sender; do not substitute
  an inventory report for a request to tell someone about a change.

Regression replays for these rules live in `test_agent_turn_safety.py`. Run them
with `python run_tests.py -q test_agent_turn_safety.py` so application imports use
disposable data rather than the local POS database.

### Removed endpoints

`/api/agent/approve`, `/api/agent/reject`, `/api/agent/task/<id>/advance`,
`/api/agent/task/<id>`, the `agent_autonomy_enabled` setting, the
`GET|POST /api/agent/autonomy` kill switch and the `AgentTask` table have been
removed together with `AgentOrchestrator.run_approved_plan()`. A "yes"/"no"
reply in chat is now an ordinary message, not an approval shortcut.

Contracts live in `test_agent_plan_execute.py`; cashier access is covered by
`test_cashier_permissions.py` and `test_ai_memory_tab.py`.

### Plan-step limit (dynamic)

`MAX_PLAN_STEPS` (5) is only the default. The effective cap comes from the
`ai_max_plan_steps` app setting, read on every planning turn by
`AgentOrchestrator._max_plan_steps()` and clamped to
`MIN_PLAN_STEPS..MAX_PLAN_STEPS_LIMIT` (1..50).

- `GET /api/agent/plan-limit` → `{success, max_plan_steps, default, min, max}`.
- `POST /api/agent/plan-limit` `{max_plan_steps: int}` → saves the value
  (manager/owner only; out-of-range and non-integer values are refused).
- The dashboard exposes it as an **AI Agent limits** card inside the
  AI Memory tab: a number input plus a *Save limit* button, loaded on every
  section open.

Raise it to let one command change more records at once (bulk product edits);
lower it to keep every request small. No restart is needed — the orchestrator
reads the setting per turn.

### Delete guards

Destructive tools refuse in the tool layer itself, independent of the agent:

- `delete_product` — manager/owner only (the dashboard hides the button and
  the API refuses with 403 for other roles). Sales and return/exchange history
  are always kept: those rows are only unlinked from the removed product, so
  reports, receipts, refunds and already-returned quantities stay accurate
  (`return_exchange_item.product_id` is nullable and the startup migration
  rebuilds the table when it is not). Warehouse stock/transfers, promotions,
  purchase order lines and supplier price agreements are removed only with
  `cascade=true`, after the user agreed in the "delete everywhere" window.
  The same rules apply to `DELETE /api/products/<id>` (`?force=1&cascade=1`)
  and to the cleanup summary at `GET /api/products/<id>/dependencies`.
- `delete_supplier` — refuses while non-terminal purchase orders exist.
- `delete_customer` — refuses while outstanding debt balances exist.

Never remove a guarded/destructive tool's own checks: they are the only
remaining brake on a destructive step.

## Plan-then-execute contract

`AgentOrchestrator.process_command(command, user_id)` returns:

```python
{
    "success": bool,
    "message": str,          # final user-facing answer
    "plan": [...],           # validated plan steps (max 5)
    "step_results": [
        {"step": str, "tool": str,
         "status": "ok" | "failed" | "skipped",
         "result": dict | None, "error": str | None}
    ]
}
```

Rules:
- Plans longer than 5 steps are rejected.
- An invalid plan is retried once with the model; on second failure the
  orchestrator falls back to single-shot execution and still returns a
  success-shaped dict.
- `$from`: `"stepN.<key>"` argument references are resolved from earlier step
  outputs between steps (e.g. `{"supplier_id": {"$from":
  "step1.suppliers.0.supplier_id"}}`). Raw `$from` markers must never reach
  the tool layer. Resolution is deliberately forgiving about *spelling*, because
  the planner can only guess key names:
  - `step1`, `step_1`, `steps.1`, `Step 1` and a bare `1` all address step 1;
  - a `result` / `data` / `output` segment is skipped;
  - keys match case-insensitively and ignore `_`/`-` (`productId` ==
    `product_id`), and a singular/plural mismatch is accepted;
  - when a collection word does not exist but the result holds exactly one
    list, that list is used (`step1.products.0.id` finds `inventory`);
  - a plain `id` falls back to the row's only `<x>_id` key (and refuses when
    several could match, instead of guessing).
  A reference that still cannot be resolved raises `FromReferenceError` whose
  message lists the keys that DO exist.
- `TOOL_OUTPUT_HINTS` (`ai_tools.py`) prints one `returns:` line per chained
  read tool in the planning catalog, so plans copy real key names. Plan
  validation (`_validate_from_references`) rejects a reference to a key the
  earlier tool never returns and retries planning once with that shape; the
  retry is advisory, so a stubborn plan still executes through the forgiving
  resolver above rather than dead-ending a change request.
- Broken references never kill a plan. `_execute_plan` resolves arguments in
  this order: the plan's own arguments, then one bounded model repair
  (`MAX_PLAN_STEP_REPAIRS`) that may only rewrite that step's arguments against
  the real result data. Repaired arguments are re-validated by the guard and
  the step records `args_source: "repaired"` for audit.
- Fail-stop: when a step raises, its status is `"failed"` and every later
  step is `"skipped"`; later steps are never executed.
- When any step fails, `success` is false and `message` must indicate the
  task is incomplete.

## Answer formatting contract

`_format_tool_results_for_user(tool_results, original_command)` builds the
user-visible answer straight from the tool payload — for data questions it *is*
the answer, so it has to carry the field the user asked for and it must never
raise.

- **Money carries the store's currency.** Never write a `$` into an answer:
  `_money(value)` renders `$1,234.50` for USD and `1,234.50 MMK` for MMK, using
  `money_display` / `currency_label` from `ai_tools` with `currency_code` read
  through the orchestrator's settings function. A Myanmar shop that was told
  its own prices were "in USD format" lost trust in the whole plan.
- **Money is a string.** Tool payloads carry amounts already formatted
  (`"68,000.00"`, `"1,050,000.00"`). Format them with `money_str(...)` /
  `money_dec(...)` from `ai_tools`; `f"{amount:.2f}"` raises `ValueError` on a
  string and used to surface as the generic "I'm sorry, something went wrong.
  Please try again." reply.
- **A specific question needs a specific field.** `search_products` renders as
  `🔎 Product Search` with SKU, barcode, category, price and stock per match;
  `get_product_details` always prints the barcode; `get_inventory_status` and
  `get_low_stock_items` add `Barcode:` when the request asked for one
  (`original_command` is passed in for exactly this). Never answer a "what is
  the barcode / price / id" question with a stock-only summary.
- **Optional fields use `_text_or_placeholder`** (`"not set"`) and `.get(...)`,
  never a bare `row['key']`, so a missing key cannot break a whole list.
- **Failures stay local.** Each result is rendered through
  `_format_single_result`, which degrades to `_readable_result` (a plain
  sentence) on any exception. One unreadable payload can therefore never hide
  the other results or turn the turn into an error message.
- Add a case to `test_agent_result_formatting.py` for every new tool payload
  shape, including the "asked for an identifier" path.

## Truthful answers

- **Failures are never narrated by a model.**
  `_generate_plan_summary` returns `_deterministic_outcome_message(...)` whenever
  a plan ends in a failed/skipped step. That message shows the read data and
  then each failed step with its real error, because an LLM narrating an
  outcome invented a currency and mangled an amount into "50.00 add,000 MMK".
  Read-only multi-step plans may still use the model to explain data.
- **Counts come from the payload.** `search_products` reports `total_products`
  as the true number of matches (only `inventory` is capped), so "how many X do
  we have" is answered with a real number instead of a page size. A query whose
  words are not in the catalogue falls back to token matching across name,
  barcode and category, and a zero-match payload carries `query`,
  `branch_total`, `categories` and `suggestions`.
- **Numeric arguments are numbers.** Tool results show prices as formatted text
  (`"5,350,000.00"`), so a planner copying one back into `price` is common.
  `ai_tool_guard.coerce_number` accepts a clean numeric string — `"5350000"`,
  `"5,350,000"`, `"5,350,000 MMK"` — for any parameter the schema declares as
  `number`/`integer`, and rejects text that still contains arithmetic
  (`"5300000 + 50000"`) with a message quoting what arrived. A rejected
  argument is repairable: `_prepare_step_arguments` gives the model the same one
  bounded repair it gives a broken `$from` reference, so an amount written as
  text no longer kills every write in the plan.
- **A zero-match search is a retry, not an answer.** The orchestrator spends one
  bounded retry (`_retry_empty_product_search`) letting the model search again
  with a name that actually exists; when it cannot, the formatter names the real
  categories and product names so the user can pick one.

## Token-budget guidelines

- Compact tool results before they enter the summary prompt
  (`_compact_result`): truncate long lists and strings, but preserve
  aggregate keys such as counts/totals (`total_products`, `totals`, ...).
- Keep the serialized summary payload small (target well under ~60 KB);
  tests assert large payloads (`500 rows x 200-char fields`) are truncated.
- Prefer `description_one_line` for per-tool prompt text; full
  descriptions stay in the registry for humans.
