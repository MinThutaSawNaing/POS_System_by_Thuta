/* Manager-only comparison; choosing branches never switches the POS branch. */
(() => {
  const el = name => document.getElementById(`product-differences-${name}`);
  const modal = document.getElementById("productDifferencesModal");
  const target = el("target"), choices = el("choices"), status = el("status");
  const compare = el("compare"), run = el("run"), all = el("select-all"), clear = el("clear");
  const results = el("results"), rows = el("rows"), prev = el("prev"), next = el("next");
  const counts = el("counts"), summary = el("summary"), pageInfo = el("page");
  let branches = [], page = 1, revision = 0;

  const checkboxes = () => [...choices.querySelectorAll("input")];
  const selected = () => checkboxes().filter(input => input.checked).map(input => input.value);
  const branchLabel = branch => `${branch.name} (${branch.code})`;

  function renderSelection() {
    const total = checkboxes().length;
    el("selection").textContent = total
      ? `${selected().length} of ${total} selected`
      : "Choose at least one comparison branch";
  }

  function resetResults() {
    revision++;
    results.hidden = true;
    run.disabled = !target.value || !selected().length;
    renderSelection();
    status.textContent = branches.length < 2 ? "At least two active branches are needed to compare catalogs." :
      "Choose comparison branches, then select Compare catalogs.";
  }

  function renderChoices(keep = []) {
    choices.replaceChildren();
    for (const branch of branches) {
      if (String(branch.id) === target.value) continue;
      const group = document.createElement("span");
      group.className = "d-inline-flex";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.className = "btn-check";
      input.id = `product-differences-branch-${branch.id}`;
      input.value = branch.id;
      input.autocomplete = "off";
      input.checked = keep.includes(input.value);
      const label = document.createElement("label");
      label.className = "btn btn-sm btn-outline-secondary";
      label.htmlFor = input.id;
      label.textContent = branchLabel(branch);
      group.append(input, label);
      choices.append(group);
    }
    compare.disabled = !choices.children.length;
    resetResults();
  }

  async function fetchData(params) {
    const response = await fetch(`/api/products/differences?${params}`, {cache: "no-store"});
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || "Could not load the comparison.");
    return data;
  }

  function renderResult(detail, item, baseline, names) {
    const issues = [];
    if (item.conflict) issues.push("Conflicting barcodes");
    if (item.duplicates.length) issues.push(`Multiple records in ${names(item.duplicates)}`);
    if (item.unverified) issues.push("Matched by name only; check manually");
    const badge = document.createElement("span");
    const missingTarget = item.missing.includes(baseline);
    badge.className = issues.length ? "badge text-bg-warning" :
      missingTarget ? "badge text-bg-danger" : "badge text-bg-secondary";
    badge.textContent = issues.length ? "Needs review" :
      missingTarget ? "Missing from target" : "Missing from comparison";
    detail.append(badge);
    if (issues.length) {
      const note = document.createElement("div");
      note.className = "small text-muted";
      note.textContent = issues.join("; ");
      detail.append(note);
    }
  }

  function renderRecords(detail, records, name) {
    const list = document.createElement("ul");
    list.className = "small text-muted mb-0 ps-3";
    for (const record of records) {
      const entry = document.createElement("li");
      entry.textContent = `${name(record.branch_id)}: ${record.name} (${record.barcode || record.sku || "no barcode or SKU"})`;
      list.append(entry);
    }
    detail.append(list);
  }


  async function load(requestedPage = 1) {
    const comparisons = selected();
    if (!target.value || !comparisons.length) return;
    const ownRevision = ++revision;
    const params = new URLSearchParams({page: requestedPage, target_branch: target.value});
    comparisons.forEach(id => params.append("compare_branch", id));
    results.hidden = true;
    status.textContent = "Comparing catalogs…";
    run.disabled = true;
    try {
      const data = await fetchData(params);
      if (ownRevision !== revision) return;
      page = data.page;
      const name = id => data.branches[id];
      const names = ids => ids.map(name).join(", ") || "—";
      counts.textContent = [String(data.baseline), ...comparisons].map(id =>
        `${name(id)}${Number(id) === data.baseline ? " (target)" : ""}: ${data.counts[id].toLocaleString()} products`).join(" · ");
      const totals = data.summary;
      summary.textContent = `${totals.missing_from_target} missing from target · ` +
        `${totals.missing_from_comparisons} missing from comparison branches · ${totals.needs_review} need review`;
      rows.replaceChildren();
      for (const item of data.items) {
        const row = rows.insertRow();
        for (const value of [item.name, item.barcode || "—", item.sku || "—", names(item.present),
            item.unverified ? "Not verified" : names(item.missing)]) row.insertCell().textContent = value;
        const detail = row.insertCell();
        renderResult(detail, item, data.baseline, names);
        if (item.records.length) renderRecords(detail, item.records, name);
      }
      if (data.total) {
        const first = (page - 1) * 50 + 1, last = (page - 1) * 50 + data.items.length;
        pageInfo.textContent = `Showing ${first}–${last} of ${data.total.toLocaleString()} differences`;
        status.textContent = "Only products that differ between the selected branches are listed.";
      } else {
        pageInfo.textContent = "";
        status.textContent = "No catalog differences found for these branches.";
      }
      prev.disabled = page <= 1;
      next.disabled = page * 50 >= data.total;
      results.hidden = !data.total;
    } catch (error) {
      if (ownRevision === revision) status.textContent = error.message;
    } finally {
      if (ownRevision === revision) run.disabled = !target.value || !selected().length;
    }
  }


  modal.addEventListener("show.bs.modal", async event => {
    if (!requireCapability("admin")) { event.preventDefault(); return; }
    const ownRevision = ++revision;
    target.disabled = compare.disabled = run.disabled = true;
    results.hidden = true;
    target.replaceChildren();
    choices.replaceChildren();
    renderSelection();
    status.textContent = "Loading active branches…";
    try {
      const data = await fetchData("options=1");
      if (ownRevision !== revision) return;
      branches = data.branches;
      target.replaceChildren(...branches.map(branch => new Option(branchLabel(branch), branch.id)));
      target.value = String(data.current_branch);
      target.disabled = branches.length < 2;
      renderChoices();
    } catch {
      if (ownRevision === revision) status.textContent = "Could not load active branches. Reopen the monitor to retry.";
    }
  });
  modal.addEventListener("hide.bs.modal", () => { revision++; });
  target.addEventListener("change", () => renderChoices(selected()));
  choices.addEventListener("change", resetResults);
  all.addEventListener("click", () => {
    checkboxes().forEach(input => { input.checked = true; });
    resetResults();
  });
  clear.addEventListener("click", () => {
    checkboxes().forEach(input => { input.checked = false; });
    resetResults();
  });
  el("form").addEventListener("submit", event => { event.preventDefault(); load(); });
  prev.addEventListener("click", () => load(page - 1));
  next.addEventListener("click", () => load(page + 1));
})();
