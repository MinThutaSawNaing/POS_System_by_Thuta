"""Behavioural tests for the dashboard's product deletion flow.

Deleting a product is manager-only. The flow asks the backend which tabs use
the product and then opens either the plain confirmation (nothing wired) or the
"delete everywhere" window that lists what will be removed, what is kept as
history, and what must be handled in another tab first.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


DASHBOARD = Path(__file__).parent / "templates" / "dashboard.html"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is required")


def _run_node_script(script):
    """Run a Node script from a temp file (node -e overflows Windows limits)."""
    handle, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(script)
        result = subprocess.run(
            [NODE, path], check=True, capture_output=True, text=True, encoding="utf-8"
        )
        return json.loads(result.stdout)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _function(source, name):
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", source)
    assert match, f"function {name} not found"
    depth = 0
    for index in range(match.end() - 1, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[match.start():index + 1]
    raise AssertionError(f"function {name} has unbalanced body")


def _run_delete_flow(dependencies, responses, role="manager", branch=None):
    """Drive deleteProduct() against scripted GET /dependencies + DELETE responses.

    Every window that opens is accepted, mirroring a manager who keeps pressing
    the confirm button, so the whole flow is exercised.
    """
    source = DASHBOARD.read_text(encoding="utf-8")
    script = f"""
// --- mocks ---
let CURRENT_USER_ROLE = {json.dumps(role)};
let currentBranch = {json.dumps(branch)};
const confirmDialogs = [];
function showConfirmDialog(title, message, onConfirm) {{
  confirmDialogs.push({{ title, message, onConfirm }});
}}
const cleanupDialogs = [];
function showProductCleanupDialog(productName, groups, canDelete, onConfirm) {{
  cleanupDialogs.push({{ productName, groups, canDelete, onConfirm }});
}}
const toasts = [];
function showToast(msg, kind) {{ toasts.push({{ msg, kind }}); }}
let cacheInvalidations = 0;
function invalidateProductsCache() {{ cacheInvalidations += 1; }}
let productReloads = 0;
function loadProducts() {{ productReloads += 1; }}
let statsReloads = 0;
function loadDashboardStats() {{ statsReloads += 1; }}

const dependencies = {json.dumps(dependencies)};
const responses = {json.dumps(responses)};
const requests = [];
let responseIndex = 0;
function fetch(url, opts) {{
  requests.push({{ url, method: opts && opts.method ? opts.method : null }});
  if (!opts || !opts.method) {{
    return Promise.resolve({{ status: 200, ok: true, json: async () => dependencies }});
  }}
  const body = responses[responseIndex++];
  return Promise.resolve({{
    status: body.status,
    ok: body.status >= 200 && body.status < 300,
    json: async () => body.json_body,
  }});
}}

// --- code under test ---
{_function(source, "hasManagerOrBossAccess")}
{_function(source, "deleteProduct")}
{_function(source, "requestProductDelete")}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
const seenConfirmDialogs = [];
const seenCleanupDialogs = [];
(async () => {{
  deleteProduct(42, "Sold Product");
  await flush();
  for (let guard = 0; guard < 10; guard += 1) {{
    if (confirmDialogs.length) {{
      const dialog = confirmDialogs.shift();
      seenConfirmDialogs.push({{ title: dialog.title, message: dialog.message }});
      dialog.onConfirm();
    }} else if (cleanupDialogs.length) {{
      const dialog = cleanupDialogs.shift();
      seenCleanupDialogs.push({{
        productName: dialog.productName,
        groups: dialog.groups,
        canDelete: dialog.canDelete,
      }});
      if (dialog.canDelete) dialog.onConfirm();
    }} else {{
      break;
    }}
    await flush();
  }}
  console.log(JSON.stringify({{
    confirmDialogs: seenConfirmDialogs,
    cleanupDialogs: seenCleanupDialogs,
    toasts,
    requests,
    cacheInvalidations,
    productReloads,
    statsReloads,
  }}));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
    return _run_node_script(script)


SALES_GROUP = {
    "key": "sales_history", "label": "Sales history lines", "count": 3,
    "action": "keep", "detail": "kept for reports",
}
PROMOTION_GROUP = {
    "key": "promotions", "label": "Promotions", "count": 1, "action": "delete",
}
PURCHASE_ORDER_GROUP = {
    "key": "purchase_order_items", "label": "Purchase order lines", "count": 2,
    "action": "delete",
}
RETURNS_GROUP = {
    "key": "returns_exchanges", "label": "Return / exchange lines", "count": 1,
    "action": "keep", "detail": "kept for refund history",
}
# No backend group requests this today; the window must still refuse to delete
# when one ever does.
BLOCKED_GROUP = {
    "key": "blocked_records", "label": "Records needing another tab", "count": 1,
    "action": "block",
}


def test_manager_delete_without_dependencies_uses_the_plain_confirmation():
    out = _run_delete_flow(
        {"success": True, "groups": [], "can_delete": True},
        [{"status": 200, "json_body": {"success": True, "message": "Product deleted"}}],
    )

    assert [dialog["title"] for dialog in out["confirmDialogs"]] == ["Delete Product"]
    assert out["cleanupDialogs"] == []
    assert out["requests"] == [
        {"url": "/api/products/42/dependencies", "method": None},
        {"url": "/api/products/42", "method": "DELETE"},
    ]
    assert out["toasts"] == [{"msg": "Product deleted successfully!", "kind": "success"}]
    assert out["cacheInvalidations"] == 1


def test_wired_product_opens_the_delete_everywhere_window():
    out = _run_delete_flow(
        {"success": True, "can_delete": True,
         "groups": [SALES_GROUP, PROMOTION_GROUP, PURCHASE_ORDER_GROUP]},
        [{"status": 200, "json_body": {"success": True, "message": "Product deleted"}}],
    )

    assert out["confirmDialogs"] == []
    assert len(out["cleanupDialogs"]) == 1
    cleanup = out["cleanupDialogs"][0]
    assert cleanup["productName"] == "Sold Product"
    assert cleanup["canDelete"] is True
    assert [group["key"] for group in cleanup["groups"]] == [
        "sales_history", "promotions", "purchase_order_items",
    ]
    # The manager's confirmation must delete everywhere in one request.
    assert out["requests"] == [
        {"url": "/api/products/42/dependencies", "method": None},
        {"url": "/api/products/42?force=1&cascade=1", "method": "DELETE"},
    ]
    assert out["toasts"] == [{"msg": "Product deleted successfully!", "kind": "success"}]


def test_a_blocking_group_disables_the_delete_everywhere_window():
    out = _run_delete_flow(
        {"success": True, "can_delete": False, "blocked_by": ["blocked_records"],
         "groups": [BLOCKED_GROUP]},
        [],
    )

    assert len(out["cleanupDialogs"]) == 1
    assert out["cleanupDialogs"][0]["canDelete"] is False
    # A blocked window never fires a delete request.
    assert out["requests"] == [{"url": "/api/products/42/dependencies", "method": None}]
    assert out["toasts"] == []


def test_cashier_never_opens_the_delete_flow():
    out = _run_delete_flow(
        {"success": True, "groups": [], "can_delete": True}, [], role="cashier"
    )

    assert out["requests"] == []
    assert out["confirmDialogs"] == []
    assert out["cleanupDialogs"] == []
    assert out["toasts"] == [
        {"msg": "Only a manager can delete products.", "kind": "error"}
    ]
    assert out["cacheInvalidations"] == 0


def test_cascade_fallback_when_records_appear_after_the_check():
    """A late cascade refusal still routes the manager through the window."""
    out = _run_delete_flow(
        {"success": True, "groups": [], "can_delete": True},
        [
            {"status": 400, "json_body": {
                "success": False,
                "requires_cascade": True,
                "product_name": "Sold Product",
                "dependency_groups": [PROMOTION_GROUP],
                "blocked_by": [],
                "message": "still used in other tabs",
            }},
            {"status": 200, "json_body": {"success": True, "message": "Product deleted"}},
        ],
    )

    assert [dialog["title"] for dialog in out["confirmDialogs"]] == ["Delete Product"]
    assert len(out["cleanupDialogs"]) == 1
    assert out["cleanupDialogs"][0]["canDelete"] is True
    assert out["requests"] == [
        {"url": "/api/products/42/dependencies", "method": None},
        {"url": "/api/products/42", "method": "DELETE"},
        {"url": "/api/products/42?force=1&cascade=1", "method": "DELETE"},
    ]
    assert out["toasts"] == [{"msg": "Product deleted successfully!", "kind": "success"}]


def _run_cleanup_dialog(groups, can_delete):
    """Render the cleanup window against a minimal DOM stub."""
    source = DASHBOARD.read_text(encoding="utf-8")
    match = re.search(r"const PRODUCT_DEPENDENCY_ACTIONS = \{[^;]*\};", source)
    assert match, "PRODUCT_DEPENDENCY_ACTIONS not found"

    script = f"""
// --- minimal DOM stub ---
function makeNode() {{
  return {{
    children: [],
    className: '',
    classList: {{ classes: new Set(), add(c) {{ this.classes.add(c); }}, remove(c) {{ this.classes.delete(c); }}, contains(c) {{ return this.classes.has(c); }} }},
    textContent: '',
    title: '',
    disabled: false,
    innerHTML: '',
    appendChild(child) {{ this.children.push(child); return child; }},
    append(...nodes) {{ nodes.forEach((node) => this.children.push(node)); }},
  }};
}}
const nodes = {{}};
globalThis.document = {{
  getElementById(id) {{ if (!nodes[id]) nodes[id] = makeNode(); return nodes[id]; }},
  createElement() {{ return makeNode(); }},
}};
let shown = null;
globalThis.bootstrap = {{ Modal: {{ getOrCreateInstance(element) {{ return {{ show() {{ shown = element; }} }}; }} }} }};

// --- code under test ---
{match.group(0)}
{_function(source, "showProductCleanupDialog")}

showProductCleanupDialog("Sold Product", {json.dumps(groups)}, {json.dumps(can_delete)}, () => {{}});
const list = nodes["productCleanupList"];
console.log(JSON.stringify({{
  intro: nodes["productCleanupIntro"].textContent,
  warning: nodes["productCleanupWarning"].textContent,
  warningHidden: nodes["productCleanupWarning"].classList.contains("d-none"),
  confirmDisabled: nodes["productCleanupConfirmBtn"].disabled,
  shown: shown !== null,
  rows: list.children.map((row) => ({{
    label: row.children[0].children[0].textContent,
    detail: row.children[0].children.length > 1 ? row.children[0].children[1].textContent : "",
    badge: row.children[1].textContent,
    badgeClass: row.children[1].className,
  }})),
}}));
"""
    return _run_node_script(script)


def test_cleanup_window_lists_each_group_with_its_action():
    out = _run_cleanup_dialog(
        [SALES_GROUP, PROMOTION_GROUP, PURCHASE_ORDER_GROUP], True
    )

    assert 'Delete "Sold Product"' in out["intro"]
    assert out["shown"] is True
    assert out["confirmDisabled"] is False
    assert out["rows"] == [
        {"label": "Sales history lines", "detail": "kept for reports",
         "badge": "3 - kept as history", "badgeClass": "badge text-bg-secondary text-nowrap"},
        {"label": "Promotions", "detail": "",
         "badge": "1 - will be deleted", "badgeClass": "badge text-bg-danger text-nowrap"},
        {"label": "Purchase order lines", "detail": "",
         "badge": "2 - will be deleted", "badgeClass": "badge text-bg-danger text-nowrap"},
    ]
    # The manager still sees the exact warning sentence about sales history.
    assert "The product you selected have sale history" in out["warning"]
    assert "Are you sure you want to delete it?" in out["warning"]


def test_cleanup_window_disables_confirm_for_a_blocking_group():
    out = _run_cleanup_dialog([BLOCKED_GROUP], False)

    assert out["confirmDisabled"] is True
    assert "handled in their own tab first" in out["warning"]
    assert "handle first" in out["rows"][0]["badge"]


def test_cleanup_window_shows_returns_as_kept_history():
    out = _run_cleanup_dialog([RETURNS_GROUP], True)

    assert out["confirmDisabled"] is False
    assert "kept as history" in out["rows"][0]["badge"]


def test_branch_id_is_propagated_to_both_requests():
    out = _run_delete_flow(
        {"success": True, "groups": [], "can_delete": True},
        [{"status": 200, "json_body": {"success": True, "message": "Product deleted"}}],
        branch={"id": 7},
    )

    assert out["requests"] == [
        {"url": "/api/products/42/dependencies?branch_id=7", "method": None},
        {"url": "/api/products/42?branch_id=7", "method": "DELETE"},
    ]


def test_product_delete_button_is_rendered_for_managers_only():
    source = DASHBOARD.read_text(encoding="utf-8")

    marker = source.index("const deleteButton = hasManagerOrBossAccess()")
    guarded_block = source[marker:marker + 400]
    assert "deleteProduct(" in guarded_block
    assert ': "";' in guarded_block
    # And the handler itself refuses non-managers even with a stale page.
    handler = source[source.index("function deleteProduct("):]
    assert "Only a manager can delete products." in handler[:600]


def test_confirm_buttons_run_their_callback_after_the_dialog_is_hidden():
    """Follow-up windows are opened from these callbacks, so each callback must
    run once its dialog finished hiding; otherwise Bootstrap's hide transition
    closes the next dialog again."""
    source = DASHBOARD.read_text(encoding="utf-8")

    assert 'modalElement.addEventListener("hidden.bs.modal", onConfirm' in source
    assert 'id="productCleanupModal"' in source
    assert 'id="productCleanupConfirmBtn"' in source
