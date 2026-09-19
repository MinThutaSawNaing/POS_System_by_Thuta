"""Behavioural tests for the dashboard's product deletion confirmation flow.

Covers the extra confirmation window shown when the selected product already
has sales history: the first delete attempt only asks for confirmation, and
accepting the warning retries the same delete with force=1.
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


def _run_delete_flow(responses):
    """Drive deleteProduct() against scripted DELETE responses.

    Every confirmation window that opens is accepted, mirroring a user who
    keeps pressing Confirm, so the whole two-step flow is exercised.
    """
    source = DASHBOARD.read_text(encoding="utf-8")
    script = f"""
// --- mocks ---
const dialogs = [];
function showConfirmDialog(title, message, onConfirm) {{
  dialogs.push({{ title, message, onConfirm }});
}}
const toasts = [];
function showToast(msg, kind) {{ toasts.push({{ msg, kind }}); }}
let cacheInvalidations = 0;
function invalidateProductsCache() {{ cacheInvalidations += 1; }}
let productReloads = 0;
function loadProducts() {{ productReloads += 1; }}
let statsReloads = 0;
function loadDashboardStats() {{ statsReloads += 1; }}

const responses = {json.dumps(responses)};
const requests = [];
let responseIndex = 0;
function fetch(url, opts) {{
  requests.push({{ url, method: opts && opts.method }});
  const body = responses[responseIndex++];
  return Promise.resolve({{
    status: body.status,
    ok: body.status >= 200 && body.status < 300,
    json: async () => body.json_body,
  }});
}}

// --- code under test ---
{_function(source, "deleteProduct")}
{_function(source, "requestProductDelete")}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
const seenDialogs = [];
(async () => {{
  deleteProduct(42, "Sold Product");
  while (dialogs.length) {{
    const dialog = dialogs.shift();
    seenDialogs.push({{ title: dialog.title, message: dialog.message }});
    dialog.onConfirm();
    await flush();
  }}
  console.log(JSON.stringify({{
    dialogs: seenDialogs,
    toasts,
    requests,
    cacheInvalidations,
    productReloads,
    statsReloads,
  }}));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
    return _run_node_script(script)


def test_unused_product_is_deleted_after_the_first_confirmation():
    out = _run_delete_flow([
        {"status": 200, "json_body": {"success": True, "message": "Product deleted"}},
    ])

    assert [dialog["title"] for dialog in out["dialogs"]] == ["Delete Product"]
    assert out["requests"] == [{"url": "/api/products/42", "method": "DELETE"}]
    assert out["toasts"] == [{"msg": "Product deleted successfully!", "kind": "success"}]
    assert out["cacheInvalidations"] == 1
    assert out["productReloads"] == 1
    assert out["statsReloads"] == 1


def test_sold_product_asks_for_an_extra_confirmation_then_force_deletes():
    out = _run_delete_flow([
        {"status": 400, "json_body": {
            "success": False,
            "has_sales_history": True,
            "requires_confirmation": True,
            "sales_history_count": 3,
            "message": "Cannot delete product 'Sold Product': it has sales history.",
        }},
        {"status": 200, "json_body": {"success": True, "message": "Product deleted"}},
    ])

    assert [dialog["title"] for dialog in out["dialogs"]] == [
        "Delete Product",
        "Product Has Sales History",
    ]
    assert out["dialogs"][1]["message"] == (
        "The product you selected have sale history. "
        "Are you sure you want to delete it?"
    )
    assert out["requests"] == [
        {"url": "/api/products/42", "method": "DELETE"},
        {"url": "/api/products/42?force=1", "method": "DELETE"},
    ]
    assert out["toasts"] == [{"msg": "Product deleted successfully!", "kind": "success"}]
    assert out["cacheInvalidations"] == 1


def test_other_delete_errors_only_show_a_toast():
    out = _run_delete_flow([
        {"status": 400, "json_body": {
            "success": False,
            "message": "Cannot delete product 'Sold Product': it appears on purchase orders.",
        }},
    ])

    assert [dialog["title"] for dialog in out["dialogs"]] == ["Delete Product"]
    assert out["requests"] == [{"url": "/api/products/42", "method": "DELETE"}]
    assert len(out["toasts"]) == 1
    assert out["toasts"][0]["kind"] == "error"
    assert "purchase orders" in out["toasts"][0]["msg"]
    assert out["cacheInvalidations"] == 0


def test_failed_force_retry_reports_the_reason_without_another_dialog():
    out = _run_delete_flow([
        {"status": 400, "json_body": {
            "success": False, "has_sales_history": True, "requires_confirmation": True,
        }},
        {"status": 400, "json_body": {
            "success": False,
            "message": "Cannot delete product 'Sold Product': it has warehouse inventory records.",
        }},
    ])

    assert len(out["dialogs"]) == 2
    assert "?force=1" in out["requests"][1]["url"]
    assert len(out["toasts"]) == 1
    assert "warehouse inventory records" in out["toasts"][0]["msg"]
    assert out["cacheInvalidations"] == 0


def test_confirm_button_runs_its_callback_after_the_dialog_is_hidden():
    """The sales-history window is opened from a confirm callback, so the
    callback must run once the first dialog finished hiding; otherwise
    Bootstrap's hide transition closes the second dialog again."""
    source = DASHBOARD.read_text(encoding="utf-8")

    assert 'modalElement.addEventListener("hidden.bs.modal", onConfirm' in source
