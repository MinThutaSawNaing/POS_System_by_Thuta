"""Behavioural tests for the Returns & Exchanges dashboard tab.

The tab must default to today, send its own filters to the export endpoint, print
a receipt for a workflow, and let the Sales History row jump straight to it.
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


HARNESS = """
const fields = __FIELDS__;
const opened = [];
const toasts = [];
const sectionsShown = [];
const invalidated = [];
const loads = [];
const standalone = __STANDALONE__;
const pageState = { returns: 1 };
const pageSize = { returns: 10 };
globalThis.window = {
  open: (url, target) => {
    opened.push({ url, target });
    return { closed: false, opener: null, location: { replace: (u) => opened.push({ replaced: u }) } };
  },
};
globalThis.document = {
  getElementById: (id) => {
    if (!(id in fields)) fields[id] = "";
    return { get value() { return fields[id]; }, set value(v) { fields[id] = v; } };
  },
};
function showToast(message, kind) { toasts.push({ message, kind }); }
function invalidateCacheGroups(groups) { invalidated.push(groups); }
function loadReturns() { loads.push(true); }
function showSection(id) { sectionsShown.push(id); }
function isStandalonePWA() { return standalone; }
function loadUrlInPwaPrintFrame(url) { opened.push({ pwaFrame: url }); return true; }
__HELPERS__

const today = yangonToday();
const results = {
  exportPdf: exportReturns("pdf"),
  exportExcel: exportReturns("xlsx"),
  exportFallback: exportReturns("csv"),
  receipt: openReturnExchangeReceipt("wf-1"),
};
setReturnRangePreset("today");
const todayStart = fields["returns-start-filter"];
const todayEnd = fields["returns-end-filter"];
setReturnRangePreset("all");
const allStart = fields["returns-start-filter"];
const allEnd = fields["returns-end-filter"];
showReturnsForTransaction("txn-route-1");
console.log(JSON.stringify({
  opened, toasts, today, results,
  todayStart, todayEnd, allStart, allEnd,
  routedSearch: fields["returns-search"],
  sectionsShown, invalidated,
}));
"""


def _run(field_values=None, standalone=False):
    source = DASHBOARD.read_text(encoding="utf-8")
    helpers = "\n".join(
        _function(source, name)
        for name in (
            "yangonToday", "returnsFilterParams", "applyReturnFilters",
            "setReturnRangePreset", "openReturnExchangeReceipt", "exportReturns",
            "showReturnsForTransaction", "openReportDownload",
        )
    )
    script = HARNESS.replace("__FIELDS__", json.dumps(field_values or {}))
    script = script.replace("__STANDALONE__", "true" if standalone else "false")
    script = script.replace("__HELPERS__", helpers)
    return _run_node_script(script)


def test_section_nav_and_cache_are_wired():
    source = DASHBOARD.read_text(encoding="utf-8")
    assert 'id="returns-section"' in source
    assert 'id="returns-table"' in source
    assert "onclick=\"showSection('returns')\"" in source
    assert "returns: () => loadReturns()," in source
    assert 'returns: ["returns"],' in source
    assert '["/api/returns_exchanges", "returns"]' in source
    assert '"returns-section h2": "returns",' in source
    assert 'returns: "Returns",' in source
    # The Sales History row routes to the new tab for a transaction.
    assert "showReturnsForTransaction(" in source


def test_export_sends_the_tab_filters():
    out = _run({
        "returns-start-filter": "2026-10-01",
        "returns-end-filter": "2026-10-31",
        "returns-mode-filter": "exchange",
        "returns-search": "  txn-1  ",
    })
    urls = [entry["url"] for entry in out["opened"] if "url" in entry]
    expected = ("/api/returns_exchanges/export?start=2026-10-01&end=2026-10-31"
                "&mode=exchange&q=txn-1&format=pdf")
    assert urls[0] == expected
    assert out["results"]["exportExcel"] is True
    urls_xlsx = [e["url"] for e in out["opened"] if e.get("url", "").endswith("format=xlsx")]
    assert urls_xlsx[0] == expected.replace("format=pdf", "format=xlsx")
    # Unknown formats fall back to PDF rather than a broken download.
    urls_pdf = [e["url"] for e in out["opened"] if e.get("url", "").endswith("format=pdf")]
    assert urls_pdf[-1] == expected


def test_export_omits_empty_filters():
    out = _run({"returns-start-filter": "  ", "returns-search": ""})
    urls = [entry["url"] for entry in out["opened"] if "url" in entry]
    assert urls[0] == "/api/returns_exchanges/export?format=pdf"


def test_presets_set_and_clear_the_range():
    out = _run()
    assert out["todayStart"] == out["today"]
    assert out["todayEnd"] == out["today"]
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", out["today"])
    assert out["allStart"] == ""
    assert out["allEnd"] == ""
    assert out["invalidated"], "applying a preset must invalidate the returns cache"


def test_receipt_button_opens_the_print_url():
    out = _run()
    replaced = [e["replaced"] for e in out["opened"] if "replaced" in e]
    assert replaced and replaced[0] == "/api/returns_exchanges/wf-1/print?autoprint=1"
    assert out["results"]["receipt"] is True


def test_receipt_uses_pwa_frame_in_standalone_mode():
    out = _run(standalone=True)
    frames = [e["pwaFrame"] for e in out["opened"] if "pwaFrame" in e]
    assert frames == ["/api/returns_exchanges/wf-1/print?autoprint=1"]


def test_sales_row_routes_to_returns_tab_with_the_transaction():
    out = _run()
    assert out["routedSearch"] == "txn-route-1"
    assert out["sectionsShown"][-1] == "returns"
