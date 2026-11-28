/* Live-only accounting workspace. No financial payloads or drafts enter browser storage. */
(() => {
  "use strict";
  const root = document.getElementById("accounting-section");
  if (!root) return;
  const el = name => document.getElementById(`accounting-${name}`);
  const API = "/api/accounting";
  const tabs = [...root.querySelectorAll("[data-accounting-tab]")];
  const state = {identity: null, epoch: 0, load: 0, list: 0, detail: 0, status: null,
    opening: null, tab: "statements", page: 1, live: false, loading: false, pending: false,
    editor: null, setup: null, items: [], serial: 0};
  const identity = () => typeof inventoryIdentity === "function" ? inventoryIdentity() : null;
  const allowed = () => typeof hasCapability === "function" && hasCapability("section.accounting") && hasCapability("admin");
  const online = () => navigator.onLine !== false &&
    !(typeof isBrowserOffline === "function" && isBrowserOffline()) &&
    !(typeof checkoutServerUnavailable !== "undefined" && checkoutServerUnavailable);
  const usable = () => allowed() && identity() === state.identity &&
    !(typeof dashboardSessionStale !== "undefined" && dashboardSessionStale) &&
    !(typeof dashboardBranchStale !== "undefined" && dashboardBranchStale) &&
    !(typeof branchSwitchInFlight !== "undefined" && branchSwitchInFlight);
  const visible = () => root.style.display !== "none" && !root.hidden;
  const today = () => {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  };
  function node(tag, text, className) {
    const result = document.createElement(tag);
    if (text != null) result.textContent = String(text);
    if (className) result.className = className;
    return result;
  }
  function button(text, callback, className = "btn btn-outline-secondary") {
    const result = node("button", text, className);
    result.type = "button";
    result.addEventListener("click", callback);
    return result;
  }
  function message(target, text) {
    target.textContent = text || "";
    target.hidden = !text;
  }
  function error(text, target = el("error")) {
    message(target, text);
    if (text && visible()) target.focus({preventScroll: true});
  }
  function id(value, label = "ID") {
    const n = Number(value);
    if (!/^\d+$/.test(String(value)) || !Number.isSafeInteger(n) || n <= 0) throw new Error(`${label} must be a positive numeric ID.`);
    return n;
  }
  function cents(value, label = "Amount") {
    const text = String(value ?? "").trim();
    if (!/^\d+(\.\d{1,2})?$/.test(text)) throw new Error(`${label} must be a non-negative amount with at most two decimals.`);
    const [whole, fraction = ""] = text.split(".");
    return BigInt(whole) * 100n + BigInt(fraction.padEnd(2, "0"));
  }
  function fromCents(value) {
    const sign = value < 0n ? "-" : "";
    const n = value < 0n ? -value : value;
    return `${sign}${n / 100n}.${String(n % 100n).padStart(2, "0")}`;
  }
  function money(value, positive = false, label = "Amount") {
    const n = cents(value, label);
    if (positive && n === 0n) throw new Error(`${label} must be greater than zero.`);
    return fromCents(n);
  }
  function displayMoney(value) {
    const text = String(value ?? "0");
    try { return (text.startsWith("-") ? "-" : "") + money(text.replace(/^-/, "")); }
    catch (_) { return text; }
  }
  function date(value, label = "Date") {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || Number.isNaN(Date.parse(`${value}T12:00:00Z`)) ||
        new Date(`${value}T12:00:00Z`).toISOString().slice(0, 10) !== value) throw new Error(`${label} must be a valid YYYY-MM-DD date.`);
    return value;
  }
  function postingDate(value) {
    date(value);
    if (state.status?.start_date && value < state.status.start_date) throw new Error("Posting date cannot precede the accounting start date.");
    if (state.status?.locked_through && value <= state.status.locked_through) throw new Error(`Posting is locked through ${state.status.locked_through}. Choose a later date.`);
    return value;
  }
  const locations = () => state.status?.locations || [];
  const locationName = value => locations().find(item => String(item.id) === String(value))?.name || `Location ${value}`;
  const central = item => item && (item.branch_id == null || ["central", "warehouse"].includes(item.kind));
  const selectedLocation = () => locations().find(item => String(item.id) === el("location").value);
  function writeLocation(branchOnly = false, centralOnly = false) {
    const item = selectedLocation();
    if (!item) throw new Error("Choose one accounting location. All locations is for reports only.");
    if (branchOnly && central(item)) throw new Error("Inventory count corrections are available for shop stock only.");
    if (centralOnly && !central(item)) throw new Error("Supplier bills and purchase returns belong to the central warehouse. Select that location first.");
    return id(item.id, "Location");
  }
  function query(values) {
    const params = new URLSearchParams();
    Object.entries(values).forEach(([key, value]) => { if (value !== "" && value != null) params.set(key, String(value)); });
    return params.toString();
  }
  function token() { return {identity: identity(), epoch: state.epoch}; }
  function current(t) { return t.identity === identity() && t.identity === state.identity && t.epoch === state.epoch && usable(); }
  async function request(path, body) {
    if (!online()) throw new Error("Accounting requires a live connection. Reconnect, refresh and retry; nothing is queued offline.");
    if (!usable()) throw new Error("Account or branch changed. Reload the dashboard before continuing.");
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      // The dashboard fetch wrapper adds the rendered account and branch headers.
      const response = await fetch(path, {method: body ? "POST" : "GET", cache: "no-store",
        credentials: "same-origin", signal: controller.signal,
        headers: body ? {"Content-Type": "application/json", "Accept": "application/json"} : {"Accept": "application/json"},
        ...(body ? {body: JSON.stringify(body)} : {})});
      const data = await response.json().catch(() => null);
      if (!response.ok || data?.success === false) {
        const failure = new Error(data?.message || data?.error || `Request failed (HTTP ${response.status}).`);
        // A timeout, transport failure, server failure or unreadable success can
        // follow a committed transaction. Never mint a replacement key for it.
        failure.definite = response.status >= 400 && response.status < 500 && ![408, 409, 425, 429].includes(response.status);
        throw failure;
      }
      if (!data || response.redirected) throw new Error("The server did not return accounting JSON. Check your sign-in and connection.");
      return data;
    } catch (failure) {
      if (failure.name === "AbortError") throw new Error("The request timed out. Its outcome has not been confirmed.");
      throw failure;
    } finally { clearTimeout(timeout); }
  }
  function options(select, choices, placeholder) {
    select.replaceChildren();
    if (placeholder != null) select.append(new Option(placeholder, ""));
    choices.forEach(([value, label]) => select.append(new Option(String(label), String(value))));
  }
  function field(parent, name, label, type = "text", value = "", choices = null, required = true) {
    const wrap = node("div", null, "col-12 col-sm-6 col-lg-4");
    const input = node(choices ? "select" : "input", null, choices ? "form-select" : "form-control");
    input.id = `accounting-field-${++state.serial}`;
    input.name = name;
    input.required = required;
    if (choices) options(input, choices, "Choose…");
    else input.type = type;
    if (type === "number") { input.step = "1"; input.min = "1"; }
    if (type === "money") {
      input.type = "text";
      input.inputMode = "decimal";
      input.pattern = "[0-9]+([.][0-9]{1,2})?";
      input.maxLength = 22;
    }
    input.value = value;
    const caption = node("label", `${label}${required ? " *" : ""}`, "form-label");
    caption.htmlFor = input.id;
    wrap.append(caption, input);
    parent.append(wrap);
    return input;
  }
  function check(parent, label) {
    const wrap = node("div", null, "form-check my-3");
    const input = node("input", null, "form-check-input");
    input.type = "checkbox"; input.required = true; input.id = `accounting-field-${++state.serial}`;
    const caption = node("label", label, "form-check-label"); caption.htmlFor = input.id;
    wrap.append(input, caption); parent.append(wrap);
    return input;
  }
  function table(title, columns, items) {
    const wrap = node("div", null, "table-responsive");
    wrap.tabIndex = 0; wrap.setAttribute("role", "region"); wrap.setAttribute("aria-label", title);
    const grid = node("table", null, "table table-hover align-middle mb-0");
    grid.append(node("caption", title, "caption-top"));
    const head = node("thead"), header = node("tr"), body = node("tbody");
    columns.forEach(([label, , numeric]) => {
      const th = node("th", label, numeric ? "accounting-money" : ""); th.scope = "col"; header.append(th);
    });
    head.append(header);
    items.forEach(item => {
      const tr = node("tr");
      columns.forEach(([, render, numeric]) => {
        const td = node("td", null, numeric ? "accounting-money" : "text-break");
        const content = render(item);
        if (content instanceof Node) td.append(content); else td.textContent = String(content ?? "—");
        tr.append(td);
      });
      body.append(tr);
    });
    if (!items.length) {
      const tr = node("tr"), td = node("td", "No entries for this scope and filters.", "text-muted py-4 text-center");
      td.colSpan = columns.length; tr.append(td); body.append(tr);
    }
    grid.append(head, body); wrap.append(grid); return wrap;
  }
  function clearResults() {
    ++state.list; ++state.detail;
    state.items = [];
    el("results").replaceChildren(); el("pagination").replaceChildren(); el("pagination").hidden = true;
    el("document").replaceChildren(); el("document").hidden = true;
    root.querySelectorAll("a[data-accounting-export]").forEach(link => link.remove());
  }
  function syncControls() {
    const blocked = state.pending || state.loading || !online() || !usable();
    tabs.forEach(tab => { tab.disabled = blocked || Boolean(state.editor?.uncertain); });
    el("location").disabled = blocked || Boolean(state.editor);
    el("refresh").disabled = state.pending || state.loading || Boolean(state.editor?.uncertain || state.setup?.uncertain);
    el("filter-form").querySelectorAll("input,select,button").forEach(input => { input.disabled = blocked; });
    el("toolbar").querySelectorAll("button").forEach(input => { input.disabled = blocked || !state.live; });
    el("setup-fields").disabled = blocked || !state.opening || Boolean(state.setup?.uncertain);
    el("activate").disabled = blocked || (!state.opening && !state.setup?.uncertain);
    if (state.editor) {
      state.editor.fields.disabled = blocked || !state.live || state.editor.uncertain;
      state.editor.submit.disabled = blocked || !state.live;
      state.editor.cancel.disabled = state.pending || state.editor.uncertain;
    }
    root.querySelectorAll("[data-accounting-action]").forEach(input => { input.disabled = blocked || !state.live; });
  }
  function acknowledgeDraft() {
    if (!state.editor) return true;
    error("Save or discard the open form before changing this view. An unconfirmed request must be retried with the same key.", state.editor.error);
    return false;
  }
  function accountOptions(expense = false, opening = false) {
    return (state.status?.accounts || []).filter(account => !account.control &&
      (!expense || account.type === "expense") &&
      (!opening || ["CASH", "BANK", "TAX_PAYABLE", "CAPITAL"].includes(account.code)))
      .map(account => [account.code, `${account.code} — ${account.name}`]);
  }
  const cashOptions = [["CASH", "Cash"], ["BANK", "Bank"]];
  const locationOptions = () => locations().map(item => [item.id, item.name]);
  const openingLocations = () => state.opening ? [
    ...state.opening.locations,
    {branch_id: null, name: "Central warehouse / head office"}
  ] : [];

  // Bounded, explicit lookup. Supplier IDs remain enterable for off-page results.
  function supplierFields(parent, prefix = "supplier") {
    const box = node("div", null, "col-12 border rounded p-3");
    const row = node("div", null, "row g-3");
    const supplierId = field(row, `${prefix}_id`, "Supplier ID", "number");
    const search = field(row, `${prefix}_search`, "Find supplier by name", "search", "", null, false);
    const select = field(row, `${prefix}_choice`, "Matching suppliers", "text", "", [], false);
    const status = node("p", "Search returns up to 30 suppliers. Refine the name to find others.", "small text-muted mt-2 mb-0");
    status.setAttribute("role", "status");
    let sequence = 0;
    const lookup = button("Find suppliers", async () => {
      const t = token(), seq = ++sequence, term = search.value.trim();
      lookup.disabled = true; status.textContent = "Finding suppliers…";
      try {
        const data = await request(`/api/suppliers?${query({view: "lookup", limit: 30, q: term})}`);
        if (!current(t) || seq !== sequence || !box.isConnected || search.value.trim() !== term) return;
        if (!Array.isArray(data)) throw new Error("Supplier lookup must return an array.");
        options(select, data.slice(0, 30).map(item => [item.id, `${item.name} (#${item.id})`]), "Choose a supplier to fill the ID");
        status.textContent = `${Math.min(data.length, 30)} matches. Supplier ID can also be entered directly.`;
        status.setAttribute("role", "status");
      } catch (failure) {
        if (current(t) && seq === sequence && box.isConnected) { status.textContent = failure.message; status.setAttribute("role", "alert"); }
      } finally { if (seq === sequence) lookup.disabled = false; }
    });
    lookup.classList.add("mt-3");
    search.addEventListener("input", () => { sequence++; lookup.disabled = false; options(select, [], "Search again"); });
    search.addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); lookup.click(); } });
    select.addEventListener("change", () => { if (select.value) { supplierId.value = select.value; supplierId.dispatchEvent(new Event("input", {bubbles: true})); } });
    box.append(row, lookup, status); parent.append(box); return supplierId;
  }

  function removableLine(container, title) {
    const fields = node("fieldset", null, "border rounded p-3 mb-3");
    fields.append(node("legend", title, "float-none w-auto px-2 h6"));
    const row = node("div", null, "row g-3 align-items-end");
    const remove = button(`Remove ${title.toLowerCase()}`, () => { fields.remove(); container.dispatchEvent(new Event("input", {bubbles: true})); }, "btn btn-outline-secondary mt-2");
    fields.append(row, remove); container.append(fields); return {fields, row};
  }
  function addOpeningLine() {
    const {fields, row} = removableLine(el("opening-lines"), "Opening balance");
    field(row, "location", "Location", "text", openingLocations()[0]?.branch_id || "",
      openingLocations().map(item => [item.branch_id, item.name]));
    field(row, "account", "Account", "text", "", accountOptions(false, true));
    field(row, "debit", "Debit", "money", "0.00"); field(row, "credit", "Credit", "money", "0.00");
    fields.dataset.openingLine = "true"; updateEquity();
  }
  function addOpeningPayable() {
    const {fields, row} = removableLine(el("opening-payables"), "Supplier balance");
    supplierFields(row);
    field(row, "reference", "Reference"); field(row, "amount", "Amount owed", "money");
    field(row, "due_date", "Due date", "date", "", null, false);
    fields.dataset.openingPayable = "true"; updateEquity();
  }
  const value = (parent, name) => parent.querySelector(`[name="${name}"]`).value.trim();
  function readOpening() {
    return [...el("opening-lines").children].map(row => {
      const loc = openingLocations().find(item => String(item.branch_id) === value(row, "location"));
      if (!loc) throw new Error("Select a valid location for each opening balance.");
      const debit = money(value(row, "debit")), credit = money(value(row, "credit"));
      if ((debit !== "0.00") === (credit !== "0.00")) throw new Error("Each opening row needs either a debit or a credit, not both.");
      return {branch_id: loc.branch_id == null ? null : id(loc.branch_id, "Branch"),
        account_code: value(row, "account"), debit, credit};
    });
  }
  function updateEquity() {
    if (!state.opening) { el("opening-equity").textContent = "Refresh the live opening review to calculate opening equity."; return; }
    try {
      const balances = new Map();
      const add = (branch, name, amount) => {
        const key = branch == null ? "central" : String(branch);
        const old = balances.get(key) || {name, total: 0n}; old.total += amount; balances.set(key, old);
      };
      (state.opening.locations || []).forEach(loc => add(loc.branch_id, loc.name,
        cents(loc.stock_value || 0) + cents(loc.receivables || 0)));
      add(null, "Central warehouse", cents(state.opening.warehouse?.value || 0) - cents(state.opening.unbilled_receipts || 0));
      [...el("opening-lines").children].forEach(row => {
        const loc = openingLocations().find(item => String(item.branch_id) === value(row, "location"));
        if (loc) add(loc.branch_id, loc.name, cents(value(row, "debit") || "0") - cents(value(row, "credit") || "0"));
      });
      [...el("opening-payables").children].forEach(row => add(null, "Central warehouse", -cents(value(row, "amount") || "0")));
      const total = [...balances.values()].reduce((sum, loc) => sum + loc.total, 0n);
      el("opening-equity").textContent = `Computed opening equity: ${fromCents(total)} ${state.opening.currency || state.status.currency}. ` +
        [...balances.values()].map(loc => `${loc.name}: ${fromCents(loc.total)}`).join("; ") + ". Positive equity is a credit; negative equity is a debit. The server calculates the final balancing entries.";
    } catch (_) { el("opening-equity").textContent = "Enter valid two-decimal opening amounts to preview net assets and opening equity."; }
  }
  function renderOpening(data) {
    el("opening-summary").replaceChildren(table(`Opening review — ${data.date} · ${data.currency}`, [
      ["Location", row => row.name], ["Stock quantity", row => row.stock_quantity, true],
      ["Stock value", row => displayMoney(row.stock_value), true], ["Receivables", row => displayMoney(row.receivables), true],
    ], [...(data.locations || []), {name: "Central warehouse", stock_quantity: data.warehouse?.quantity,
      stock_value: data.warehouse?.value, receivables: "0.00"}]));
    const warnings = el("opening-warnings"); warnings.replaceChildren();
    (data.warnings || []).forEach(item => warnings.append(node("p", typeof item === "string" ? item : JSON.stringify(item))));
    if (data.unvalued_stock?.length) {
      warnings.append(node("p", "Unvalued stock needs cost review before activation:", "fw-semibold"));
      const list = node("ul", null, "mb-0");
      data.unvalued_stock.forEach(item => list.append(node("li", `${item.name} (#${item.product_id}) — ${item.quantity} at ${typeof item.location === "object" ? JSON.stringify(item.location) : item.location}`)));
      warnings.append(list);
    }
    warnings.hidden = !warnings.childNodes.length; updateEquity();
  }

  async function saveOperation(op, build, path, success) {
    if (state.pending) return;
    if (!usable() || op.identity !== identity()) { error("Account or branch changed. Reload before posting.", op.error); return; }
    if (!online() || !state.live) { error("Reconnect and refresh live accounting data before posting. Nothing is queued offline.", op.error); return; }
    let payload;
    try {
      if (op.uncertain) payload = op.payload;
      else {
        if (!op.form.reportValidity()) return;
        payload = build();
        if (!op.key) {
          if (!globalThis.crypto?.randomUUID) throw new Error("Secure UUID generation is unavailable. Use HTTPS or localhost before posting.");
          op.key = crypto.randomUUID();
        }
        payload = {...payload, key: op.key};
        op.payload = payload;
      }
    } catch (failure) { error(failure.message, op.error); return; }
    state.pending = true; op.form.setAttribute("aria-busy", "true");
    message(op.error, ""); op.progress.textContent = "Posting to the live ledger…"; syncControls();
    const requestIdentity = identity();
    try {
      await request(API + path, payload);
      if (requestIdentity !== identity() || !usable()) return;
      op.key = null; op.payload = null; op.uncertain = false;
      op.progress.textContent = "Saved successfully.";
      success();
      message(el("notice"), "Saved to the ledger. Refreshing live balances…");
    } catch (failure) {
      if (requestIdentity !== identity() || !usable()) return;
      op.uncertain = !failure.definite;
      op.progress.textContent = op.uncertain ? "Outcome unconfirmed. The original values and request key are retained; use Retry to safely confirm this same operation. Do not re-enter it elsewhere." : "Not saved. Correct the form and retry.";
      op.submit.textContent = op.uncertain ? "Retry the same request" : op.submitLabel;
      error(failure.message, op.error);
    } finally {
      state.pending = false; op.form.removeAttribute("aria-busy"); syncControls();
    }
    if (!op.key && usable() && visible()) await loadAccounting();
  }
  function closeEditor() {
    if (state.pending || state.editor?.uncertain) return;
    state.editor = null; el("editor").replaceChildren(); el("editor").hidden = true; syncControls();
  }
  function openEditor(title, help, path, buildFields, buildPayload, {centralOnly = false, branchOnly = false} = {}) {
    if (!acknowledgeDraft()) return;
    try { writeLocation(branchOnly, centralOnly); } catch (failure) { error(failure.message); return; }
    if (!state.live || !usable() || !online()) { error("Refresh live accounting data before opening a form."); return; }
    const host = el("editor"); host.replaceChildren(); host.hidden = false;
    const body = node("div", null, "card-body");
    const heading = node("h3", title, "h5"); heading.tabIndex = -1;
    body.append(heading, node("p", `${help} Location: ${locationName(el("location").value)}. Required fields are marked *.`, "text-muted"));
    const form = node("form"); form.autocomplete = "off";
    const fields = node("fieldset"); fields.append(node("legend", title, "visually-hidden"));
    const row = node("div", null, "row g-3"); fields.append(row);
    const controls = buildFields(row, fields);
    const alert = node("div", null, "alert alert-danger mt-3"); alert.hidden = true; alert.tabIndex = -1; alert.setAttribute("role", "alert");
    const progress = node("p", "", "mt-3"); progress.setAttribute("role", "status"); progress.setAttribute("aria-live", "polite");
    const actions = node("div", null, "d-flex flex-wrap gap-2 mt-3");
    const submit = node("button", "Post to ledger", "btn btn-primary"); submit.type = "submit";
    const cancel = button("Discard form", closeEditor);
    actions.append(submit, cancel); form.append(fields, alert, progress, actions); body.append(form); host.append(body);
    const op = {form, fields, error: alert, progress, submit, cancel, identity: identity(),
      location: writeLocation(branchOnly, centralOnly), key: null, uncertain: false, submitLabel: "Post to ledger", dirty: false};
    state.editor = op;
    form.addEventListener("input", () => { op.dirty = true; });
    form.addEventListener("submit", event => {
      event.preventDefault();
      saveOperation(op, () => buildPayload(controls, op), path, closeEditorAfterSuccess);
    });
    heading.focus(); syncControls();
  }
  function closeEditorAfterSuccess() {
    state.editor = null; el("editor").replaceChildren(); el("editor").hidden = true;
  }
  function dated(row) { return field(row, "date", "Posting date", "date", today()); }
  function payment(kind, item) {
    const ar = kind === "receivables";
    openEditor(ar ? "Receive customer payment" : "Pay supplier", `${ar ? item.customer_name : item.supplier_name} · ${item.reference || item.sale_id || item.id} · Outstanding ${displayMoney(item.balance)}. This settles the existing record, not a duplicate debt.`,
      `/${kind}/${id(item.id)}/pay`, row => ({date: dated(row), amount: field(row, "amount", "Payment amount", "money", displayMoney(item.balance)),
        account: field(row, "account", ar ? "Receive into" : "Pay from", "text", "CASH", cashOptions)}), controls => {
        const amount = money(controls.amount.value, true);
        if (cents(amount) > cents(item.balance)) throw new Error("Payment exceeds the displayed outstanding balance. Refresh if it changed.");
        return {date: postingDate(controls.date.value), amount, [ar ? "paid_to" : "paid_from"]: controls.account.value};
      }, {centralOnly: !ar});
  }
  function newExpense() {
    openEditor("Record expense", "Record a paid operating expense. Supplier credit bills belong in Payables.", "/expenses", row => ({
      date: dated(row), account: field(row, "account", "Expense account", "text", "", accountOptions(true)),
      amount: field(row, "amount", "Amount", "money"), paid: field(row, "paid", "Pay from", "text", "CASH", cashOptions),
      payee: field(row, "payee", "Payee"), description: field(row, "description", "Description"),
    }), (c, op) => ({date: postingDate(c.date.value), location_id: op.location, account_code: c.account.value,
      amount: money(c.amount.value, true), paid_from: c.paid.value, payee: c.payee.value.trim(), description: c.description.value.trim()}));
  }
  function newAsset() {
    openEditor("Register fixed asset", "Register a purchase or a reviewed opening asset. Opening asset uses opening equity instead of cash/bank. Depreciation is posted manually after review, not on an automatic schedule.", "/assets", row => ({
      date: dated(row), name: field(row, "name", "Asset name"), category: field(row, "category", "Category"),
      cost: field(row, "cost", "Cost", "money"), salvage: field(row, "salvage", "Salvage value", "money", "0.00"),
      life: field(row, "life", "Useful life (months)", "number"),
      paid: field(row, "paid", "Funding", "text", "CASH", [...cashOptions, ["OPENING_EQUITY", "Opening asset — opening equity (no cash payment)"]]),
    }), (c, op) => {
      const cost = money(c.cost.value, true), salvage = money(c.salvage.value);
      if (cents(salvage) > cents(cost)) throw new Error("Salvage value cannot exceed cost.");
      return {date: postingDate(c.date.value), location_id: op.location, name: c.name.value.trim(), category: c.category.value.trim(),
        cost, salvage_value: salvage, life_months: id(c.life.value, "Useful life"), paid_from: c.paid.value};
    });
  }
  function assetAction(item, dispose) {
    openEditor(dispose ? "Dispose of asset" : "Post reviewed depreciation", `${item.name} · Carrying value ${displayMoney(item.carrying_value)}. ${dispose ? "Disposal removes the asset and records proceeds and gain/loss. It does not delete its history." : "Review the amount and period yourself; no automatic depreciation schedule runs."}`,
      `/assets/${id(item.id)}/${dispose ? "dispose" : "depreciate"}`, (row, fields) => {
        const controls = {date: dated(row), amount: field(row, "amount", dispose ? "Disposal proceeds (zero allowed)" : "Reviewed depreciation amount", "money", dispose ? "0.00" : "")};
        if (dispose) controls.paid = field(row, "paid", "Receive proceeds into", "text", "CASH", cashOptions);
        controls.confirm = check(fields, dispose ? "I confirm disposal and its asset, cash and gain/loss impact." : "I reviewed this depreciation amount and confirm posting it once for this period.");
        return controls;
      }, c => ({date: postingDate(c.date.value), ...(dispose ? {proceeds: money(c.amount.value), paid_to: c.paid.value} : {amount: money(c.amount.value, true)})}));
  }
  function newBill() {
    openEditor("Add non-stock supplier bill", "Central warehouse only. Stock purchase invoices are posted automatically from Purchases: do not duplicate them here. This form records additional NON-stock expenses owed to a supplier.", "/payables", row => ({
      date: dated(row), supplier: supplierFields(row), reference: field(row, "reference", "Supplier reference"),
      due: field(row, "due", "Due date", "date", "", null, false), amount: field(row, "amount", "Amount", "money"),
      account: field(row, "account", "Non-stock expense account", "text", "", accountOptions(true)),
    }), c => ({date: postingDate(c.date.value), supplier_id: id(c.supplier.value, "Supplier"), reference: c.reference.value.trim(),
      ...(c.due.value ? {due_date: date(c.due.value)} : {}), amount: money(c.amount.value, true), expense_account: c.account.value}), {centralOnly: true});
  }
  function newJournal() {
    openEditor("Post manual journal", "Control accounts are excluded. Every location must balance separately; a debit in one shop cannot balance a credit in another.", "/journals", (row, fields) => {
      const controls = {date: dated(row), memo: field(row, "memo", "Memo / reason")};
      const lines = node("div", null, "accounting-lines mt-3"), totals = node("p", "", "fw-semibold mb-0");
      totals.setAttribute("role", "status"); totals.setAttribute("aria-live", "polite");
      const update = () => {
        try {
          const byLocation = new Map(); let debit = 0n, credit = 0n;
          [...lines.children].forEach(line => {
            const d = cents(value(line, "debit") || "0"), c = cents(value(line, "credit") || "0");
            debit += d; credit += c;
            const loc = value(line, "location"); byLocation.set(loc, (byLocation.get(loc) || 0n) + d - c);
          });
          totals.textContent = `Debit ${fromCents(debit)} · Credit ${fromCents(credit)} · Difference ${fromCents(debit - credit)}. ` +
            [...byLocation].map(([loc, amount]) => `${locationName(loc)} difference: ${fromCents(amount)}`).join("; ");
        } catch (_) { totals.textContent = "Enter valid two-decimal amounts to calculate journal totals."; }
      };
      const add = () => {
        const {row: line} = removableLine(lines, "Journal line");
        field(line, "location", "Location", "text", el("location").value, locationOptions());
        field(line, "account", "Account (non-control)", "text", "", accountOptions());
        field(line, "debit", "Debit", "money", "0.00"); field(line, "credit", "Credit", "money", "0.00"); update();
      };
      const lineActions = node("div", null, "d-flex flex-wrap gap-2 mb-3");
      lineActions.append(button("Add journal line", add));
      fields.append(lines, lineActions, totals); lines.addEventListener("input", update); add(); add();
      controls.lines = lines; return controls;
    }, c => {
      const sums = new Map();
      const lines = [...c.lines.children].map(line => {
        const location = id(value(line, "location"), "Location"), debit = money(value(line, "debit")), credit = money(value(line, "credit"));
        if ((debit !== "0.00") === (credit !== "0.00")) throw new Error("Every journal line needs either a debit or a credit, not both.");
        sums.set(location, (sums.get(location) || 0n) + cents(debit) - cents(credit));
        return {location_id: location, account_code: value(line, "account"), debit, credit};
      });
      if (lines.length < 2 || [...sums.values()].some(sum => sum !== 0n)) throw new Error("The journal must contain at least two lines and balance separately at every location.");
      return {date: postingDate(c.date.value), memo: c.memo.value.trim(), lines};
    });
  }
  function reverseJournal(item) {
    openEditor("Reverse journal", `Journal #${item.id}: ${item.memo || item.source_type}. This creates an equal and opposite journal; it does not delete the original. Only manual and expense journals are reversible here.`,
      `/journals/${id(item.id)}/reverse`, (row, fields) => {
        const controls = {date: dated(row)};
        fields.append(node("p", "Reason: correct or cancel this manual/expense posting. Check its cash, expense and location impact before proceeding.", "mt-3"));
        check(fields, "I reviewed the reason and impact and confirm an equal and opposite posting."); return controls;
      }, c => ({date: postingDate(c.date.value)}));
  }
  function adjustInventory() {
    openEditor("Correct shop inventory count", "Shop stock only. The correction updates stock and its accounting journal together. Negative quantities remove remaining FIFO stock; positive quantities require the reviewed incoming unit cost. Catalog costs are not edited here.", "/inventory/adjust", (row, fields) => {
      const c = {date: dated(row), product: field(row, "product", "Product ID", "number"), quantity: field(row, "quantity", "Quantity change (+add / −remove)", "number"),
        cost: field(row, "cost", "Unit cost (additions only)", "money"), reason: field(row, "reason", "Count correction reason")};
      c.quantity.removeAttribute("min");
      const update = () => { c.cost.required = Number(c.quantity.value) > 0; c.cost.disabled = Number(c.quantity.value) <= 0; };
      c.quantity.addEventListener("input", update); update();
      check(fields, "I reviewed the physical count and confirm the stock and journal correction."); return c;
    }, (c, op) => {
      const quantity = Number(c.quantity.value);
      if (!Number.isSafeInteger(quantity) || quantity === 0) throw new Error("Quantity change must be a non-zero signed whole number.");
      return {date: postingDate(c.date.value), location_id: op.location, product_id: id(c.product.value, "Product"), quantity,
        ...(quantity > 0 ? {unit_cost: money(c.cost.value)} : {}), reason: c.reason.value.trim()};
    }, {branchOnly: true});
  }
  function purchaseReturn(payable = null) {
    const choices = state.items.map(item => [item.id, `#${item.id} · ${item.supplier_name} · ${item.reference}`]);
    openEditor("Return purchase stock", "Return only remaining central warehouse batches matched to the selected purchase invoice. Sold or transferred stock cannot be returned here. The server verifies invoice/batch ownership and remaining quantity. Unpaid value reduces the supplier bill; any excess requires an explicit cash or bank refund.", "/purchase-returns", (row, fields) => {
      const c = {date: dated(row), payable: field(row, "payable", "Purchase payable ID", "number", payable?.id || ""),
        choice: field(row, "payable_choice", "Payables on this page", "text", payable?.id || "", choices, false), reason: field(row, "reason", "Return reason"),
        paid: field(row, "paid_to", "Refund account (if return exceeds unpaid balance)", "text", "CASH", cashOptions)};
      c.choice.addEventListener("change", () => { if (c.choice.value) c.payable.value = c.choice.value; });
      const searchRow = node("div", null, "row g-3 my-2");
      const search = field(searchRow, "batch_search", "Find warehouse batches by product", "search", "", null, false);
      const batchChoice = field(searchRow, "batch_choice", "Remaining warehouse batch", "text", "", [], false);
      const status = node("p", "Search for remaining batches; results are not pre-matched to an invoice. Check the batch against the original purchase.", "small text-muted");
      status.setAttribute("role", "status");
      const lines = node("div", null, "accounting-lines"); let sequence = 0, cursor = null, next = null;
      const add = (batch = "") => {
        const {row: line} = removableLine(lines, "Return batch");
        field(line, "warehouse_id", "Warehouse batch ID", "number", batch);
        field(line, "quantity", "Quantity to return", "number", "1");
      };
      const lookup = async (more = false) => {
        const t = token(), seq = ++sequence, term = search.value.trim();
        if (!more) cursor = null;
        find.disabled = true; moreButton.disabled = true; status.textContent = "Loading warehouse batches…";
        try {
          const data = await request(`/api/warehouse?${query({view: "register", per_page: 30, limit: 30, q: term, cursor})}`);
          if (!current(t) || seq !== sequence || !fields.isConnected || term !== search.value.trim()) return;
          if (!Array.isArray(data.items)) throw new Error("Warehouse register must return items.");
          options(batchChoice, data.items.slice(0, 30).filter(item => Number(item.quantity) > 0).map(item => [item.id,
            `#${item.id} · ${item.product_name} · batch ${item.batch_number || "—"} · remaining ${item.quantity}`]), "Choose a remaining batch");
          next = data.has_more ? data.next_cursor : null;
          status.textContent = "Up to 30 batches shown. Add a selected batch, search by product, or use Next batches. Only matched purchase stock is accepted.";
          status.setAttribute("role", "status");
        } catch (failure) {
          if (current(t) && seq === sequence) { status.textContent = failure.message; status.setAttribute("role", "alert"); }
        } finally { if (seq === sequence) { find.disabled = false; moreButton.disabled = !next; } }
      };
      const find = button("Find batches", () => lookup());
      const moreButton = button("Next batches", () => { cursor = next; lookup(true); }); moreButton.disabled = true;
      search.addEventListener("input", () => { sequence++; next = null; find.disabled = false; moreButton.disabled = true; options(batchChoice, [], "Search again"); });
      search.addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); find.click(); } });
      const tools = node("div", null, "d-flex flex-wrap gap-2 mt-2 mb-3"); tools.append(find, moreButton,
        button("Add selected batch", () => { if (batchChoice.value) add(batchChoice.value); else { status.textContent = "Choose a batch first."; status.setAttribute("role", "alert"); } }),
        button("Add batch ID manually", () => add()));
      fields.append(searchRow, status, tools, lines);
      check(fields, "I verified every batch belongs to this purchase invoice and remains in central warehouse stock.");
      add(); c.lines = lines; return c;
    }, c => {
      const seen = new Set();
      const items = [...c.lines.children].map(line => {
        const batch = id(value(line, "warehouse_id"), "Warehouse batch");
        if (seen.has(batch)) throw new Error("Combine duplicate batch IDs into one return row."); seen.add(batch);
        return {warehouse_inventory_id: batch, quantity: id(value(line, "quantity"), "Return quantity")};
      });
      if (!items.length) throw new Error("Add at least one matched warehouse batch.");
      return {date: postingDate(c.date.value), payable_id: id(c.payable.value, "Payable"), items, reason: c.reason.value.trim(), paid_to: c.paid.value};
    }, {centralOnly: true});
  }
  function periodLock() {
    openEditor("Lock accounting period", "This applies to the entire ledger, not just the selected location. Posting on or before the chosen date will be blocked. This is irreversible in this workspace: no unlock is supported.", "/period-lock", (row, fields) => {
      const through = field(row, "through", "Lock through (inclusive)", "date", today());
      check(fields, "I reviewed and exported the statements and confirm this irreversible ledger-wide lock. There is no unlock."); return {through};
    }, c => {
      const through = date(c.through.value);
      if (state.status.locked_through && through <= state.status.locked_through) throw new Error("Choose a date after the existing lock.");
      return {through};
    });
  }

  function navigationURL(path) {
    if (typeof buildIdentityNavigationURL === "function") return buildIdentityNavigationURL(path);
    if (typeof dashboardIdentityUrl === "function") return dashboardIdentityUrl(path);
    return null;
  }
  function exportLinks(parent, path, filters, t = token()) {
    ["pdf", "xlsx"].forEach(format => {
      const route = `${API}${path}?${query({...filters, format})}`;
      const link = node("a", format === "pdf" ? "PDF / Print" : "Excel (XLSX)", "btn btn-outline-secondary");
      link.dataset.accountingExport = "true";
      const href = navigationURL(route);
      if (!href) { link.setAttribute("aria-disabled", "true"); return; }
      link.href = href; // Same-tab navigation, always bound to the rendered identity.
      link.addEventListener("click", event => {
        const fresh = current(t) && online() && state.live && !state.pending;
        const bound = fresh ? navigationURL(route) : null;
        if (!bound) { event.preventDefault(); error("Refresh live accounting data before downloading."); return; }
        link.href = bound;
      });
      parent.append(link);
    });
  }
  const labels = {statements: "Statements", journals: "Journals", expenses: "Expenses", assets: "Assets",
    receivables: "Receivables", payables: "Payables", inventory: "Inventory", invoices: "Invoices"};
  const helps = {
    statements: "Review period statements, export PDF / Excel, or explicitly lock a reviewed period.",
    journals: "Live ledger entries. Only manual and expense journals can be reversed here; source transactions stay in their original workflows.",
    expenses: "Paid operating expenses by location. Use Payables for supplier bills purchased on credit.",
    assets: "Fixed assets, carrying values and reviewed manual depreciation. No depreciation schedule runs automatically.",
    receivables: "Existing customer credit sales and Debts remain the source records. Payments here settle the same receivable; do not create a duplicate debt.",
    payables: "Shared central warehouse owns accounts payable. Stock invoices post automatically from Purchases; manual bills here are NON-stock only.",
    inventory: "Remaining FIFO valuation, not editable catalog cost. Shop count corrections update stock and journal together.",
    invoices: "Immutable invoice snapshots for sales, sales returns, purchases and purchase returns. Export the selected snapshot to PDF or Excel.",
  };
  function configureTab() {
    tabs.forEach(tab => { const active = tab.dataset.accountingTab === state.tab;
      tab.classList.toggle("active", active); tab.setAttribute("aria-selected", String(active)); tab.tabIndex = active ? 0 : -1; });
    el("panel").setAttribute("aria-labelledby", `accounting-tab-${state.tab}`);
    el("panel-title").textContent = labels[state.tab]; el("panel-help").textContent = helps[state.tab];
    const dates = ["statements", "journals"].includes(state.tab);
    el("start-group").hidden = !dates; el("end-group").hidden = !dates;
    el("start").required = dates; el("end").required = dates;
    const kinds = state.tab === "statements" ? [["profit-loss", "Profit and loss"], ["balance-sheet", "Balance sheet"], ["trial-balance", "Trial balance"]] :
      state.tab === "invoices" ? [["sales", "Sales"], ["sales-return", "Sales returns"], ["purchase", "Purchases"], ["purchase-return", "Purchase returns"]] : [];
    el("kind-group").hidden = !kinds.length; options(el("kind"), kinds);
    const toolbar = el("toolbar"); toolbar.replaceChildren();
    const actions = {statements: [["Lock period…", periodLock]], journals: [["New manual journal", newJournal]],
      expenses: [["Record expense", newExpense]], assets: [["Register asset", newAsset]], receivables: [],
      payables: [["Add non-stock bill", newBill], ["Return purchase stock", () => purchaseReturn()]],
      inventory: [["Correct shop count", adjustInventory]], invoices: []};
    actions[state.tab].forEach(([label, action]) => toolbar.append(button(label, action, label.startsWith("Lock") ? "btn btn-outline-danger" : "btn btn-outline-primary")));
    updateScope(); syncControls();
  }
  function updateScope() {
    const loc = selectedLocation();
    el("scope-note").textContent = loc ? `${loc.name} · ${central(loc) ? "Central warehouse — shared supplier liabilities" : "Shop — sales, cash and receivables"}. Monetary values: ${state.status?.currency || ""}.` :
      "All locations — consolidated statements and read-only reports. Select one location before any write.";
  }
  function filters() {
    const result = {location_id: el("location").value};
    if (["statements", "journals"].includes(state.tab)) {
      result.start = date(el("start").value, "From date"); result.end = date(el("end").value, "Through date");
      if (result.start > result.end) throw new Error("From date cannot be after Through date.");
    }
    if (["statements", "invoices"].includes(state.tab)) result.kind = el("kind").value;
    if (["journals", "payables", "invoices"].includes(state.tab)) result.page = state.page;
    return result;
  }
  function rowActions(item, actions) {
    const group = node("div", null, "d-flex flex-wrap gap-2"); const t = token(), seq = state.list;
    actions.forEach(([label, action]) => {
      const control = button(label, () => {
        if (current(t) && seq === state.list && state.live && online()) action(item);
        else error("This entry is no longer current. Refresh before acting.");
      }, "btn btn-outline-primary"); control.dataset.accountingAction = "true"; group.append(control);
    });
    return group;
  }
  function journalLines(item) {
    const detail = node("details"); detail.append(node("summary", `${item.lines?.length || 0} lines`));
    detail.append(table(`Journal #${item.id} lines`, [["Location", line => locationName(line.location_id)], ["Account", line => line.account_code],
      ["Debit", line => displayMoney(line.debit), true], ["Credit", line => displayMoney(line.credit), true]], item.lines || [])); return detail;
  }
  function renderList(data) {
    const definitions = {
      journals: [["Date", x => x.date], ["Journal", x => `#${x.id}`], ["Memo", x => x.memo], ["Source", x => x.source_type], ["Lines", journalLines],
        ["Actions", x => ["manual", "expense"].includes(x.source_type) ? rowActions(x, [["Reverse…", reverseJournal]]) : "Source workflow"]],
      expenses: [["Date", x => x.date], ["Payee", x => x.payee], ["Description", x => x.description], ["Account", x => x.account_code], ["Paid from", x => x.paid_from], ["Amount", x => displayMoney(x.amount), true]],
      assets: [["Asset", x => x.name], ["Category", x => x.category], ["Acquired", x => x.acquired_on], ["Cost", x => displayMoney(x.cost), true],
        ["Salvage", x => displayMoney(x.salvage_value), true], ["Life (months)", x => x.life_months, true], ["Depreciation", x => displayMoney(x.accumulated_depreciation), true],
        ["Carrying value", x => displayMoney(x.carrying_value), true], ["Status", x => x.status],
        ["Actions", x => x.status === "disposed" ? "Disposed" : rowActions(x, [["Depreciate…", item => assetAction(item, false)], ["Dispose…", item => assetAction(item, true)]])]],
      receivables: [["Customer", x => x.customer_name], ["Sale", x => x.sale_id], ["Branch", x => x.branch_id], ["Due", x => x.due_date],
        ["Original amount", x => displayMoney(x.amount), true], ["Balance", x => displayMoney(x.balance), true],
        ["Actions", x => Number(x.balance) > 0 ? rowActions(x, [["Receive payment", item => payment("receivables", item)]]) : "Settled"]],
      payables: [["Supplier", x => `${x.supplier_name} (#${x.supplier_id})`], ["Reference", x => x.reference], ["Invoice date", x => x.invoice_date], ["Due", x => x.due_date],
        ["Total", x => displayMoney(x.total), true], ["Balance", x => displayMoney(x.balance), true],
        ["Actions", x => rowActions(x, [...(Number(x.balance) > 0 ? [["Pay supplier", item => payment("payables", item)]] : []), ["Return stock…", purchaseReturn]])]],
      inventory: [["Product", x => `${x.product_name} (#${x.product_id})`], ["Location", x => x.location_name], ["Quantity", x => x.quantity, true], ["FIFO value", x => displayMoney(x.value), true]],
      invoices: [["Number", x => x.number], ["Kind", x => x.kind], ["Date", x => x.date], ["Total", x => displayMoney(x.total), true], ["Actions", x => rowActions(x, [["View snapshot", invoiceDetails]])]],
    };
    const caption = `${labels[state.tab]} · ${selectedLocation()?.name || "All locations"} · ${state.status.currency}`;
    el("results").append(table(caption, definitions[state.tab], data.items));
    if (state.tab === "inventory") el("results").append(node("p", `Total remaining FIFO value: ${displayMoney(data.total_value)} ${state.status.currency}`, "fw-semibold mt-3"));
  }
  function renderStatements(data, params) {
    const scope = selectedLocation()?.name || "All locations";
    el("results").append(table(`${data.title} · ${scope} · ${data.start || params.start} through ${data.end || params.end} · ${data.currency}`,
      [["Section", row => row.section], ["Account", row => row.code], ["Description", row => row.label], ["Amount", row => displayMoney(row.amount), true]], data.rows || []));
    const totals = node("dl", null, "row mt-3");
    Object.entries(data.totals || {}).forEach(([name, amount]) => { totals.append(node("dt", name.replace(/_/g, " "), "col-7 col-md-5"), node("dd", displayMoney(amount), "col-5 col-md-7")); });
    el("results").append(totals); exportLinks(el("toolbar"), "/statements/export", params);
  }
  function paginate(data) {
    const nav = el("pagination"); nav.replaceChildren();
    if (!["journals", "payables", "invoices"].includes(state.tab)) { nav.hidden = true; return; }
    const total = Math.max(1, Number(data.total_pages) || 1);
    const previous = button("Previous", () => { if (!acknowledgeDraft()) return; state.page--; loadList(); }); previous.disabled = state.page <= 1;
    const next = button("Next", () => { if (!acknowledgeDraft()) return; state.page++; loadList(); }); next.disabled = state.page >= total;
    nav.append(previous, node("span", `Page ${data.page || state.page} of ${total}`, "small text-muted"), next); nav.hidden = false;
  }
  async function loadList() {
    clearResults(); message(el("error"), "");
    if (!state.live || !online() || !usable()) { error("Reconnect and refresh to load live accounting records."); return; }
    if (!selectedLocation() && !["statements", "inventory", "invoices", "journals"].includes(state.tab)) {
      el("results").append(node("p", "Choose a specific location to view this operational register.", "alert alert-info")); return;
    }
    let params;
    try { params = filters(); } catch (failure) { error(failure.message); return; }
    const t = token(), seq = ++state.list, tab = state.tab;
    el("results").append(node("p", "Loading live records…", "text-muted py-3")); el("results").setAttribute("aria-busy", "true");
    try {
      const data = await request(`${API}/${tab}?${query(params)}`);
      if (!current(t) || seq !== state.list || tab !== state.tab) return;
      el("results").replaceChildren();
      if (tab === "statements") renderStatements(data, params);
      else {
        if (!Array.isArray(data.items)) throw new Error("The accounting list response must contain items.");
        state.items = data.items; renderList(data); paginate(data);
      }
    } catch (failure) {
      if (!current(t) || seq !== state.list) return;
      el("results").replaceChildren(); error(failure.message);
    } finally { if (seq === state.list) { el("results").removeAttribute("aria-busy"); syncControls(); } }
  }
  function snapshotNode(value, depth = 0) {
    if (value == null || typeof value !== "object") return node("span", value == null ? "—" : value);
    if (depth > 7) return node("pre", JSON.stringify(value, null, 2), "text-wrap text-break");
    if (Array.isArray(value)) {
      if (value.length && value.every(item => item && typeof item === "object" && !Array.isArray(item))) {
        const keys = [...new Set(value.flatMap(item => Object.keys(item)))];
        return table("Snapshot entries", keys.map(key => [key.replace(/_/g, " "), item => snapshotNode(item[key], depth + 1)]), value);
      }
      const list = node("ul"); value.forEach(item => { const entry = node("li"); entry.append(snapshotNode(item, depth + 1)); list.append(entry); }); return list;
    }
    const list = node("dl", null, "row");
    Object.entries(value).forEach(([key, item]) => {
      const detail = node("dd", null, "col-12 col-md-8 text-break"); detail.append(snapshotNode(item, depth + 1));
      list.append(node("dt", key.replace(/_/g, " "), "col-12 col-md-4"), detail);
    }); return list;
  }
  async function invoiceDetails(item) {
    const t = token(), seq = ++state.detail, list = state.list;
    const host = el("document"); host.hidden = false; host.replaceChildren(node("p", "Loading invoice snapshot…", "p-3"));
    try {
      const data = await request(`${API}/invoices/${id(item.id)}`);
      if (!current(t) || seq !== state.detail || list !== state.list) return;
      const body = node("div", null, "card-body"); const title = node("h3", `Invoice ${item.number}`, "h5"); title.tabIndex = -1;
      const tools = node("div", null, "d-flex flex-wrap gap-2 mb-3"); exportLinks(tools, `/invoices/${id(item.id)}/export`, {}, t);
      tools.append(button("Close snapshot", () => { ++state.detail; host.replaceChildren(); host.hidden = true; }));
      body.append(title, tools, snapshotNode(data.snapshot ?? data)); host.replaceChildren(body); title.focus();
    } catch (failure) { if (current(t) && seq === state.detail && list === state.list) { host.replaceChildren(); host.hidden = true; error(failure.message); } }
  }

  async function loadAccounting() {
    if (!allowed()) { root.hidden = true; return; }
    if (state.pending) return;
    const who = identity();
    if (who !== state.identity) {
      state.identity = who; state.epoch++; state.status = null; state.opening = null; state.editor = null; state.setup = null;
      el("setup-form").reset(); el("opening-lines").replaceChildren(); el("opening-payables").replaceChildren();
      el("editor").replaceChildren(); el("editor").hidden = true;
      message(el("setup-error"), ""); message(el("notice"), "");
    }
    if (state.editor?.uncertain || state.setup?.uncertain) {
      error("An earlier posting has an unconfirmed outcome. Retry it with its retained request key before refreshing."); return;
    }
    state.epoch++; const t = token(), seq = ++state.load;
    state.live = false; state.loading = true; clearResults();
    el("opening-summary").replaceChildren(); el("opening-warnings").replaceChildren(); el("opening-warnings").hidden = true;
    el("book-status").textContent = ""; message(el("error"), ""); el("loading").hidden = false; syncControls();
    try {
      const status = await request(`${API}/status`);
      if (!current(t) || seq !== state.load) return;
      if (!Array.isArray(status.locations) || !Array.isArray(status.accounts)) throw new Error("Accounting status must contain locations and accounts.");
      const first = !state.status; state.status = status;
      el("book-status").textContent = status.configured ? `${status.currency} · Active since ${status.start_date} · ${status.locked_through ? `Locked through ${status.locked_through}` : "No locked period"}` : "Accounting is not activated.";
      el("setup").hidden = Boolean(status.configured); el("workspace").hidden = !status.configured;
      const oldLocation = el("location").value;
      options(el("location"), locationOptions(), "All locations (reports only)");
      if (locations().some(item => String(item.id) === oldLocation)) el("location").value = oldLocation;
      if (!status.configured) {
        const opening = await request(`${API}/opening`);
        if (!current(t) || seq !== state.load) return;
        state.opening = opening; renderOpening(opening);
        if (!state.setup) state.setup = {form: el("setup-form"), fields: el("setup-fields"), error: el("setup-error"), progress: el("setup-progress"),
          submit: el("activate"), submitLabel: "Activate with reviewed balances", identity: who, key: null, uncertain: false};
      } else {
        state.opening = null;
        if (state.setup && !state.setup.uncertain) {
          state.setup = null;
          el("setup-form").reset();
          el("opening-lines").replaceChildren();
          el("opening-payables").replaceChildren();
          message(el("setup-error"), "");
        }
        if (first || !el("start").value) {
          el("start").value = status.start_date || `${today().slice(0, 7)}-01`; el("end").value = today(); configureTab();
        }
        updateScope();
      }
      state.live = true;
    } catch (failure) {
      if (current(t) && seq === state.load) { state.opening = null; updateEquity(); error(failure.message); }
    } finally {
      if (seq === state.load) { state.loading = false; el("loading").hidden = true; syncControls(); }
    }
    if (current(t) && state.live && state.status.configured) await loadList();
  }
  window.loadAccounting = loadAccounting;

  el("setup-form").addEventListener("submit", event => {
    event.preventDefault(); const op = state.setup; if (!op) return;
    saveOperation(op, () => ({confirm: el("setup-confirm").checked, stock_costs_reviewed: el("costs-reviewed").checked,
      opening_balances: readOpening(), payables: [...el("opening-payables").children].map(row => ({
        supplier_id: id(value(row, "supplier_id"), "Supplier"), reference: value(row, "reference"), amount: money(value(row, "amount"), true),
        ...(value(row, "due_date") ? {due_date: date(value(row, "due_date"))} : {}),
      }))}), "/setup", () => { el("setup-form").reset(); el("opening-lines").replaceChildren(); el("opening-payables").replaceChildren(); state.setup = null; });
  });
  el("add-opening").addEventListener("click", addOpeningLine);
  el("add-opening-payable").addEventListener("click", addOpeningPayable);
  el("setup-form").addEventListener("input", updateEquity);
  el("refresh").addEventListener("click", loadAccounting);
  el("location").addEventListener("change", () => { state.page = 1; updateScope(); loadList(); });
  el("filter-form").addEventListener("submit", event => { event.preventDefault(); state.page = 1; loadList(); });
  ["start", "end", "kind"].forEach(name => el(name).addEventListener("change", clearResults));
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => {
      if (!acknowledgeDraft() || state.pending || !usable()) return;
      state.tab = tab.dataset.accountingTab; state.page = 1; configureTab(); loadList();
    });
    tab.addEventListener("keydown", event => {
      let target;
      if (event.key === "ArrowRight") target = (index + 1) % tabs.length;
      if (event.key === "ArrowLeft") target = (index + tabs.length - 1) % tabs.length;
      if (event.key === "Home") target = 0;
      if (event.key === "End") target = tabs.length - 1;
      if (target != null) { event.preventDefault(); tabs[target].focus(); tabs[target].click(); }
    });
  });
  function clearLiveAccounting() {
    state.epoch++; state.live = false; clearResults(); state.opening = null;
    el("opening-summary").replaceChildren(); el("opening-warnings").replaceChildren(); el("opening-warnings").hidden = true;
    updateEquity(); error("Offline: financial data is unavailable. Drafts stay on this screen, but no accounting form can be submitted or queued."); syncControls();
  }
  function handleAccountingConnection(event) {
    if (!visible()) return;
    const status = event?.detail?.status;
    if (status === "offline") { clearLiveAccounting(); return; }
    if (!state.identity || !["online", "synced"].includes(status) || state.live || state.loading || state.pending) return;
    // Refreshing is read-only. Uncertain operations require an explicit retry.
    if (state.editor?.uncertain || state.setup?.uncertain) { state.live = true; syncControls(); message(el("notice"), "Connection restored. Retry the retained request to confirm its outcome."); }
    else loadAccounting();
  }
  window.addEventListener("offline", clearLiveAccounting);
  // The dashboard emits this only after its server-health probe settles.
  window.addEventListener("pos-connection-status", handleAccountingConnection);
  window.addEventListener("beforeunload", event => {
    if (state.pending || state.editor?.dirty || state.editor?.uncertain || state.setup?.key ||
        (!el("setup").hidden && (el("opening-lines").children.length || el("opening-payables").children.length))) {
      event.preventDefault(); event.returnValue = "";
    }
  });
  // Leaving this SPA section discards rendered financial results, not an editable
  // form or its retry key. Re-entry always invokes the live section loader.
  new MutationObserver(() => {
    if (!visible()) { state.epoch++; clearResults(); el("opening-summary").replaceChildren(); }
  }).observe(root, {attributes: true, attributeFilter: ["style", "hidden"]});
})();
