"""Behavioural tests for the Products tab multi-select and its bulk actions.

The Products tab is the only place a manager can act on many products at once:
rows carry a checkbox, the selection survives paging, and every bulk action
reuses the existing single-product endpoints (labels, export, category, delete).
The confirmation windows are Bootstrap modals, never native browser dialogs.
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
            [NODE, path], capture_output=True, text=True, encoding="utf-8"
        )
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
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


def _source():
    return DASHBOARD.read_text(encoding="utf-8")


# A DOM stub rich enough for the selection helpers: nodes keep listeners,
# classes, children and a value, and getElementById() creates them on demand.
DOM_STUB = r"""
function makeNode(tag = "div") {
  const node = {
    tagName: String(tag).toUpperCase(), children: [], className: "", textContent: "",
    _value: "", _html: "", disabled: false, indeterminate: false, checked: false,
    dataset: {}, listeners: {}, style: {},
    classList: {
      set: new Set(),
      add(name) { this.set.add(name); },
      remove(name) { this.set.delete(name); },
      toggle(name, force) {
        const on = force === undefined ? !this.set.has(name) : Boolean(force);
        if (on) this.set.add(name); else this.set.delete(name);
        return on;
      },
      contains(name) { return this.set.has(name); },
    },
    appendChild(child) { this.children.push(child); return child; },
    append(...nodes) { nodes.forEach((child) => this.children.push(child)); },
    replaceChildren(...nodes) { this.children = []; this.append(...nodes); },
    addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); },
    removeEventListener(type, listener) {
      this.listeners[type] = (this.listeners[type] || []).filter((item) => item !== listener);
    },
    emit(type, event = {}) { (this.listeners[type] || []).slice().forEach((fn) => fn(event)); },
    querySelectorAll() { return []; },
    closest() { return null; },
  };
  Object.defineProperty(node, "value", {
    get() { return node._value; },
    set(value) { node._value = String(value === null || value === undefined ? "" : value); },
  });
  Object.defineProperty(node, "innerHTML", {
    get() { return node._html; },
    set(value) { node._html = String(value); node.children = []; },
  });
  return node;
}
const nodes = Object.create(null);
function node(id) { return nodes[id] ||= makeNode(id); }
const toasts = [];
function showToast(message, kind) { toasts.push({ message, kind }); }
"""


def test_rows_carry_a_checkbox_and_keep_their_ticked_state():
    source = _source()
    helpers = "\n".join(_function(source, name) for name in (
        "parsePagedResponse", "getProductPageCheckboxes", "syncProductSelectionUi",
        "renderProductsResponse",
    ))
    script = DOM_STUB + helpers + r"""
const pageCheckboxes = [];
const table = makeNode("tbody");
table.querySelectorAll = (selector) =>
  selector === ".product-select-checkbox" ? pageCheckboxes : [];
const document = {
  getElementById(id) { return id === "products-table" ? table : node(id); },
  createElement(tag) { return makeNode(tag); },
};
const productSelection = new Map([["2", { id: 2, name: "Selected", barcode: "" }]]);
function escapeHtml(value) { return String(value === null || value === undefined ? "" : value); }
function getProductPhotoHtml() { return ""; }
function hasManagerOrBossAccess() { return true; }
function hasCapability() { return true; }
function renderPagination() {}
function loadProducts() {}

renderProductsResponse({ items: [
  { id: 1, name: "Plain", barcode: "A1", price: 1, stock: 2, tax_rate: 0 },
  { id: 2, name: "Selected", barcode: "A2", price: 2, stock: 3, tax_rate: 0 },
], total: 2, total_pages: 1, page: 1, per_page: 30 });

const rows = table.children;
console.log(JSON.stringify({
  rowCount: rows.length,
  selectedRow: rows[1].dataset.productId,
  selectedRowClass: rows[1].className,
  plainRowClass: rows[0].className,
  plainHtml: rows[0].innerHTML,
  selectedHtml: rows[1].innerHTML,
  barHidden: node("products-bulk-bar").classList.contains("d-none"),
  count: node("products-selection-count").textContent,
  selectAllDisabled: node("products-select-all").disabled,
}));
"""
    out = _run_node_script(script)

    assert out["rowCount"] == 2
    assert out["selectedRow"] == "2"
    assert out["selectedRowClass"] == "product-row-selected"
    assert out["plainRowClass"] == ""
    # Every row offers a checkbox keyed by the product id.
    assert 'class="form-check-input product-select-checkbox"' in out["plainHtml"]
    assert 'data-id="1"' in out["plainHtml"]
    assert "checked" not in out["plainHtml"]
    assert 'data-id="2"' in out["selectedHtml"]
    assert "checked" in out["selectedHtml"]
    # The bulk bar reflects the retained selection even after a re-render.
    assert out["barHidden"] is False
    assert out["count"] == "1 product selected"
    # The header checkbox cannot select rows that are not on screen.
    assert out["selectAllDisabled"] is True


def test_selection_survives_paging_and_clear_resets_everything():
    source = _source()
    helpers = "\n".join(_function(source, name) for name in (
        "selectedProductIds", "getProductPageCheckboxes", "selectProductRow",
        "toggleProductSelection", "toggleAllProductRows", "clearProductSelection",
        "syncProductSelectionUi", "ensureProductSelection",
    )) + "\nconst PRODUCT_SELECTION_LIMIT = 200;"
    script = DOM_STUB + helpers + r"""
const pageCheckboxes = [];
const table = makeNode("tbody");
table.querySelectorAll = (selector) =>
  selector === ".product-select-checkbox" ? pageCheckboxes : [];
const document = {
  getElementById(id) { return id === "products-table" ? table : node(id); },
  createElement(tag) { return makeNode(tag); },
};
const productSelection = new Map();

function makeCheckbox(id) {
  const checkbox = makeNode("input");
  checkbox.dataset.id = String(id);
  checkbox.dataset.name = "Product " + id;
  checkbox.dataset.barcode = "B" + id;
  return checkbox;
}

// Page 1: tick two rows by hand, then use the header checkbox for all of them.
const firstPage = [makeCheckbox(1), makeCheckbox(2), makeCheckbox(3)];
pageCheckboxes.push(...firstPage);
firstPage[0].checked = true; toggleProductSelection(firstPage[0]);
firstPage[1].checked = true; toggleProductSelection(firstPage[1]);
const afterTwo = { ids: selectedProductIds(), count: node("products-selection-count").textContent,
  indeterminate: node("products-select-all").indeterminate,
  barHidden: node("products-bulk-bar").classList.contains("d-none") };

node("products-select-all").checked = true;
toggleAllProductRows(true);
const afterAll = { ids: selectedProductIds(), checked: node("products-select-all").checked };

// Page 2 replaces the on-screen rows; the selection must not be forgotten.
const secondPage = [makeCheckbox(4)];
pageCheckboxes.length = 0;
pageCheckboxes.push(...secondPage);
secondPage[0].checked = true; toggleProductSelection(secondPage[0]);
const afterPaging = { ids: selectedProductIds(), count: node("products-selection-count").textContent,
  pageAllChecked: node("products-select-all").checked,
  keep: ensureProductSelection() };

clearProductSelection();
const cleared = { ids: selectedProductIds(), count: node("products-selection-count").textContent,
  barHidden: node("products-bulk-bar").classList.contains("d-none"),
  pageChecked: secondPage[0].checked, keep: ensureProductSelection() };

console.log(JSON.stringify({ afterTwo, afterAll, afterPaging, cleared, toasts }));
"""
    out = _run_node_script(script)

    assert out["afterTwo"]["ids"] == [1, 2]
    assert out["afterTwo"]["count"] == "2 products selected"
    assert out["afterTwo"]["indeterminate"] is True
    assert out["afterTwo"]["barHidden"] is False
    assert out["afterAll"]["ids"] == [1, 2, 3]
    assert out["afterAll"]["checked"] is True
    assert out["afterPaging"]["ids"] == [1, 2, 3, 4]
    assert out["afterPaging"]["count"] == "4 products selected"
    assert out["afterPaging"]["pageAllChecked"] is True
    assert out["afterPaging"]["keep"] is True
    assert out["cleared"]["ids"] == []
    assert out["cleared"]["count"] == "No products selected"
    assert out["cleared"]["barHidden"] is True
    assert out["cleared"]["pageChecked"] is False
    assert out["cleared"]["keep"] is False
    assert out["toasts"] == [{"message": "Select at least one product first.", "kind": "warning"}]


def test_selection_is_capped_so_bulk_requests_stay_bounded():
    source = _source()
    helpers = "\n".join(_function(source, name) for name in (
        "selectedProductIds", "getProductPageCheckboxes", "selectProductRow",
        "toggleProductSelection", "syncProductSelectionUi",
    )) + "\nconst PRODUCT_SELECTION_LIMIT = 3;"
    script = DOM_STUB + helpers + r"""
const pageCheckboxes = [];
const table = makeNode("tbody");
table.querySelectorAll = (selector) =>
  selector === ".product-select-checkbox" ? pageCheckboxes : [];
const document = {
  getElementById(id) { return id === "products-table" ? table : node(id); },
  createElement(tag) { return makeNode(tag); },
};
const productSelection = new Map();
function makeCheckbox(id) {
  const checkbox = makeNode("input");
  checkbox.dataset.id = String(id);
  checkbox.checked = true;
  pageCheckboxes.push(checkbox);
  return checkbox;
}
[1, 2, 3, 4].forEach((id) => toggleProductSelection(makeCheckbox(id)));
console.log(JSON.stringify({
  ids: selectedProductIds(),
  extraStillChecked: pageCheckboxes[3].checked,
  toasts,
}));
"""
    out = _run_node_script(script)

    assert out["ids"] == [1, 2, 3]
    assert out["extraStillChecked"] is False
    assert out["toasts"] == [
        {"message": "Select at most 3 products at a time.", "kind": "warning"}
    ]


def test_label_printing_hands_over_the_selection_up_to_the_sheet_limit():
    source = _source()
    helpers = "\n".join(_function(source, name) for name in (
        "ensureProductSelection", "printSelectedProductLabels",
    )) + "\nconst BARCODE_SELECTION_LIMIT = 3;"
    script = DOM_STUB + helpers + r"""
const handed = [];
function showBarcodeLabelDialog(prefillIds) { handed.push(prefillIds); }
function requireCapability() { return true; }
const productSelection = new Map([["1", {}], ["2", {}], ["3", {}], ["4", {}], ["5", {}]]);
printSelectedProductLabels();
productSelection.clear();
printSelectedProductLabels();
console.log(JSON.stringify({ handed, toasts }));
"""
    out = _run_node_script(script)

    # The sheet takes at most three products per run; the manager is told so.
    assert out["handed"] == [["1", "2", "3"]]
    assert out["toasts"] == [
        {"message": "A label sheet prints 3 products at a time — the first 3 selected products are loaded.",
         "kind": "warning"},
        {"message": "Select at least one product first.", "kind": "warning"},
    ]


def test_bulk_export_sends_only_the_ticked_ids():
    source = _source()
    helpers = "\n".join(_function(source, name) for name in (
        "selectedProductIds", "ensureProductSelection", "exportSelectedProducts",
    ))
    script = DOM_STUB + helpers + r"""
const downloads = [];
function openReportDownload(url) { downloads.push(url); return url; }
const productSelection = new Map([["4", { id: 4 }], ["9", { id: 9 }]]);
exportSelectedProducts("pdf");
exportSelectedProducts("xlsx");
productSelection.clear();
const refused = exportSelectedProducts("pdf");
console.log(JSON.stringify({ downloads, refused: refused === undefined, toasts }));
"""
    out = _run_node_script(script)

    assert out["downloads"] == [
        "/api/products/export?format=pdf&ids=4%2C9",
        "/api/products/export?format=xlsx&ids=4%2C9",
    ]
    assert out["refused"] is True
    assert out["toasts"] == [{"message": "Select at least one product first.", "kind": "warning"}]


def test_bulk_delete_merges_dependencies_then_deletes_each_product():
    source = _source()
    helpers = "\n".join(_function(source, name) for name in (
        "selectedProductIds", "ensureProductSelection", "confirmDeleteSelectedProducts",
        "deleteSelectedProducts",
    ))
    script = DOM_STUB + helpers + r"""
let currentBranch = { id: 7 };
const requests = [];
const dependencies = {
  4: { success: true, can_delete: true, groups: [
    { key: "sales_history", label: "Sales history lines", count: 2, action: "keep" },
    { key: "promotions", label: "Promotions", count: 1, action: "delete" },
  ] },
  9: { success: true, can_delete: true, groups: [
    { key: "promotions", label: "Promotions", count: 3, action: "delete" },
  ] },
};
const deleteResponses = [{ success: true }, { success: true }];
function fetch(url, options) {
  requests.push({ url, method: options && options.method ? options.method : null });
  if (!options || !options.method) {
    const id = Number(/\/api\/products\/(\d+)\/dependencies/.exec(url)[1]);
    return Promise.resolve({ json: async () => dependencies[id] });
  }
  return Promise.resolve({ json: async () => deleteResponses.shift() });
}
const dialogs = [];
function showProductCleanupDialog(productName, groups, canDelete, onConfirm, options) {
  dialogs.push({ productName, groups, canDelete, onConfirm, options });
}
const state = { cleared: 0, invalidated: 0, reloads: 0, stats: 0 };
function hasManagerOrBossAccess() { return true; }
function clearProductSelection() { state.cleared += 1; }
function invalidateProductsCache() { state.invalidated += 1; }
function loadProducts() { state.reloads += 1; }
function loadDashboardStats() { state.stats += 1; }
const productSelection = new Map([["4", { id: 4 }], ["9", { id: 9 }]]);

(async () => {
  await confirmDeleteSelectedProducts();
  const dialog = dialogs[0];
  const summary = { productName: dialog.productName, canDelete: dialog.canDelete,
    options: dialog.options,
    groups: dialog.groups.map((group) => ({
      key: group.key, count: group.count, action: group.action, label: group.label,
    })) };
  await dialog.onConfirm();
  console.log(JSON.stringify({ summary, requests, state, toasts }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    out = _run_node_script(script)

    assert out["summary"]["productName"] == "2 products"
    assert out["summary"]["canDelete"] is True
    assert out["summary"]["options"]["title"] == "Delete Selected Products"
    assert "Delete 2 selected products" in out["summary"]["options"]["intro"]
    assert out["summary"]["groups"] == [
        {"key": "sales_history", "count": 2, "action": "keep", "label": "Sales history lines"},
        {"key": "promotions", "count": 4, "action": "delete", "label": "Promotions"},
    ]
    assert out["requests"] == [
        {"url": "/api/products/4/dependencies?branch_id=7", "method": None},
        {"url": "/api/products/9/dependencies?branch_id=7", "method": None},
        {"url": "/api/products/4?force=1&cascade=1&branch_id=7", "method": "DELETE"},
        {"url": "/api/products/9?force=1&cascade=1&branch_id=7", "method": "DELETE"},
    ]
    assert out["state"] == {"cleared": 1, "invalidated": 1, "reloads": 1, "stats": 1}
    assert out["toasts"] == [{"message": "2 product(s) deleted.", "kind": "success"}]


def test_import_confirmation_is_a_styled_window_not_a_browser_prompt():
    source = _source()
    variants = re.search(r"const CONFIRM_VARIANTS = \{.*?\};", source, re.S).group(0)
    helpers = "\n".join(_function(source, name) for name in (
        "setConfirmDialogIcon", "resetConfirmDialog", "confirmDialogDetails",
        "renderConfirmStats", "renderConfirmNote", "stackDialogAboveOpenModals",
        "showConfirmDialogAsync", "productImportConfirmDialog",
    ))
    script = DOM_STUB + "let confirmModalCallback = null;\n" + variants + "\n" + helpers + r"""
const pendingHides = [];
let shown = null;
// The Excel import window stays open behind the confirmation dialog.
const importModal = node("importProductsModal");
importModal.classList.add("show");
importModal.style.zIndex = "1055";
const document = {
  getElementById(id) { return node(id); },
  createElement(tag) { return makeNode(tag); },
  querySelectorAll(selector) {
    return selector === ".modal.show" ? [importModal, node("confirmModal")]
      .filter((element) => element.classList.contains("show")) : [];
  },
};
globalThis.getComputedStyle = (element) => ({ zIndex: element.style.zIndex || "1055" });
globalThis.bootstrap = { Modal: { getOrCreateInstance(element) {
  return { show() { shown = element; }, hide() { pendingHides.push(element); } };
} } };

function details() {
  const wrapper = node("confirmModalDetails").children[0];
  return {
    hidden: node("confirmModalDetails").classList.contains("d-none"),
    stats: wrapper.children.map((card) => ({
      value: card.children[0].textContent,
      label: card.children[1].textContent,
      tone: card.children[0].className,
    })),
  };
}

(async () => {
  const answer = productImportConfirmDialog("Import 30 new product(s)?",
    { create: 30, update: 0, skip: 2, error: 0 });
  const opened = {
    shown: shown === node("confirmModal"),
    // Bootstrap paints both windows at 1055; the dialog must be raised or it
    // stays hidden behind the import window and the screen looks frozen.
    zIndex: node("confirmModal").style.zIndex,
    importZIndex: importModal.style.zIndex,
    title: node("confirmModalTitle").textContent,
    message: node("confirmModalMessage").textContent,
    confirmLabel: node("confirmModalBtnText").textContent,
    cancelLabel: node("confirmModalCancelBtn").textContent,
    confirmClass: node("confirmModalBtn").className,
    headerClass: node("confirmModalHeader").className,
    closeClass: node("confirmModalClose").className,
    icon: node("confirmModalIcon").className,
    details: details(),
  };
  // Pressing the confirm button approves the import once the window closes.
  node("confirmModalBtn").emit("click");
  pendingHides.splice(0).forEach((element) => element.emit("hidden.bs.modal"));
  const approved = await answer;

  const cancelled = productImportConfirmDialog("Import 1 new product(s)?",
    { create: 1, update: 0, skip: 0, error: 0 });
  // Dismissing the window (Cancel, Esc, backdrop) must never import.
  node("confirmModal").emit("hidden.bs.modal");
  const declined = await cancelled;

  console.log(JSON.stringify({ opened, approved, declined,
    zIndexAfterClose: node("confirmModal").style.zIndex, toasts }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    out = _run_node_script(script)

    opened = out["opened"]
    assert opened["shown"] is True
    assert opened["importZIndex"] == "1055"
    assert opened["zIndex"] == "1065"
    assert out["zIndexAfterClose"] == ""
    assert opened["title"] == "Confirm product import"
    assert opened["message"] == "Import 30 products into the active branch?"
    assert opened["confirmLabel"] == "Import 30 products"
    assert opened["cancelLabel"] == "Keep reviewing"
    assert "btn-success" in opened["confirmClass"]
    assert "bg-success" in opened["headerClass"]
    assert opened["closeClass"] == "btn-close btn-close-white"
    assert "bi-upload" in opened["icon"]
    assert opened["details"]["hidden"] is False
    assert opened["details"]["stats"] == [
        {"value": "30", "label": "New", "tone": "confirm-stat-value text-success"},
        {"value": "0", "label": "Updates", "tone": "confirm-stat-value text-primary"},
        {"value": "2", "label": "Skipped", "tone": "confirm-stat-value text-secondary"},
        {"value": "0", "label": "Errors", "tone": "confirm-stat-value text-danger"},
    ]
    assert out["approved"] is True
    assert out["declined"] is False
    assert out["toasts"] == []
