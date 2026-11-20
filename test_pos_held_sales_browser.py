"""Exercise the real checkout controls in Chromium at narrow and wide cart widths."""
import re
import subprocess

import pytest

from test_delivery_actions_browser import BROWSER
from test_offline_sync_javascript import DASHBOARD, _function


@pytest.mark.skipif(not BROWSER, reason="Local Chrome/Chromium/Edge required")
@pytest.mark.parametrize("cart_width", [270, 400])
def test_cashier_holds_and_resumes_sale_using_visible_controls(tmp_path, cart_width):
    source = DASHBOARD.read_text(encoding="utf-8")
    styles = re.search(r"<style>(.*?)</style>", source, re.S).group(1)
    desk = source.split('<div class="pos-hold-desk">', 1)[1].split('<hr />', 1)[0]
    module = source.split("// ===== POS HELD SALES =====", 1)[1].split("// ===== END POS HELD SALES =====", 1)[0]
    helpers = "\n".join(_function(source, name) for name in (
        "posCartScopeCurrent", "ensurePOSCustomerOption", "togglePosDeliveryFields",
    ))
    fields = "".join(f'<input id="{field}">' for field in (
        "product-search", "cash-received", "payment-method", "pos-enable-delivery",
    ))
    script = r"""
const CACHE_USER_ID = 7, currentBranch = {id:1};
const dashboardSessionStale = false, dashboardBranchStale = false, branchSwitchInFlight = false;
const isSaleProcessing = false, checkoutPreflight = false;
let cart = [{name:'Family-size Jasmine Rice', quantity:4}], appliedCoupon = null;
const get = id => document.getElementById(id);
function showToast() {}
function resetAppliedCoupon() { appliedCoupon = null; }
function resetPOSCustomer() { get('pos-customer-select').value = ''; }
function resetSplitPaymentFields() {}
function clearLegacyMMQRDeadline() {}
function updateCartDisplay() {}
function handlePaymentMethodChange() {}
function check(ok, message) { if (!ok) throw Error(message); }
try {
  renderPOSHeldSales();
  const hold = get('pos-hold-sale'), toggle = get('pos-held-sales-toggle');
  const panel = get('pos-held-sales'), count = get('pos-held-sales-count');
  check(panel.hidden && toggle.getAttribute('aria-expanded') === 'false', 'Panel starts collapsed');
  for (const button of [hold, toggle]) check(button.getBoundingClientRect().height >= 44, 'Touch target too small');
  hold.click();
  check(hold.disabled && !cart.length && count.textContent === '1' && count.hasAttribute('data-waiting'), 'Hold starts a new sale');
  toggle.click();
  check(!panel.hidden && toggle.getAttribute('aria-expanded') === 'true', 'Toggle opens the queue');
  const resume = panel.querySelector('.pos-held-resume');
  check(resume.getBoundingClientRect().height >= 44, 'Resume touch target too small');
  check(panel.scrollWidth <= panel.clientWidth, 'Held sales must fit in the cart');
  check(panel.textContent.includes('Family-size Jasmine Rice'), 'Ticket identifies the sale');
  resume.click();
  check(cart[0]?.name === 'Family-size Jasmine Rice' && count.textContent === '0' && panel.hidden, 'Resume restores the cart');
  check(document.activeElement === get('product-search'), 'Return focus to scanning');
  document.body.dataset.testResult = 'passed';
} catch (error) { document.body.dataset.testResult = 'failed'; document.body.append(error.stack); }
"""
    bootstrap = (DASHBOARD.parent.parent / "public/vendor/bootstrap/bootstrap.min.css").read_text(encoding="utf-8")
    page = tmp_path / "hold.html"
    page.write_text(
        f'<!doctype html><meta charset="utf-8"><style>{bootstrap}\n{styles}</style>'
        f'<main id="pos-section" style="width:{cart_width}px"><div class="pos-hold-desk">{desk}'
        f'{fields}<select id="pos-customer-select"><option value="">Walk-in</option></select></main>'
        f'<script>{module}\n{helpers}\n{script}</script>', encoding="utf-8",
    )
    result = subprocess.run([
        BROWSER, "--headless", "--disable-gpu", "--no-sandbox",
        f"--user-data-dir={tmp_path / 'profile'}", "--dump-dom", page.as_uri(),
    ], capture_output=True, text=True, encoding="utf-8", timeout=25)
    assert 'data-test-result="passed"' in result.stdout, result.stdout[-4000:] + result.stderr[-1000:]