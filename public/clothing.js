/* Optional clothing UI. Product IDs, not styles or size/color guesses, enter the cart. */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root && root.document) {
    const start = () => {
      const ui = api.createUI({
        document: root.document,
        fetch: (...args) => root.fetch(...args),
        context: () => ({
          branch: typeof currentBranch !== "undefined" ? currentBranch?.id : null,
          user: typeof CACHE_USER_ID !== "undefined" ? CACHE_USER_ID : null,
          blocked: (typeof branchSwitchInFlight !== "undefined" && branchSwitchInFlight) ||
            (typeof dashboardBranchStale !== "undefined" && dashboardBranchStale) ||
            (typeof dashboardSessionStale !== "undefined" && dashboardSessionStale),
        }),
        canWrite: () => typeof hasCapability === "function" && hasCapability("catalog.write"),
        addProduct: (product) => {
          // Re-fetch the exact Product through the normal promotion/stock path.
          addToCart(product.id, api.displayName(product), product.price, 1, product.tax_rate);
        },
        changed: () => { if (typeof invalidateProductsCache === "function") invalidateProductsCache(); },
        receive: () => { if (typeof showSection === "function") showSection("purchases"); },
        key: () => root.crypto?.randomUUID ? root.crypto.randomUUID()
          : "clothing-" + Date.now() + "-" + Math.random().toString(36).slice(2),
      });
      root.ClothingUI = ui;
      ui.mount();
    };
    if (root.document.readyState === "loading") root.document.addEventListener("DOMContentLoaded", start);
    else start();
  }
})(typeof window !== "undefined" ? window : null, function () {
  "use strict";

  function displayName(product) {
    if (product.display_name) return product.display_name;
    if (!product.variant_id) return product.name || "";
    const attrs = [product.color, product.size].filter(Boolean).join(" / ");
    return (product.name || product.style_name || "") + (attrs ? " — " + attrs : "");
  }

  function exactProduct(variants, id, styleId) {
    return variants.find((p) => String(p.id) === String(id) &&
      String(p.style_id) === String(styleId) && !p.archived && Number(p.stock) > 0);
  }

  function createUI(env) {
    const doc = env.document;
    const panels = [];
    let epoch = 0;
    const scope = () => {
      const c = env.context();
      if (!c.branch || c.blocked) throw new Error("Choose an active branch and reload this panel.");
      return String(c.user) + ":" + String(c.branch);
    };
    const ticket = () => ({scope: scope(), epoch});
    function current(t) {
      try { return t.epoch === epoch && t.scope === scope(); } catch (_) { return false; }
    }
    async function request(url, body, t) {
      if (!current(t)) throw new Error("Branch changed. Reopen the clothing panel.");
      const response = await env.fetch(url, {
        method: body === undefined ? "GET" : "POST",
        credentials: "same-origin", cache: "no-store",
        headers: {"Content-Type": "application/json", "x-pos-branch-id": String(env.context().branch)},
        ...(body === undefined ? {} : {body: JSON.stringify(body)}),
      });
      const data = await response.json();
      if (!current(t)) throw new Error("Branch changed. Reopen the clothing panel.");
      if (!response.ok || data.success === false) throw new Error(data.message || data.error || "Clothing request failed.");
      return data;
    }
    function el(tag, text, cls) {
      const node = doc.createElement(tag);
      if (text !== undefined) node.textContent = String(text);
      if (cls) node.className = cls;
      return node;
    }
    function button(text, handler, cls) {
      const node = el("button", text, cls || "btn btn-outline-primary btn-sm");
      node.type = "button";
      node.addEventListener("click", handler);
      return node;
    }
    function field(parent, label, type, required) {
      const wrap = el("label", label, "form-label d-block mt-2");
      const input = el(type === "textarea" ? "textarea" : "input", undefined, "form-control");
      if (type !== "textarea") input.type = type || "text";
      input.required = !!required;
      if (type === "number") { input.min = "0"; input.step = "0.01"; }
      wrap.append(input); parent.append(wrap);
      return input;
    }
    function option(select, value, text) {
      const node = el("option", text); node.value = String(value); select.append(node);
    }
    function selectField(parent, label) {
      const wrap = el("label", label, "form-label d-block mt-2");
      const select = el("select", undefined, "form-select"); wrap.append(select); parent.append(wrap);
      return select;
    }
    function table(parent, headers, rows) {
      const wrap = el("div", undefined, "table-responsive mt-2");
      const tab = el("table", undefined, "table table-sm");
      const head = el("thead"), tr = el("tr");
      headers.forEach((h) => tr.append(el("th", h))); head.append(tr); tab.append(head);
      const body = el("tbody");
      rows.forEach((values) => {
        const row = el("tr");
        values.forEach((value) => { const td = el("td"); td.append(value && value.nodeType ? value : doc.createTextNode(String(value ?? ""))); row.append(td); });
        body.append(row);
      });
      tab.append(body); wrap.append(tab); parent.append(wrap);
    }
    function status(panel, message) { panel.status.textContent = message; }
    function clearPreview(panel) {
      panel.preview = null;
      if (panel.commit) panel.commit.disabled = true;
      if (panel.review) panel.review.replaceChildren();
    }
    function reset(panel) {
      panel.token = null; panel.variants = []; clearPreview(panel);
      panel.style.replaceChildren(); option(panel.style, "", "Select a style");
      panel.grid.replaceChildren();
      if (panel.size) { panel.size.replaceChildren(); option(panel.size, "", "All sizes"); }
      if (panel.color) { panel.color.replaceChildren(); option(panel.color, "", "All colors"); }
    }
    function invalidate() {
      epoch += 1;
      panels.forEach((panel) => { reset(panel); status(panel, "Catalog or stock changed. Reload clothing styles before selecting a variant."); });
    }
    async function loadStyles(panel) {
      reset(panel);
      try {
        const t = ticket(); panel.token = t;
        status(panel, "Loading styles…");
        const data = await request("/api/clothing/styles", undefined, t);
        if (panel.token !== t) return;
        (data.styles || []).filter((s) => !s.archived).forEach((s) => option(panel.style, s.id, [s.name, s.code].filter(Boolean).join(" · ")));
        status(panel, "Select a style; stock is for the active branch only.");
      } catch (error) { status(panel, error.message); }
    }
    function renderStock(panel) {
      panel.grid.replaceChildren();
      const styleId = panel.style.value;
      const rows = panel.variants.filter((p) => !panel.size ||
        ((!panel.size.value || p.size === panel.size.value) && (!panel.color.value || p.color === panel.color.value)));
      if (!rows.length) { panel.grid.append(el("p", "No matching variants in this branch.")); return; }
      table(panel.grid, ["Size", "Color", "Product ID", "Barcode", "Price", "Stock", panel.admin ? "Status" : "Choose exact variant"], rows.map((p) => {
        let action = p.archived ? "Archived" : "Active";
        if (!panel.admin) {
          action = button("Add 1", () => {
            if (!panel.token || !current(panel.token)) { reset(panel); status(panel, "Branch or catalog changed. Reload clothing styles."); return; }
            const product = exactProduct(panel.variants, p.id, styleId);
            if (!product) { status(panel, "Variant unavailable. Reload stock."); return; }
            env.addProduct(product);
          });
          action.disabled = !!p.archived || !(Number(p.stock) > 0);
        }
        return [p.size, p.color, p.id, p.barcode, p.price, p.stock, action];
      }));
    }
    async function loadVariants(panel) {
      panel.variants = []; panel.grid.replaceChildren();
      const styleId = panel.style.value;
      if (!styleId) return;
      try {
        const t = ticket(); panel.token = t;
        status(panel, "Loading exact branch variants…");
        const data = await request("/api/clothing/styles/" + encodeURIComponent(styleId) + "/variants" + (panel.admin ? "?include_archived=true" : ""), undefined, t);
        if (panel.token !== t || panel.style.value !== styleId) return;
        panel.variants = data.variants || [];
        if (panel.size) {
          [[panel.size, "size", "All sizes"], [panel.color, "color", "All colors"]].forEach(([select, key, title]) => {
            select.replaceChildren(); option(select, "", title);
            [...new Set(panel.variants.map((p) => p[key]).filter(Boolean))].forEach((v) => option(select, v, v));
          });
        }
        renderStock(panel); status(panel, "Stock is read-only. Receive purchases through the existing receiving workflow.");
      } catch (error) { status(panel, error.message); }
    }
    function setupPicker(host, admin) {
      const panel = {admin, host, variants: []}; panels.push(panel);
      panel.status = el("p", "Open this panel to load styles.", "small text-muted mt-2");
      panel.status.setAttribute("role", "status");
      panel.style = selectField(host, "Clothing style");
      host.append(button("Reload clothing styles / stock", () => loadStyles(panel)));
      if (!admin) {
        panel.size = selectField(host, "Filter size (does not select a product)");
        panel.color = selectField(host, "Filter color (does not select a product)");
        panel.size.addEventListener("change", () => renderStock(panel));
        panel.color.addEventListener("change", () => renderStock(panel));
      }
      panel.grid = el("div"); host.append(panel.status, panel.grid);
      panel.style.addEventListener("change", () => loadVariants(panel));
      reset(panel);
      const details = host.closest("details");
      if (details) details.addEventListener("toggle", () => {
        if (details.open) return loadStyles(panel);
      });
      return panel;
    }
    function form(host, title, fields, submit) {
      const details = el("details", undefined, "border rounded p-2 mt-3"); details.append(el("summary", title));
      const node = el("form"); const inputs = {};
      fields.forEach(([key, label, type, required]) => { inputs[key] = field(node, label, type, required); });
      const save = el("button", title, "btn btn-primary btn-sm mt-2"); save.type = "submit"; node.append(save);
      node.addEventListener("submit", async (event) => {
        event.preventDefault(); if (save.disabled) return;
        save.disabled = true;
        try { await submit(Object.fromEntries(Object.entries(inputs).map(([key, input]) => [key, input.value.trim()])), node); }
        finally { save.disabled = false; }
      });
      details.append(node); host.append(details);
    }
    async function write(panel, url, values) {
      try {
        if (!env.canWrite()) throw new Error("Catalog write permission required.");
        await request(url, values, ticket());
        env.changed(); invalidate();
        await loadStyles(panel); status(panel, "Saved. Select a style to review its variants. New variants have zero stock.");
      } catch (error) { status(panel, error.message); }
    }
    function setupAdmin(panel) {
      const host = panel.host;
      host.append(el("p", "New variants start with zero stock. Stock cannot be overwritten here.", "small"));
      host.append(button("Review purchase orders / receive stock", () => env.receive()));
      form(host, "Create style", [["name", "Style name", "text", true], ["code", "Style code", "text", true], ["brand", "Brand", "text"], ["season", "Season", "text"]],
        (values) => write(panel, "/api/clothing/styles", values));
      form(host, "Create variant in selected style", [["size", "Size", "text", true], ["color", "Color", "text", true], ["barcode", "Unique barcode", "text", true], ["price", "Price", "number", true], ["cost", "Cost (optional)", "number"]],
        async (values) => {
          if (!panel.style.value) { status(panel, "Select a style first."); return; }
          if (!values.cost) delete values.cost;
          await write(panel, "/api/clothing/styles/" + encodeURIComponent(panel.style.value) + "/variants", values);
        });
      const section = el("details", undefined, "border rounded p-2 mt-3"); section.append(el("summary", "CSV import — preview, review, then commit")); host.append(section);
      section.append(el("p", "CSV headers: style_id,size,color,barcode,name,price,cost,product_id. Use explicit existing style IDs. Optional product_id attaches an existing product without changing stock. No stock column is accepted.", "small"));
      const csv = field(section, "Paste CSV", "textarea"); csv.rows = 5;
      csv.addEventListener("input", () => { panel.previewGeneration = (panel.previewGeneration || 0) + 1; clearPreview(panel); });
      panel.review = el("div");
      const preview = button("Preview CSV (no changes)", async () => {
        clearPreview(panel);
        const generation = (panel.previewGeneration || 0) + 1; panel.previewGeneration = generation;
        try {
          const t = ticket();
          const data = await request("/api/clothing/import/preview", {csv: csv.value}, t);
          if (generation !== panel.previewGeneration) return;
          if (!data.valid) {
            table(panel.review, ["Row", "Field", "Error"], (data.errors || []).map((e) => [e.row, e.field, e.message]));
            status(panel, "Preview rejected. Fix the CSV; nothing has been imported."); return;
          }
          if (!data.preview_token) throw new Error("Server did not return a reviewed preview token.");
          const rowsJSON = JSON.stringify(data.rows);
          panel.preview = {rowsJSON, preview_token: data.preview_token, idempotency_key: env.key(), ticket: t};
          table(panel.review, ["Style ID", "Size", "Color", "Barcode", "Name", "Price", "Product ID"], data.rows.map((r) => [r.style_id, r.size, r.color, r.barcode, r.name, r.price, r.product_id || "new"]));
          panel.commit.disabled = false;
          status(panel, "Review every row above. Commit imports exactly this preview; new products start with zero stock.");
        } catch (error) { status(panel, error.message); }
      });
      panel.commit = button("Commit reviewed preview", async () => {
        const batch = panel.preview;
        if (!batch || panel.commit.disabled) return;
        panel.commit.disabled = true;
        try {
          await request("/api/clothing/import/commit", {rows: JSON.parse(batch.rowsJSON), preview_token: batch.preview_token, idempotency_key: batch.idempotency_key}, batch.ticket);
          env.changed(); invalidate(); csv.value = "";
          await loadStyles(panel); status(panel, "Reviewed import committed. Select a style to review stock; use purchase receiving to add stock.");
        } catch (error) {
          // An ambiguous network failure must retry the same batch and key.
          if (panel.preview === batch && current(batch.ticket)) panel.commit.disabled = false;
          status(panel, error.message + " Retry uses the same reviewed batch and import key.");
        }
      }, "btn btn-success btn-sm ms-2");
      panel.commit.disabled = true; section.append(preview, panel.commit, panel.review);
    }
    function mount() {
      const pos = doc.querySelector("[data-clothing-picker]");
      if (pos) setupPicker(pos, false);
      const admin = doc.querySelector("[data-clothing-admin]");
      if (admin && env.canWrite()) setupAdmin(setupPicker(admin, true));
    }
    function load(adminOnly) {
      return Promise.all(panels
        .filter((panel) => !adminOnly || panel.admin)
        .map((panel) => loadStyles(panel)));
    }
    return {mount, invalidate, load};
  }
  return {displayName, exactProduct, createUI};
});