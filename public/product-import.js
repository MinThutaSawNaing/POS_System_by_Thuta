/* Product catalog imports are previewed and explicitly confirmed, never automatic. */
(function (root) {
  "use strict";
  function createImporter(env) {
    let busy = false, preview = null, token = null, error = "", snapshot = null;
    let confirming = false;
    let revision = 0;
    const settings = () => ({file: env.file(), scope: env.scope(),
      mapping: JSON.stringify(env.mapping()), duplicate_mode: env.duplicateMode(),
      create_categories: env.createCategories?.() === true});
    const unchanged = saved => {
      const current = settings();
      return saved && current.file === saved.file && current.scope === saved.scope &&
        current.mapping === saved.mapping && current.duplicate_mode === saved.duplicate_mode &&
        current.create_categories === saved.create_categories;
    };
    const canCommit = () => !busy && !confirming && !!token && unchanged(snapshot) &&
      !preview?.summary?.error && (preview?.summary?.create + preview?.summary?.update > 0);
    const state = () => ({busy, confirming, preview, error, canCommit: canCommit()});
    const render = () => env.render(state());
    function invalidate() {
      revision++; token = null; preview = null; snapshot = null; error = "";
      render();
    }
    function payload(saved) {
      return {file: saved.file, mapping: JSON.parse(saved.mapping),
        duplicate_mode: saved.duplicate_mode, create_categories: saved.create_categories};
    }
    async function inspect() {
      if (busy) return;
      const saved = settings(), ownRevision = ++revision;
      token = null; preview = null; snapshot = null; error = "";
      if (!saved.file || !saved.scope) {
        error = "Choose a spreadsheet and an active branch first."; render(); return;
      }
      busy = true; render();
      try {
        const data = await env.request("/api/products/import/preview", payload(saved));
        if (revision !== ownRevision || !unchanged(saved)) return;
        if (!data.success) throw new Error(data.message || "Could not preview the spreadsheet.");
        env.columns?.(data.columns || [], data.mapping || {});
        preview = {...data.preview, warnings: data.preview?.warnings || data.warnings || []};
        token = data.preview_token; snapshot = settings();
      } catch (reason) {
        if (revision === ownRevision) error = reason.message;
      } finally { busy = false; render(); }
    }
    async function commit() {
      if (!canCommit()) return;
      const summary = preview.summary;
      const message = `Import ${summary.create} new product(s), update ${summary.update} existing ` +
        `product(s), and skip ${summary.skip}? Stock quantities will not change.`;
      // The confirmation is the dashboard's own window, so it may resolve
      // asynchronously. `confirming` keeps a second click from opening a second
      // window or starting two imports.
      confirming = true; render();
      let confirmed = false;
      try { confirmed = await env.confirm(message, {...summary}); }
      finally { confirming = false; }
      if (!confirmed || !canCommit()) { render(); return; }
      const saved = snapshot, signedToken = token, ownRevision = revision;
      busy = true; error = ""; render();
      try {
        const data = await env.request("/api/products/import/commit",
          {...payload(saved), preview_token: signedToken});
        if (!data.success) throw new Error(data.message || "Import failed. Preview again before retrying.");
        token = null; snapshot = null;
        if (revision === ownRevision && unchanged(saved)) {
          preview = null;
          busy = false; render();
          env.committed(data);
        }
      } catch (reason) {
        token = null;
        if (revision === ownRevision) error = reason.message + " Preview again before importing.";
      } finally { busy = false; render(); }
    }
    return {preview: inspect, commit, invalidate, state};
  }
  if (typeof module !== "undefined" && module.exports) module.exports = {createImporter};
  if (!root.document) return;
  const doc = root.document, el = id => doc.getElementById(id);
  const modal = el("importProductsModal");
  if (!modal) return;
  const fields = {name: "Product name *", barcode: "Barcode / SKU", price: "Selling price *",
    cost: "Cost", category: "Category", unit: "Unit", tax_rate: "Tax rate (%)",
    photo: "Photo (picture in the cell)"};
  const scope = () => typeof currentBranch !== "undefined" && currentBranch?.id &&
    !dashboardSessionStale && !dashboardBranchStale && !branchSwitchInFlight &&
    hasCapability("catalog.write") ? String(currentBranch.id) : null;
  const mapping = () => {
    const result = {};
    Object.keys(fields).forEach(key => {
      const select = el("product-import-map-" + key);
      if (select) result[key] = select.value;
    });
    return result;
  };
  let headers = [], mappingReady = false;
  function renderColumns(columns, inferred) {
    if (mappingReady && JSON.stringify(columns) === JSON.stringify(headers)) return;
    headers = columns; mappingReady = true;
    const holder = el("product-import-columns"); holder.replaceChildren();
    Object.entries(fields).forEach(([key, label]) => {
      const wrapper = doc.createElement("div"); wrapper.className = "col-12 col-sm-6 col-lg-4";
      const title = doc.createElement("label"); title.className = "form-label small";
      title.htmlFor = "product-import-map-" + key; title.textContent = label;
      const select = doc.createElement("select"); select.className = "form-select form-select-sm";
      select.id = title.htmlFor;
      const empty = doc.createElement("option"); empty.value = ""; empty.textContent = "Not imported";
      select.appendChild(empty);
      columns.forEach(header => {
        const option = doc.createElement("option"); option.value = header; option.textContent = header;
        select.appendChild(option);
      });
      select.value = inferred[key] || "";
      select.addEventListener("change", importer.invalidate);
      wrapper.appendChild(title); wrapper.appendChild(select); holder.appendChild(wrapper);
    });
    el("product-import-mapping").classList.remove("d-none");
  }
  const importer = createImporter({
    file: () => el("product-import-file").files[0], scope,
    mapping: () => mappingReady ? mapping() : {},
    duplicateMode: () => el("product-import-mode").value,
    createCategories: () => el("product-import-create-categories").checked,
    confirm: (message, summary) => typeof root.productImportConfirmDialog === "function"
      ? root.productImportConfirmDialog(message, summary)
      : root.confirm(message),
    columns: renderColumns,
    async request(path, values) {
      const body = new FormData();
      Object.entries(values).forEach(([key, value]) => {
        body.append(key, key === "mapping" ? JSON.stringify(value) : value);
      });
      const response = await root.fetch(path, {method: "POST", body, credentials: "same-origin"});
      let data;
      try { data = await response.json(); }
      catch (error) { throw new Error(response.status === 413 ? "File exceeds the 15 MB upload limit." : "The server could not read this import. Please retry."); }
      if (!response.ok) throw new Error(data.message || "The import request failed.");
      return data;
    },
    render(state) {
      modal.setAttribute("aria-busy", String(state.busy));
      el("product-import-working").classList.toggle("d-none", !state.busy);
      el("product-import-preview-btn").disabled = state.busy;
      el("product-import-confirm-btn").disabled = !state.canCommit;
      modal.querySelectorAll("input, select, [data-bs-dismiss]").forEach(control => { control.disabled = state.busy; });
      el("product-import-error").textContent = state.error;
      el("product-import-error").classList.toggle("d-none", !state.error);
      el("product-import-preview").classList.toggle("d-none", !state.preview);
      if (!state.preview) return;
      const summary = state.preview.summary;
      el("product-import-summary").textContent = `${summary.create} new · ${summary.update} update · ${summary.skip} skip · ${summary.error} error(s)`;
      const notices = el("product-import-warnings"); notices.replaceChildren();
      (state.preview.warnings || []).forEach(warning => {
        const line = doc.createElement("li"); line.textContent = warning; notices.appendChild(line);
      });
      const table = el("product-import-rows"); table.replaceChildren();
      state.preview.rows.forEach(row => {
        const tr = doc.createElement("tr");
        const changes = Object.entries(row.changes || {}).map(([key, value]) =>
          `${fields[key] || key}: ${value}`).join("; ");
        const detail = [row.message, changes, row.photo ? `photo: ${row.photo}` : ""].filter(Boolean).join(" — ");
        [row.row, row.name, row.barcode, row.action, detail].forEach(value => {
          const td = doc.createElement("td"); td.textContent = String(value ?? ""); tr.appendChild(td);
        });
        if (row.action === "error") tr.className = "table-danger";
        table.appendChild(tr);
      });
    },
    committed(data) {
      const summary = data.summary;
      showToast(`Import complete: ${summary.create} added, ${summary.update} updated, ${summary.skip} skipped.`, "success");
      el("product-import-file").value = "";
      markBranchFormSaved("importProductsModal");
      bootstrap.Modal.getInstance(modal)?.hide();
      invalidateProductsCache(); cachedCategories = null;
      loadProducts(); loadDashboardStats(); loadCategoryDropdowns();
    },
  });
  el("product-import-file").addEventListener("change", () => {
    mappingReady = false; headers = []; el("product-import-columns").replaceChildren();
    el("product-import-mapping").classList.add("d-none"); importer.invalidate();
  });
  el("product-import-mode").addEventListener("change", importer.invalidate);
  el("product-import-create-categories").addEventListener("change", importer.invalidate);
  el("product-import-preview-btn").addEventListener("click", importer.preview);
  el("product-import-confirm-btn").addEventListener("click", importer.commit);
  el("product-import-template-btn").addEventListener("click", () => {
    if (requireCapability("catalog.write")) openReportDownload("/api/products/import/template");
  });
  modal.addEventListener("show.bs.modal", event => {
    if (!scope()) { event.preventDefault(); return; }
    el("product-import-branch").textContent = currentBranch.name || String(currentBranch.id);
    importer.invalidate();
  });
  modal.addEventListener("hide.bs.modal", event => {
    const state = importer.state();
    if (state.busy || state.confirming) event.preventDefault();
  });
  modal.addEventListener("hidden.bs.modal", importer.invalidate);
  importer.invalidate();
})(typeof window === "undefined" ? globalThis : window);