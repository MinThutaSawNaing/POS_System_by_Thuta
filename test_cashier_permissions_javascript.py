"""Behavioral cashier permission regressions executing dashboard code in Node."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_offline_sync_javascript import _function, _run_node_script

DASHBOARD = Path(__file__).parent / "templates" / "dashboard.html"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
CASHIER_ALLOWED = ["pos", "products", "categories", "sales", "returns", "deliveries", "reports"]
MANAGER_ONLY = ["dashboard", "promotions", "customers", "debts", "suppliers", "purchases",
                "warehouse", "users", "logs", "settings"]
PRIVILEGED_ACTIONS = ["catalog.write", "deliveries.write", "labels", "debts", "branches.switch", "admin"]


def run_js(body, functions=(), role="cashier", setup=""):
    # Read afresh to pick up ongoing dashboard edits.
    source = DASHBOARD.read_text(encoding="utf-8")
    constants = []
    for name in ("CASHIER_SECTIONS", "MANAGER_CAPABILITIES", "SECTION_LOADERS"):
        ending = r"\n\s*\};" if name == "SECTION_LOADERS" else ";"
        match = re.search(rf"const {name} = .*?{ending}", source, re.S)
        assert match, f"missing {name}"
        constants.append(match.group())
    loader_calls = re.findall(r"\b(\w+)\(\)", constants[-1])
    stubs = "\n".join(
        f"function {name}() {{ loaders.push({json.dumps(name)}); }}"
        for name in sorted(set(loader_calls)) if name not in functions
    )
    helpers = "\n".join(_function(source, name) for name in dict.fromkeys((
        "hasManagerOrBossAccess", "hasCapability", "requireCapability", *functions,
    )))
    constant_source = "\n".join(constants)
    script = f"""
let CURRENT_USER_ROLE = {json.dumps(role)};
let currentSection = 'pos', currentBranch = {{id: 7}}, currentProduct = {{id: 42}};
const requests = [], toasts = [], loaders = [], storageWrites = [];
let domTouches = 0;
function showToast(message, kind) {{ toasts.push({{message, kind}}); }}
let document = new Proxy({{}}, {{get() {{
  domTouches++;
  throw new Error('DOM accessed before permission check');
}}}});
const window = {{scrollY: 0}};
const ACTIVE_SECTION_STORAGE_KEY = 'pos_active_section';
let savedSection = null;
const localStorage = {{getItem: () => savedSection,
  setItem: (...args) => storageWrites.push(args)}};
function fetch(url, options) {{
  requests.push({{url, method: options?.method || 'GET'}});
  return Promise.resolve({{ok: true, json: async () => []}});
}}
function sectionNeedsRender() {{ loaders.push('sectionNeedsRender'); return true; }}
function sectionStateOf() {{ loaders.push('sectionStateOf'); return {{}}; }}
function sectionResourcesOf() {{ return []; }}
function cacheGroupEpoch() {{ return 0; }}
function showConfirmDialog() {{ loaders.push('confirmation'); }}
function clearBarcodeLabelSearch() {{ loaders.push('barcodeSearch'); }}
{stubs}
{constant_source}
{helpers}
const tick = () => new Promise(resolve => setTimeout(resolve, 10));
{setup}
(async () => {{
{body}
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    try:
        return _run_node_script(script)
    except subprocess.CalledProcessError as error:
        pytest.fail(f"Dashboard JavaScript failed:\n{error.stderr}")


@pytest.mark.parametrize("role", ["cashier", "manager", "boss"])
def test_section_allowlist_and_privileged_capabilities(role):
    capabilities = [f"section.{name}" for name in CASHIER_ALLOWED + MANAGER_ONLY]
    capabilities += PRIVILEGED_ACTIONS
    out = run_js(
        f"console.log(JSON.stringify({json.dumps(capabilities)}.map(hasCapability)));", role=role,
    )
    expected = [True] * len(CASHIER_ALLOWED)
    expected += [role != "cashier"] * (len(MANAGER_ONLY) + len(PRIVILEGED_ACTIONS))
    assert out == expected


@pytest.mark.parametrize("role", ["cashier", "manager", "boss"])
def test_unknown_sections_and_capabilities_are_denied_even_for_managers(role):
    out = run_js("""
console.log(JSON.stringify(['section.unknown', 'section.toString',
  'section.__proto__', 'unknown', 'catalog.unknown'].map(requireCapability)));
""", role=role)
    assert out == [False] * 5


def test_require_capability_allows_pos_but_warns_on_admin_action():
    out = run_js("""
const allowed = requireCapability('section.pos');
const denied = requireCapability('catalog.write');
console.log(JSON.stringify({allowed, denied, toasts, domTouches, requests}));
""")
    assert out["allowed"] is True
    assert out["denied"] is False
    assert len(out["toasts"]) == 1
    assert out["toasts"][0]["kind"] == "warning"
    assert "manager" in out["toasts"][0]["message"].lower()
    assert out["domTouches"] == 0
    assert out["requests"] == []



@pytest.mark.parametrize("saved", [None, "pos", "products", "sales", "settings", "dashboard", "logs", "unknown"])
def test_cashier_always_starts_at_pos_despite_saved_navigation(saved):
    out = run_js(
        f"savedSection = {json.dumps(saved)}; console.log(JSON.stringify(resolveStartSection()));",
        functions=("resolveStartSection",),
    )
    assert out == "pos"


@pytest.mark.parametrize("section", MANAGER_ONLY + ["unknown", "toString"])
def test_show_section_denies_before_dom_storage_or_loader(section):
    out = run_js(f"""
showSection({json.dumps(section)});
await tick();
console.log(JSON.stringify({{domTouches, requests, loaders, storageWrites, currentSection}}));
""", functions=("showSection",))
    assert out == {"domTouches": 0, "requests": [], "loaders": [],
                   "storageWrites": [], "currentSection": "pos"}


@pytest.mark.parametrize("section", MANAGER_ONLY + ["unknown", "toString"])
def test_render_section_denies_without_invoking_loader_or_render_state(section):
    out = run_js(f"""
const result = renderSection({json.dumps(section)});
console.log(JSON.stringify({{result, loaders, domTouches, requests}}));
""", functions=("renderSection",))
    assert out == {"result": False, "loaders": [], "domTouches": 0, "requests": []}


@pytest.mark.parametrize("role,section,expected_loaders", [
    ("cashier", "products", ["loadProducts"]),
    ("manager", "users", ["loadUsers"]),
    ("boss", "users", ["loadUsers"]),
])
def test_render_section_still_loads_authorized_sections(role, section, expected_loaders):
    out = run_js(f"""
const result = renderSection({json.dumps(section)});
console.log(JSON.stringify({{result, loaders}}));
""", functions=("renderSection",), role=role)
    assert out == {"result": True, "loaders": expected_loaders + ["sectionStateOf"]}


@pytest.mark.parametrize("handler,args", [
    ("saveProduct", ""), ("updateProduct", ""), ("editProduct", "42"),
    ("saveCategory", ""), ("updateCategory", ""), ("editCategory", "42"),
    ("deleteCategory", "42, 'Example'"), ("showBarcodeLabelDialog", ""),
    ("generateBarcodeLabels", ""), ("loadProductsForBarcodeLabels", ""),
    ("switchBranch", "8"),
    ("openCreateDeliveryModal", ""), ("saveDelivery", ""),
    ("openUpdateDeliveryModal", "42"), ("updateDelivery", ""),
    ("advanceDeliveryStage", "42, 'packaged'"),
])
def test_direct_manager_handlers_deny_before_network_dom_or_dialog(handler, args):
    out = run_js(f"""
await {handler}({args});
await tick();
console.log(JSON.stringify({{domTouches, requests, loaders, currentBranch}}));
""", functions=(handler,))
    assert out == {"domTouches": 0, "requests": [], "loaders": [], "currentBranch": {"id": 7}}


@pytest.mark.parametrize("role", ["cashier", "manager", "boss"])
def test_settings_get_remains_accessible_but_ai_settings_are_manager_only(role):
    out = run_js("""
loadSettings();
await tick();
console.log(JSON.stringify({requests, aiLoads, applied, APP_SETTINGS}));
""", functions=("loadSettings",), role=role, setup="""
let aiLoads = 0, applied = 0;
const APP_SETTINGS = {posName: 'Old', currencyCode: 'MMK', currencySuffix: 'Ks'};
const SETTINGS_CACHE_KEY = 'settings';
function currencySuffixFromCode() { return 'Ks'; }
function applySettingsUI() { applied++; }
function offlineCacheSet() {}
function offlineCacheGet() { return null; }
function loadAISettings() { aiLoads++; }
fetch = async (url, options) => {
  requests.push({url, method: options?.method || 'GET'});
  return {ok: true, json: async () => ({pos_name: 'Shop', receipt_paper_size: '80mm'})};
};
""")
    assert out["requests"] == [{"url": "/api/settings", "method": "GET"}]
    assert out["aiLoads"] == (0 if role == "cashier" else 1)
    assert out["applied"] == 1
    assert out["APP_SETTINGS"]["posName"] == "Shop"
    assert out["APP_SETTINGS"]["receiptPaperSize"] == "80mm"


@pytest.mark.parametrize("role", ["cashier", "manager", "boss"])
def test_branch_selector_uses_only_current_branch_for_cashier(role):
    out = run_js("""
loadBranchSelector();
await tick();
console.log(JSON.stringify({requests, currentBranch, bound, populated}));
""", functions=("loadBranchSelector",), role=role, setup="""
let branchesList = [], bound = [], populated = 0;
const BRANCHES_CACHE_KEY = 'branches', CURRENT_BRANCH_CACHE_KEY = 'current';
function offlineCacheSet() {}
function offlineCacheGet() { return null; }
function populateBranchSelectors() { populated++; }
function bindCurrentBranchUI(branch) { bound.push(branch.id); }
function applyCachedCurrentBranch() { throw new Error('Unexpected offline fallback'); }
fetch = async (url, options) => {
  requests.push({url, method: options?.method || 'GET'});
  return {ok: true, json: async () => url === '/api/branches/current'
    ? {id: 7, name: 'Assigned branch', is_active: true}
    : [{id: 7, name: 'Assigned branch', is_active: true}]};
};
""")
    expected = ["/api/branches/current"] if role == "cashier" else ["/api/branches", "/api/branches/current"]
    assert out["requests"] == [{"url": url, "method": "GET"} for url in expected]
    assert out["currentBranch"]["id"] == 7
    assert out["bound"] == [7]


@pytest.mark.parametrize("role", ["cashier", "manager", "boss"])
def test_pos_customer_loader_uses_login_accessible_pos_endpoint(role):
    out = run_js("""
loadCustomersForPOS();
await tick();
console.log(JSON.stringify({requests, options: select.options, selected: select.value,
  warningHidden: warning.hidden}));
""", functions=("loadCustomersForPOS", "posResponseJSON"), role=role, setup="""
const select = {value: '23', innerHTML: '', options: [],
  appendChild(option) { this.options.push(option); }};
const warning = {hidden: false};
document = {getElementById: id => id === 'pos-customer-select' ? select : warning,
  createElement: () => ({})};
fetch = async (url, options) => {
  requests.push({url, method: options?.method || 'GET'});
  return {ok: true, status: 200, json: async () => [{id: 23, name: 'Customer', phone: '123'}]};
};
""")
    assert out["requests"] == [{"url": "/api/pos/customers", "method": "GET"}]
    assert out["options"] == [{"value": 23, "textContent": "Customer (123)"}]
    assert out["selected"] == "23"
    assert out["warningHidden"] is True
