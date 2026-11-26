"""Startup indicator reads the saved branch setting independently of product downloads."""

from pathlib import Path

from test_offline_sync_javascript import _function, _run_node_script, pytestmark


SOURCE = (Path(__file__).parent / "templates" / "dashboard.html").read_text(encoding="utf-8")


def run_refresh(body):
    helpers = "\n".join(_function(SOURCE, name) for name in (
        "refreshAutomatedOfflineFetchOnStartup", "bindCurrentBranchUI", "applyCachedCurrentBranch",
    ))
    return _run_node_script(r"""
const assert = require('node:assert/strict');
let currentBranch = null, currentSection = 'pos';
let dashboardBranchStale = false, dashboardSessionStale = false;
let startupSettingsRequest = null;
let automatedOfflineFetchBranchId = null, automatedOfflineFetchEnabled = false;
const APP_SETTINGS = {};
const CURRENT_BRANCH_CACHE_KEY = 'current';
const nodes = {
  'pos-offline-mode': {textContent: 'Automated offline fetch: Off'},
  'settings-automated-offline-fetch': {value: 'off'},
};
const document = {getElementById: key => nodes[key] || null};
let response, requests = [], catalogChecks = 0, cache = null, cachedBranch = null, settingsWrites = [];
function fetch(url, options) {
  requests.push([url, options.cache]);
  return response();
}
function offlineCacheGet(key) { return key === CURRENT_BRANCH_CACHE_KEY ? cachedBranch : cache; }
function offlineCacheSet(key, data) { settingsWrites.push([key, data]); }
function settingsCacheKey(id) { return `settings:${id}`; }
function updateBranchIndicators() {}
function toggleReportScopeControls() {}
function updateBranchSaveButton() {}
function hasCapability() { return false; }
function checkOfflineProductCatalog() { catalogChecks++; }
function cachedProductsKey() { throw Error('Product cache must not be accessed'); }
""" + helpers + "\n(async () => {\n" + body + "\nconsole.log(JSON.stringify({passed: true}));\n})().catch(e => { console.error(e); process.exit(1); });")


def test_branch_binding_refreshes_status_when_early_settings_response_was_discarded():
    assert run_refresh("""
response = async () => ({ok: true, json: async () => ({branch_id: 7, automated_offline_fetch: true})});
currentBranch = {id: 7};
bindCurrentBranchUI(currentBranch);
await new Promise(resolve => setTimeout(resolve, 0));
assert.deepEqual(requests, [['/api/settings', 'no-store']]);
assert.equal(nodes['pos-offline-mode'].textContent, 'Automated offline fetch: On');
assert.equal(nodes['settings-automated-offline-fetch'].value, 'on');
assert.equal(APP_SETTINGS.automatedOfflineFetch, true);
assert.equal(automatedOfflineFetchBranchId, 7);
assert.equal(automatedOfflineFetchEnabled, true);
assert.deepEqual(settingsWrites, [['settings:7', {branch_id: 7, automated_offline_fetch: true}]]);
assert.equal(catalogChecks, 1); // Only the existing branch check runs.
""")["passed"]


def test_failed_request_uses_same_branch_cached_setting_without_touching_catalog():
    assert run_refresh("""
currentBranch = {id: 7};
cache = {data: {branch_id: 7, automated_offline_fetch: true}};
response = async () => ({ok: false, status: 503});
const warn = console.warn;
console.warn = () => {};
await refreshAutomatedOfflineFetchOnStartup();
console.warn = warn;
assert.equal(nodes['pos-offline-mode'].textContent, 'Automated offline fetch: On');
assert.equal(catalogChecks, 0);
assert.equal(automatedOfflineFetchEnabled, false); // Cached value is display-only.
assert.equal(settingsWrites.length, 0);
""")["passed"]


def test_stale_response_cannot_change_another_branch_indicator():
    assert run_refresh("""
currentBranch = {id: 7};
let release;
response = () => new Promise(resolve => { release = resolve; });
const pending = refreshAutomatedOfflineFetchOnStartup();
await Promise.resolve();
currentBranch = {id: 8};
release({ok: true, json: async () => ({branch_id: 7, automated_offline_fetch: true})});
await pending;
assert.equal(nodes['pos-offline-mode'].textContent, 'Automated offline fetch: Off');
assert.equal(APP_SETTINGS.automatedOfflineFetch, undefined);
""")["passed"]


def test_refresh_waits_for_initial_settings_failure_before_painting_saved_value():
    assert run_refresh("""
currentBranch = {id: 7};
let finishStartup;
startupSettingsRequest = new Promise(resolve => { finishStartup = resolve; });
response = async () => ({ok: true, json: async () => ({branch_id: 7, automated_offline_fetch: true})});
const pending = refreshAutomatedOfflineFetchOnStartup();
await Promise.resolve();
assert.equal(requests.length, 0);
finishStartup();
await pending;
assert.deepEqual(requests, [['/api/settings', 'no-store']]);
assert.equal(nodes['pos-offline-mode'].textContent, 'Automated offline fetch: On');
""")["passed"]


def test_cached_branch_resolution_refreshes_indicator_after_branch_endpoint_failure():
    assert run_refresh("""
cachedBranch = {data: {id: 7, name: 'Shop'}};
response = async () => ({ok: true, json: async () => ({branch_id: 7, automated_offline_fetch: true})});
applyCachedCurrentBranch();
await new Promise(resolve => setTimeout(resolve, 0));
assert.deepEqual(requests, [['/api/settings', 'no-store']]);
assert.equal(nodes['pos-offline-mode'].textContent, 'Automated offline fetch: On');
assert.equal(catalogChecks, 1);
""")["passed"]


def test_saved_off_mode_replaces_previous_on_indicator():
    assert run_refresh("""
currentBranch = {id: 7};
nodes['pos-offline-mode'].textContent = 'Automated offline fetch: On';
APP_SETTINGS.automatedOfflineFetch = true;
response = async () => ({ok: true, json: async () => ({branch_id: 7, automated_offline_fetch: false})});
await refreshAutomatedOfflineFetchOnStartup();
assert.equal(nodes['pos-offline-mode'].textContent, 'Automated offline fetch: Off');
assert.equal(APP_SETTINGS.automatedOfflineFetch, false);
assert.equal(catalogChecks, 0);
""")["passed"]


def test_late_startup_response_does_not_undo_successful_save():
    assert run_refresh("""
currentBranch = {id: 7};
APP_SETTINGS.automatedOfflineFetch = false;
let release;
response = () => new Promise(resolve => { release = resolve; });
const pending = refreshAutomatedOfflineFetchOnStartup();
await Promise.resolve();
APP_SETTINGS.automatedOfflineFetch = true;
automatedOfflineFetchEnabled = true;
nodes['pos-offline-mode'].textContent = 'Automated offline fetch: On';
release({ok: true, json: async () => ({branch_id: 7, automated_offline_fetch: false})});
await pending;
assert.equal(nodes['pos-offline-mode'].textContent, 'Automated offline fetch: On');
assert.equal(automatedOfflineFetchEnabled, true);
assert.equal(catalogChecks, 0);
""")["passed"]


def test_failed_refresh_does_not_override_confirmed_off_with_stale_cached_on():
    assert run_refresh("""
currentBranch = {id: 7};
APP_SETTINGS.automatedOfflineFetch = false;
cache = {data: {branch_id: 7, automated_offline_fetch: true}};
response = async () => ({ok: false, status: 503});
console.warn = () => {};
await refreshAutomatedOfflineFetchOnStartup();
assert.equal(nodes['pos-offline-mode'].textContent, 'Automated offline fetch: Off');
assert.equal(APP_SETTINGS.automatedOfflineFetch, false);
""")["passed"]