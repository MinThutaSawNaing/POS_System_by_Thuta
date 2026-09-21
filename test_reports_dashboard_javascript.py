"""Behavioural tests for the dashboard's report download buttons.

The Warehouse and Purchase & Receiving tabs must download the professionally
formatted version of exactly what they are showing, so each button has to send
the tab's current filters to the export endpoint in a new tab.
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


def _run_export(field_values, blocked=False):
    """Call both export functions with the given tab field values."""
    source = DASHBOARD.read_text(encoding="utf-8")
    helpers = "\n".join(
        _function(source, name)
        for name in ("openReportDownload", "exportWarehouseStock", "exportPurchaseOrders")
    )
    script = f"""
const fieldValues = {json.dumps(field_values)};
const opened = [];
const toasts = [];
const blocked = {"true" if blocked else "false"};
globalThis.window = {{
  open: (url, target) => {{
    opened.push({{ url, target }});
    return blocked ? null : {{ closed: false }};
  }},
}};
globalThis.document = {{
  getElementById: (id) => (id in fieldValues ? {{ value: fieldValues[id] }} : null),
}};
function showToast(message, kind) {{ toasts.push({{ message, kind }}); }}
{helpers}

const results = {{
  warehousePdf: exportWarehouseStock("pdf"),
  warehouseExcel: exportWarehouseStock("xlsx"),
  purchasePdf: exportPurchaseOrders("pdf"),
  purchaseExcel: exportPurchaseOrders("xlsx"),
  warehouseFallback: exportWarehouseStock("csv"),
  purchaseFallback: exportPurchaseOrders(""),
}};
console.log(JSON.stringify({{ opened, toasts, results }}));
"""
    return _run_node_script(script)


def test_export_buttons_sit_in_their_own_sections():
    """Warehouse stock downloads belong to the Warehouse tab, PO downloads to
    the Purchase & Receiving tab."""
    source = DASHBOARD.read_text(encoding="utf-8")
    purchases_start = source.index('id="purchases-section"')
    warehouse_start = source.index('id="warehouse-section"')
    deliveries_start = source.index('id="deliveries-section"')
    assert purchases_start < warehouse_start < deliveries_start

    purchases_html = source[purchases_start:warehouse_start]
    warehouse_html = source[warehouse_start:deliveries_start]

    for report_format, icon in (
        ("pdf", "bi-file-earmark-pdf"),
        ("xlsx", "bi-file-earmark-excel"),
    ):
        assert f'onclick="exportPurchaseOrders(\'{report_format}\')"' in purchases_html
        assert f'onclick="exportWarehouseStock(\'{report_format}\')"' in warehouse_html
        assert icon in purchases_html
        assert icon in warehouse_html

    # No stock-list download button may leak into the purchasing tab.
    assert "exportWarehouseStock" not in purchases_html
    assert "exportPurchaseOrders" not in warehouse_html


def test_warehouse_stock_report_sends_the_tab_filters():
    out = _run_export({
        "warehouse-search": "  coffee  ",
        "warehouse-low-stock-filter": "true",
    })
    urls = [entry["url"] for entry in out["opened"]]
    assert urls[0] == "/api/warehouse/export?format=pdf&q=coffee&low_stock=true"
    assert urls[1] == "/api/warehouse/export?format=xlsx&q=coffee&low_stock=true"
    assert all(entry["target"] == "_blank" for entry in out["opened"])
    assert out["results"]["warehousePdf"] is True
    # Unknown formats fall back to PDF rather than producing a broken download.
    assert urls[4] == "/api/warehouse/export?format=pdf&q=coffee&low_stock=true"


def test_warehouse_stock_report_omits_empty_filters():
    out = _run_export({"warehouse-search": "  ", "warehouse-low-stock-filter": ""})
    assert out["opened"][0]["url"] == "/api/warehouse/export?format=pdf"


def test_purchase_order_report_sends_the_tab_filters():
    out = _run_export({
        "purchase-orders-search": "po-2",
        "purchase-orders-status-filter": "pending",
        "purchase-orders-supplier-filter": "7",
        "purchase-orders-start-date": "2026-09-01",
        "purchase-orders-end-date": "2026-09-30",
    })
    urls = [entry["url"] for entry in out["opened"]]
    expected = (
        "/api/purchase_orders/export?format=xlsx&q=po-2&status=pending"
        "&supplier_id=7&start_date=2026-09-01&end_date=2026-09-30"
    )
    assert urls[3] == expected
    assert urls[2] == expected.replace("format=xlsx", "format=pdf")
    assert urls[5] == expected.replace("format=xlsx", "format=pdf")


def test_blocked_popup_warns_instead_of_silently_failing():
    source = DASHBOARD.read_text(encoding="utf-8")
    assert "showToast" in _function(source, "openReportDownload")

    out = _run_export(
        {"warehouse-search": "", "warehouse-low-stock-filter": ""}, blocked=True
    )
    assert out["results"]["warehousePdf"] is False
    assert out["results"]["purchaseExcel"] is False
    assert len(out["toasts"]) == 6
    assert all("blocked" in toast["message"].lower() for toast in out["toasts"])
    assert all(toast["kind"] == "warning" for toast in out["toasts"])

