"""Check every tab and real Chart.js canvases in local Chrome without POS data."""
import subprocess
from pathlib import Path

import pytest

from test_delivery_actions_browser import BROWSER
from test_offline_sync_javascript import _function
from test_section_cache_javascript import _cache_source

ROOT = Path(__file__).parent


@pytest.mark.skipif(not BROWSER, reason="Local Chrome/Chromium/Edge required")
def test_tab_revisits_keep_content_and_charts_resize_in_a_browser(tmp_path):
    source = (ROOT / "templates/dashboard.html").read_text(encoding="utf-8")
    helpers = "\n".join(_function(source, name) for name in (
        "showSection", "isOfflineNavigationProtected", "initOfflineNavigationProtection",
    ))
    chart_library = (ROOT / "public/vendor/chartjs/chart.umd.min.js").read_text(encoding="utf-8")
    script = r"""
let currentBranch = null, currentSection = 'products', offline = false;
let checkoutServerUnavailable = false;
const ACTIVE_SECTION_STORAGE_KEY = 'tab-cache-browser-test';
function isBrowserOffline() { return offline; }
function hasCapability() { return true; }
function requireCapability() { return true; }
function isAutoBarcodeScannerDetectionEnabled() { return false; }
function showToast() {}
let dashboardChart = null, salesChart = null, paymentChart = null;
function check(value, message) { if (!value) throw Error(message); }
function chart(id) {
  const canvas = document.createElement('canvas');
  document.getElementById(id + '-section').append(canvas);
  return new Chart(canvas, {type:'bar', data:{labels:['One'],datasets:[{data:[2]}]},
    options:{animation:false,responsive:true,maintainAspectRatio:false}});
}
try {
  const loads = {};
  for (const section of Object.keys(SECTION_LOADERS)) {
    const pane = document.createElement('section');
    pane.id = section + '-section';
    pane.style.cssText = 'display:none;width:600px;height:250px';
    document.body.append(pane);
    SECTION_LOADERS[section] = () => {
      loads[section] = (loads[section] || 0) + 1;
      pane.dataset.content = 'loaded';
      if (section === 'dashboard') dashboardChart = chart(section);
      if (section === 'reports') salesChart = chart(section);
    };
  }
  for (let visit = 0; visit < 2; visit++) {
    for (const section of Object.keys(SECTION_LOADERS)) showSection(section);
  }
  check(Object.values(loads).every(count => count === 1), 'Every fresh tab must load once');
  const originalDashboard = dashboardChart, originalSales = salesChart;
  showSection('dashboard');
  check(dashboardChart === originalDashboard && dashboardChart.width > 0, 'Dashboard chart must resize without rebuilding');
  showSection('reports');
  check(salesChart === originalSales && salesChart.width > 0, 'Reports chart must resize without rebuilding');
  offline = true;
  for (const section of Object.keys(SECTION_LOADERS)) {
    sectionStateOf(section).renderedAt = Date.now() - 3600000;
    showSection(section);
  }
  check(Object.values(loads).every(count => count === 1), 'Offline expiry must preserve every loaded tab');
  initOfflineNavigationProtection();
  const reload = new KeyboardEvent('keydown', {key:'r',ctrlKey:true,cancelable:true,bubbles:true});
  window.dispatchEvent(reload);
  check(reload.defaultPrevented, 'Offline reload shortcut must be prevented');
  offline = false;
  const online = new KeyboardEvent('keydown', {key:'F5',cancelable:true});
  window.dispatchEvent(online);
  check(!online.defaultPrevented, 'Online refresh must remain available');
  localStorage.removeItem(ACTIVE_SECTION_STORAGE_KEY);
  document.body.dataset.testResult = 'passed';
} catch (error) {
  document.body.dataset.testResult = 'failed';
  document.body.append(error.stack);
}
"""
    page = tmp_path / "tab-cache.html"
    page.write_text(
        '<!doctype html><html><body>'
        f'<script>{chart_library}</script><script>{_cache_source()}\n{helpers}\n{script}</script>'
        '</body></html>', encoding="utf-8",
    )
    result = subprocess.run([
        BROWSER, "--headless", "--disable-gpu", "--no-sandbox",
        f"--user-data-dir={tmp_path / 'profile'}", "--virtual-time-budget=3000",
        "--dump-dom", page.as_uri(),
    ], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert 'data-test-result="passed"' in result.stdout, result.stdout[-6000:] + result.stderr[-1000:]