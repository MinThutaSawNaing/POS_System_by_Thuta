"""Regression tests for the SPA data cache that makes tab switching free.

The dashboard is one page: every sidebar item is a section that used to re-fetch
all of its data on every click. With a large catalogue that turns a tab switch
into a database stress test. These tests run the cache layer - extracted verbatim
from the template - against a stub network, so a tab switch that should cost
nothing cannot silently start querying the server again, and so no write can be
allowed to leave stale rows on screen.
"""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


DASHBOARD = Path(__file__).parent / "templates" / "dashboard.html"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is required to execute dashboard JavaScript")

CACHE_START = "// ===== SPA DATA CACHE ====="
CACHE_END = "// ===== END SPA DATA CACHE ====="
SECTION_START = "// ===== SECTION (TAB) RENDER STATE ====="
SECTION_END = "// ===== END SECTION (TAB) RENDER STATE ====="


def _block(source, start_marker, end_marker):
    assert start_marker in source, f"missing marker {start_marker}"
    assert end_marker in source, f"missing marker {end_marker}"
    start = source.index(start_marker)
    end = source.index(end_marker, start) + len(end_marker)
    return source[start:end]


def _cache_source():
    source = DASHBOARD.read_text(encoding="utf-8")
    return "\n".join(
        (
            _block(source, CACHE_START, CACHE_END),
            _block(source, SECTION_START, SECTION_END),
        )
    )


# The stub network is installed before the extracted code runs, so nativeFetch
# binds to it and the code under test then replaces window.fetch with smartFetch.
PROLOGUE = """
globalThis.currentBranch = null;
globalThis.log = [];
globalThis.results = [];
let network = null;

// Stubs for the helpers the cache layer shares with the rest of the dashboard
// (offline snapshot + the legacy in-memory product list).
let cachedProducts = [];
let productsCacheTimestamp = 0;
let cachedProductsBranchId = null;
function isBrowserOffline() { return false; }
function offlineCacheRemove() {}
function cachedProductsKey() { return "pos_products_all_cache"; }

globalThis.window = {
  location: { origin: "http://localhost:5000" },
  fetch: (input, init) => network(input, init),
};
"""

EPILOGUE = """
// In a browser window === globalThis, so patching window.fetch also patches the
// bare fetch the template calls. Node has its own global fetch, so mirror it here.
globalThis.fetch = window.fetch;
function check(name, passed) {
  results.push({ name: name, passed: Boolean(passed) });
}
function jsonResponse(payload, options) {
  const opts = options || {};
  return new Response(JSON.stringify(payload), {
    status: opts.status || 200,
    statusText: opts.failed ? "Internal Server Error" : "OK",
    headers: { "content-type": opts.contentType || "application/json" },
  });
}
function bodyOf(response) {
  // Accepts a Response or the promise that resolves to one.
  return Promise.resolve(response).then((res) => res.text()).then((text) => (text ? JSON.parse(text) : null));
}
function callsTo(fragment) {
  return log.filter((entry) => entry.indexOf(fragment) !== -1);
}
async function tick() {
  await new Promise((resolve) => setTimeout(resolve, 5));
  await new Promise((resolve) => setTimeout(resolve, 5));
}
async function main() {
"""

FOOTER = """
}
main()
  .then(() => console.log(JSON.stringify(results)))
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });
"""



# --------------------------------------------------------------------------
# Read path
# --------------------------------------------------------------------------


def test_reads_are_served_from_memory_and_keyed_by_query():
    body = """
  network = async (input) => { log.push(String(input)); return jsonResponse({ hit: log.length }); };

  const first = await bodyOf(await fetch("/api/products?page=1&per_page=10"));
  const second = await bodyOf(await fetch("/api/products?page=1&per_page=10"));
  const otherPage = await bodyOf(await fetch("/api/products?page=2&per_page=10"));

  check("one round trip per url", callsTo("page=1&per_page=10").length === 1);
  check("repeat read paints the cached payload", first.hit === second.hit && first.hit === 1);
  check("a different query is a different key", callsTo("page=2&per_page=10").length === 1);
"""
    run_cache_tests(body)


def test_concurrent_duplicate_reads_share_one_round_trip():
    body = """
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  network = async (input) => { log.push(String(input)); await gate; return jsonResponse({ n: log.length }); };

  const a = fetch("/api/sales?page=1");
  const b = fetch("/api/sales?page=1");
  const c = fetch("/api/sales?page=1");
  const oneQuery = callsTo("/api/sales").length === 1;
  release();
  const payloads = await Promise.all([bodyOf(a), bodyOf(b), bodyOf(c)]);

  check("three simultaneous readers caused one query", oneQuery);
  check("every reader received the payload", payloads.every((p) => p && p.n === 1));
"""
    run_cache_tests(body)


def test_stale_but_recent_data_paints_immediately_and_revalidates():
    body = """
  let version = 1;
  network = async (input) => { log.push(String(input)); return jsonResponse({ version: version }); };

  const painted = await bodyOf(await fetch("/api/products?page=1"));
  check("first visit queried the server", callsTo("/api/products").length === 1);

  // Age the entry past the 60s TTL but inside the 15s stale grace window.
  const entry = Array.from(cacheStore.values())[0];
  entry.savedAt = Date.now() - 65000;
  version = 2;

  const stalePaint = await bodyOf(await fetch("/api/products?page=1"));
  check("stale payload is painted without waiting", stalePaint.version === 1);
  check("stale read was answered from memory", callsTo("/api/products").length === 2);
  await tick();
  check("background refresh ran once", callsTo("/api/products").length === 2);

  const fresh = await bodyOf(await fetch("/api/products?page=1"));
  check("next visit shows the refreshed payload", fresh.version === 2);
"""
    run_cache_tests(body)


def test_payload_older_than_the_grace_window_waits_for_the_server():
    body = """
  let version = 1;
  network = async (input) => { log.push(String(input)); return jsonResponse({ version: version }); };

  await fetch("/api/sales?page=1");
  const entry = Array.from(cacheStore.values())[0];
  entry.savedAt = Date.now() - 20 * 60 * 1000; // far beyond sales' 15s grace
  version = 2;

  const painted = await bodyOf(await fetch("/api/sales?page=1"));
  check("expired payload is not painted", painted.version === 2);
  check("expired read queried the server", callsTo("/api/sales").length === 2);
"""
    run_cache_tests(body)


def test_single_records_downloads_and_offlist_paths_are_never_cached():
    body = """
  network = async (input) => {
    log.push(String(input));
    const url = String(input);
    if (url.indexOf("/api/products/5") !== -1) return jsonResponse({ id: 5 });
    if (url.indexOf("database_backup") !== -1) {
      return new Response("SQL...", { status: 200, headers: { "content-type": "application/octet-stream" } });
    }
    return jsonResponse({ ok: true });
  };

  await bodyOf(await fetch("/api/products/5"));
  await bodyOf(await fetch("/api/products/5"));
  check("a single product lookup is not cached", callsTo("/api/products/5").length === 2);

  const backupA = await fetch("/api/settings/database_backup");
  const backupB = await fetch("/api/settings/database_backup");
  const backupTextA = await backupA.text();
  const backupTextB = await backupB.text();
  check("a file download is not cached", callsTo("database_backup").length === 2);
  check("the download body still streams through", backupTextA === "SQL..." && backupTextB === "SQL...");

  await fetch("/api/sales/TX-1");
  check("paths off the allowlist pass through", callsTo("/api/sales/TX-1").length === 1);
"""
    run_cache_tests(body)


def run_cache_tests(body):
    """Execute one scenario against a freshly loaded copy of the cache layer."""
    script = PROLOGUE + _cache_source() + EPILOGUE + body + FOOTER
    handle = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    handle.write(script)
    handle.close()
    try:
        completed = subprocess.run([NODE, handle.name], capture_output=True, text=True, timeout=120)
    finally:
        Path(handle.name).unlink(missing_ok=True)
    assert completed.returncode == 0, completed.stderr or completed.stdout
    checks = json.loads(completed.stdout.strip() or "[]")
    assert checks, "the scenario reported no assertions"
    failed = [item["name"] for item in checks if not item["passed"]]
    assert not failed, "failed cache assertions: " + ", ".join(failed)


# --------------------------------------------------------------------------
# Write path: a saved change must never leave stale rows on another tab
# --------------------------------------------------------------------------


def test_successful_write_invalidates_the_groups_it_touches():
    body = """
  let version = 1;
  network = async (input, init) => {
    log.push((init && init.method ? init.method + " " : "GET ") + String(input));
    if (String(input).indexOf("/api/sales") !== -1 && init && init.method === "POST") {
      return jsonResponse({ success: true });
    }
    return jsonResponse({ version: version });
  };

  await bodyOf(await fetch("/api/products?page=1"));   // products group
  await bodyOf(await fetch("/api/sales?page=1"));      // sales group
  await bodyOf(await fetch("/api/users"));             // users group
  version = 2;

  await fetch("/api/sales", { method: "POST", body: "{}" });

  const afterSale = await bodyOf(await fetch("/api/sales?page=1"));
  const productsAfterSale = await bodyOf(await fetch("/api/products?page=1"));
  const usersAfterSale = await bodyOf(await fetch("/api/users"));
  check("selling repaints the sales list", afterSale.version === 2);
  check("selling repaints stock on the products list", productsAfterSale.version === 2);
  check("an unrelated group keeps its cache", usersAfterSale.version === 1);
"""
    run_cache_tests(body)


def test_failed_write_keeps_the_cache_intact():
    body = """
  network = async (input, init) => {
    log.push(String(input));
    if (init && init.method === "POST") {
      return new Response(JSON.stringify({ success: false }), {
        status: 500, statusText: "Internal Server Error",
        headers: { "content-type": "application/json" },
      });
    }
    return jsonResponse({ ok: true });
  };

  await fetch("/api/products?page=1");
  const failed = await fetch("/api/products", { method: "POST", body: "{}" });
  await fetch("/api/products?page=1");

  check("the write was rejected", failed.status === 500);
  check("a rejected write did not wipe the cache", callsTo("page=1").length === 1);
"""
    run_cache_tests(body)


def test_branch_switch_drops_every_cached_row():
    body = """
  network = async (input, init) => {
    log.push(String(input));
    if (init && init.method === "POST") return jsonResponse({ success: true, branch: { id: 2 } });
    return jsonResponse({ rows: 1 });
  };

  currentBranch = { id: 1 };
  await fetch("/api/products?page=1");
  await fetch("/api/users");
  await fetch("/api/categories");
  const beforeSwitch = cacheStore.size;

  currentBranch = { id: 2 };
  await fetch("/api/branches/switch/2", { method: "POST" });

  check("switching branch emptied the cache", cacheStore.size === 0);
  check("every tab now has to repaint", cacheStore.size < beforeSwitch || beforeSwitch === 0);

  await fetch("/api/users");
  check("the new branch gets its own round trip", callsTo("/api/users").length === 2);
"""
    run_cache_tests(body)


def test_same_url_under_another_branch_is_a_different_key():
    body = """
  network = async (input) => { log.push(String(input)); return jsonResponse({ seq: log.length }); };

  currentBranch = { id: 1 };
  const branchOne = await bodyOf(await fetch("/api/products?page=1"));
  currentBranch = { id: 2 };
  const branchTwo = await bodyOf(await fetch("/api/products?page=1"));
  currentBranch = { id: 1 };
  const backToBranchOne = await bodyOf(await fetch("/api/products?page=1"));

  check("each branch queried once", callsTo("/api/products").length === 2);
  check("branch two did not receive branch one's rows", branchTwo.seq === 2);
  check("each branch keeps its own entry", branchOne.seq === 1 && backToBranchOne.seq === 1);
"""
    run_cache_tests(body)


def test_a_write_landing_during_an_inflight_read_wins():
    body = """
  let releaseRead;
  const readGate = new Promise((resolve) => { releaseRead = resolve; });
  let version = 1;
  network = async (input, init) => {
    log.push(String(input));
    if (init && init.method === "POST") { version = 2; return jsonResponse({ success: true }); }
    const queriedVersion = version; // the rows the query actually saw
    await readGate;
    return jsonResponse({ version: queriedVersion });
  };

  const slowRead = fetch("/api/products?page=1"); // pre-write snapshot in flight
  await fetch("/api/products", { method: "POST", body: "{}" }); // the write lands first
  releaseRead();
  const painted = await bodyOf(slowRead); // caller still gets what it asked for

  check("the read still resolved for its caller", painted.version === 1);
  check("the stale in-flight payload was not cached", cacheStore.size === 0);
  const after = await bodyOf(await fetch("/api/products?page=1"));
  check("the next read re-queries and sees the write", after.version === 2);
"""
    run_cache_tests(body)



# --------------------------------------------------------------------------
# Tab (section) render state: the half that stops the DOM churn
# --------------------------------------------------------------------------


def test_a_fresh_tab_is_not_repainted_and_a_stale_one_is():
    body = """
  let renders = 0;
  SECTION_LOADERS.products = () => { renders += 1; };

  check("a tab never visited has to render", sectionNeedsRender("products") === true);
  renderSection("products");
  check("the loader ran once", renders === 1);
  check("a revisit right after is skipped", sectionNeedsRender("products") === false);

  // Age the tab past the products TTL (60s).
  sectionStateOf("products").renderedAt = Date.now() - 61000;
  check("an out-of-date tab repaints", sectionNeedsRender("products") === true);
"""
    run_cache_tests(body)


def test_a_write_repaints_the_tabs_that_show_that_data():
    body = """
  SECTION_LOADERS.products = () => {};
  SECTION_LOADERS.pos = () => {};
  SECTION_LOADERS.users = () => {};
  renderSection("products");
  renderSection("pos");
  renderSection("users");
  check("all three tabs are settled", !sectionNeedsRender("products") && !sectionNeedsRender("pos") && !sectionNeedsRender("users"));

  invalidateProductsCache();

  check("the products tab repaints after a stock change", sectionNeedsRender("products") === true);
  check("the POS grid repaints after a stock change", sectionNeedsRender("pos") === true);
  check("unrelated tabs are left alone", sectionNeedsRender("users") === false);
"""
    run_cache_tests(body)


def test_chart_tabs_always_repaint_when_shown():
    body = """
  SECTION_LOADERS.dashboard = () => {};
  SECTION_LOADERS.settings = () => {};
  renderSection("dashboard");
  renderSection("settings");

  check("the dashboard repaints so its charts re-size", sectionNeedsRender("dashboard") === true);
  check("a plain tab is still skipped while fresh", sectionNeedsRender("settings") === false);
"""
    run_cache_tests(body)


def test_a_sale_forces_every_tab_that_shows_stock_to_repaint():
    body = """
  SECTION_LOADERS.pos = () => {};
  SECTION_LOADERS.products = () => {};
  SECTION_LOADERS.users = () => {};
  network = async () => jsonResponse({ success: true });

  renderSection("pos");
  renderSection("products");
  renderSection("users");

  await fetch("/api/sales", { method: "POST", body: "{}" });

  check("the POS grid repaints after a sale", sectionNeedsRender("pos") === true);
  check("the products list repaints after a sale", sectionNeedsRender("products") === true);
  check("the users tab is untouched", sectionNeedsRender("users") === false);
"""
    run_cache_tests(body)


def test_an_agent_command_drops_every_cache_because_it_can_change_anything():
    body = """
  network = async () => jsonResponse({ ok: true });
  await fetch("/api/products?page=1");
  await fetch("/api/users");
  check("cache is warm", cacheStore.size === 2);

  await fetch("/api/agent/chat", { method: "POST", body: "{}" });
  check("an assistant command clears the cache", cacheStore.size === 0);
"""
    run_cache_tests(body)


def test_saving_a_memory_does_not_clear_business_data():
    body = """
  network = async () => jsonResponse({ ok: true });
  await fetch("/api/products?page=1");
  check("cache is warm", cacheStore.size === 1);

  await fetch("/api/agent/memories", { method: "POST", body: "{}" });
  check("a saved memory leaves business data cached", cacheStore.size === 1);
"""
    run_cache_tests(body)


def test_the_pos_grid_and_the_products_list_are_separate_groups():
    """A Products-tab refresh must not leave a stale POS grid behind, and back.

    Both screens read /api/products, so the group has to depend on the query -
    otherwise refreshing one tab silently does nothing for the other.
    """
    body = """
  network = async (input) => { log.push(String(input)); return jsonResponse({ seq: log.length }); };

  const posGrid = await bodyOf(await fetch("/api/products?view=pos&per_page=50&branch_id=1"));
  const managerList = await bodyOf(await fetch("/api/products?page=1&per_page=10"));
  const groups = Array.from(cacheStore.values()).map((entry) => entry.group).sort();
  check("the two screens land in different groups", groups.join(",") === "pos,products");

  // Refreshing the POS tab (its own groups) must evict the grid payload only.
  invalidateCacheGroups(SECTION_RESOURCES.pos);
  const gridAfterRefresh = await bodyOf(await fetch("/api/products?view=pos&per_page=50&branch_id=1"));
  const listAfterRefresh = await bodyOf(await fetch("/api/products?page=1&per_page=10"));
  check("a POS refresh re-queries the grid", gridAfterRefresh.seq === 3);
  check("a POS refresh leaves the manager list alone", listAfterRefresh.seq === 2);

  // And refreshing the Products tab must not throw away the POS grid.
  invalidateCacheGroups(SECTION_RESOURCES.products);
  const listAfterProductsRefresh = await bodyOf(await fetch("/api/products?page=1&per_page=10"));
  const gridAfterProductsRefresh = await bodyOf(await fetch("/api/products?view=pos&per_page=50&branch_id=1"));
  check("a Products refresh re-queries the list", listAfterProductsRefresh.seq === 4);
  check("a Products refresh leaves the POS grid alone", gridAfterProductsRefresh.seq === 3);
"""
    run_cache_tests(body)


def test_a_product_write_refreshes_both_screens():
    body = """
  network = async (input, init) => {
    log.push(String(input));
    if (init && init.method === "POST") return jsonResponse({ success: true });
    return jsonResponse({ seq: log.length });
  };

  await fetch("/api/products?view=pos&per_page=50&branch_id=1");
  await fetch("/api/products?page=1&per_page=10");
  await fetch("/api/products", { method: "POST", body: "{}" });

  check("the write emptied both product groups", cacheStore.size === 0);
"""
    run_cache_tests(body)


def test_unknown_write_endpoint_conservatively_clears_everything():
    body = """
  network = async (input, init) => {
    log.push(String(input));
    if (init && init.method === "PUT") return jsonResponse({ success: true });
    return jsonResponse({ rows: 1 });
  };

  await fetch("/api/deliveries");
  await fetch("/api/warehouse/summary");
  check("two groups are warm", cacheStore.size === 2);

  await fetch("/api/brand/new_thing", { method: "PUT", body: "{}" });
  check("an unmapped write drops the whole cache", cacheStore.size === 0);
"""
    run_cache_tests(body)


# --------------------------------------------------------------------------
# Static guarantees about the template itself
# --------------------------------------------------------------------------


def _code_without_comments(text):
    """Drop // comment lines so assertions test behaviour, not prose."""
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


def test_boot_does_not_eagerly_load_the_big_tables():
    source = DASHBOARD.read_text(encoding="utf-8")
    start = source.index("// Load only what the boot screen needs")
    boot = _code_without_comments(source[start : source.index("initAutoBarcodeScannerToggle();", start)])
    assert "loadProducts()" not in boot, "boot still loads the whole product table"
    assert "loadSales()" not in boot, "boot still loads sales history"
    assert "showSection(startSection)" in source
    assert "resolveStartSection()" in source

    # The old demo block issued a top-level fetch("/api/users") at parse time.
    script = source[source.index("<script>", source.index("bootstrap.bundle.min.js")) : source.rindex("</script>")]
    assert "\n      fetch(" not in script.replace("\n        fetch(", ""), "a top-level fetch runs on every page load"


def test_tab_switching_goes_through_the_render_state_not_a_load_chain():
    source = DASHBOARD.read_text(encoding="utf-8")
    show_section = _code_without_comments(
        source[source.index("function showSection(sectionId)") : source.index("function checkAuthError")]
    )
    assert "sectionNeedsRender(sectionId)" in show_section
    assert "renderSection(sectionId)" in show_section
    assert "loadProductsForPOS()" not in show_section, "showSection still reloads POS data directly"
    assert show_section.count("sectionId === ") <= 1, "the per-section if/else chain is back"

    refresh = _code_without_comments(
        source[source.index("function refreshCurrentSection()") : source.index("function loadDashboardStats")]
    )
    assert "loadSalesReportData" not in refresh, "refresh calls a function that does not exist"
    assert "invalidateCacheGroups" in refresh
    assert "loadProductsForPOS()" not in refresh, "refresh still has its own per-section chain"


def test_every_tab_resource_group_is_a_real_cache_group():
    source = DASHBOARD.read_text(encoding="utf-8")
    known = set(re.findall(r"^\s*([a-z_]+):\s*\d+,", _block(source, "const CACHE_GROUP_TTL = {", "};"), re.M))
    resources = _block(source, "const SECTION_RESOURCES = {", "};")
    used = set(re.findall(r'"([a-z_]+)"', resources))
    assert used, "SECTION_RESOURCES was not parsed"
    assert used <= known, f"tabs reference unknown cache groups: {sorted(used - known)}"

    loaders = _block(source, "const SECTION_LOADERS = {", "};")
    assert set(re.findall(r"^\s*([a-z_]+):", loaders, re.M)) == set(re.findall(r"^\s*([a-z_]+):", resources, re.M)), (
        "a sidebar tab is missing from one of the two registries"
    )


def test_allowlisted_reads_exclude_single_records_and_downloads():
    body = """
  const paths = Array.from(CACHEABLE_READ_PATHS.keys());

  check("no single-record paths", paths.every((path) => !/[0-9]+$/.test(path)));
  check("no exports or downloads", paths.every((path) => !path.includes("export") && !path.includes("backup")));
  check("product lookups stay live", !CACHEABLE_READ_PATHS.has("/api/products/5"));
  check("receipt printing stays live", !CACHEABLE_READ_PATHS.has("/api/sales/TX-1/print"));
"""
    run_cache_tests(body)


def test_sidebar_clicks_keep_the_remembered_scroll_position():
    """The sidebar anchors are href="#", and the handler must suppress the jump.

    Without preventDefault the browser performs a fragment navigation *after* the
    click handler, scrolling to the top and silently undoing the position
    showSection() just restored. (Found by driving Chrome - a setTimeout re-apply
    loses that race, so the navigation has to be prevented, exactly as the
    pagination links already do.)
    """
    source = DASHBOARD.read_text(encoding="utf-8")
    assert 'href="#"' in source, "sidebar anchors changed shape - revisit the scroll fix"

    nav_guard = _code_without_comments(
        source[source.index("function initSidebarNavLinks()") : source.index("function initSidebarSwipeGestures()")]
    )
    assert "preventDefault()" in nav_guard
    assert 'link.addEventListener("click"' in nav_guard
    assert "initSidebarNavLinks();" in source, "the sidebar nav guard is never installed"

    show_section = _code_without_comments(
        source[source.index("function showSection(sectionId)") : source.index("function checkAuthError")]
    )
    assert "window.scrollTo(0, sectionStateOf(sectionId).scrollY || 0)" in show_section


def test_cache_constants_are_sane():
    body = """
  const groups = Object.keys(CACHE_GROUP_TTL);
  check("every group has a positive ttl", groups.every((group) => CACHE_GROUP_TTL[group] > 0));
  check("volatile groups are not cached for long", CACHE_GROUP_TTL.pos <= 60000 && CACHE_GROUP_TTL.products <= 60000);
  check("reference data may be cached longer", CACHE_GROUP_TTL.categories >= 60000);
  check("stale grace extends past the ttl", CACHE_SHORT_STALE_GRACE_MS > 0 && CACHE_LONG_STALE_GRACE_MS > CACHE_SHORT_STALE_GRACE_MS);
  check("the memory footprint is bounded", CACHE_MAX_ENTRIES > 0 && CACHE_MAX_ENTRIES <= 1000);
"""
    run_cache_tests(body)


def test_every_api_endpoint_the_page_touches_is_accounted_for():
    """Each endpoint is either deliberately cache-free (reads) or has a write rule.

    A write with no rule still falls back to dropping everything, which is safe -
    but the blast radius of every change should stay readable in the table.
    """
    source = DASHBOARD.read_text(encoding="utf-8")
    prefixes = re.findall(
        r'^\s*\["(/api/[a-z_/]+)",', _block(source, "const CACHE_WRITE_INVALIDATIONS = [", "];"), re.M
    )
    assert prefixes, "CACHE_WRITE_INVALIDATIONS was not parsed"
    assert "/api/products" in prefixes and "/api/sales" in prefixes and "/api/branches" in prefixes

    readable = re.findall(
        r'^\s*\["(/api/[a-z_/]+)",', _block(source, "const CACHEABLE_READ_PATHS = new Map([", "]);"), re.M
    )
    assert readable, "CACHEABLE_READ_PATHS was not parsed"

    api_paths = set()
    for match in re.finditer(r'fetch\(\s*[`"](/api/[a-z_/]+)', source):
        api_paths.add(match.group(1).rstrip("/"))
    assert api_paths, "no API paths were found in the template"

    def covered(path, known):
        return any(path == item or path.startswith(item + "/") for item in known)

    unaccounted = [path for path in sorted(api_paths) if not covered(path, prefixes) and not covered(path, readable)]
    assert not unaccounted, f"endpoints with neither a cache rule nor an invalidation rule: {unaccounted}"


