/* Manager-only catalog coverage, loaded when the monitor opens. */
(() => {
  const el = name => document.getElementById(`product-differences-${name}`);
  const modal = document.getElementById("productDifferencesModal");
  const branch = el("branch"), rows = el("rows"), status = el("status");
  const counts = el("counts"), prev = el("prev"), next = el("next");
  let page = 1, revision = 0;

  async function load(requestedPage = 1) {
    const ownRevision = ++revision;
    const params = new URLSearchParams({page: requestedPage});
    const selected = branch.value;
    if (selected) params.set("compare_branch", selected);
    branch.disabled = prev.disabled = next.disabled = true;
    rows.replaceChildren();
    counts.textContent = "";
    status.textContent = "Comparing catalogs…";
    try {
      const response = await fetch(`/api/products/differences?${params}`);
      if (!response.ok) throw new Error("Cannot compare catalogs right now. Reopen the monitor to retry.");
      const data = await response.json();
      if (ownRevision !== revision) return;
      page = data.page;
      branch.replaceChildren(new Option("All other active branches", ""));
      for (const [id, name] of Object.entries(data.branches)) {
        if (Number(id) !== data.baseline) branch.add(new Option(name, id));
      }
      branch.value = selected;
      counts.textContent = Object.entries(data.counts).map(([id, count]) =>
        `${data.branches[id]}: ${count.toLocaleString()} products`).join(" · ");
      const names = ids => ids.map(id => data.branches[id]).join(", ") || "—";
      for (const item of data.items) {
        const row = rows.insertRow();
        for (const value of [item.name, item.barcode || "—", item.sku || "—", names(item.present),
            names(item.missing), names(item.duplicates)]) row.insertCell().textContent = value;
      }
      status.textContent = Object.keys(data.branches).length < 2 ? "Add another active branch to compare catalogs." :
        data.total ? `Showing ${(page - 1) * 50 + 1}–${(page - 1) * 50 + data.items.length} of ${data.total} catalog differences.` :
          "No catalog differences found.";
      prev.disabled = page <= 1;
      next.disabled = page * 50 >= data.total;
    } catch (error) {
      if (ownRevision === revision) status.textContent = error.message;
    } finally {
      if (ownRevision === revision) branch.disabled = false;
    }
  }

  modal.addEventListener("show.bs.modal", event => {
    if (!requireCapability("admin")) { event.preventDefault(); return; }
    branch.replaceChildren(new Option("All other active branches", ""));
    load();
  });
  modal.addEventListener("hidden.bs.modal", () => { revision++; });
  branch.addEventListener("change", () => load());
  prev.addEventListener("click", () => load(page - 1));
  next.addEventListener("click", () => load(page + 1));
})();