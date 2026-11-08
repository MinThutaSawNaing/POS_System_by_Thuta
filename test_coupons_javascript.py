"""Frontend contract tests for checkout coupons (UI wiring + cart money math)."""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

DASHBOARD = Path(__file__).parent / "templates" / "dashboard.html"
NODE = shutil.which("node")


def _function(source, name):
    match = re.search(rf"function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", source)
    assert match, f"dashboard function {name} was not found"
    depth = 0
    for index in range(match.end() - 1, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[match.start():index + 1]
    raise AssertionError(f"dashboard function {name} has an unbalanced body")


class CouponDashboardTests(unittest.TestCase):
    def setUp(self):
        with open(DASHBOARD, encoding="utf-8") as handle:
            self.source = handle.read()

    def test_coupon_management_ui_is_wired_into_the_promotions_tab(self):
        for marker in (
            'id="coupons-table"',
            'id="coupons-pagination"',
            'id="coupons-search"',
            'id="addCouponModal"',
            'id="coupon-code"',
            'id="coupon-kind"',
            'id="coupon-usage-limit"',
            'id="coupon-min-subtotal"',
            'data-bs-target="#addCouponModal"',
            'onclick="prepareNewCoupon()"',
            'onclick="saveCoupon()"',
        ):
            self.assertIn(marker, self.source, f"missing coupon UI marker: {marker}")
        for function_name in (
            "loadCoupons", "prepareNewCoupon", "saveCoupon", "editCoupon",
            "deleteCoupon", "handleCouponKindChange",
        ):
            self.assertIn(f"function {function_name}(", self.source)
        self.assertIn(
            'promotions: () => { loadPromotions(); loadProductsForPromotionModal(); loadCoupons(); }',
            self.source,
        )
        self.assertIn('bindReload("coupons-search", loadCoupons, "coupons")', self.source)

    def test_checkout_coupon_fields_and_handlers_exist(self):
        for marker in (
            'id="pos-coupon-code"',
            'id="pos-coupon-apply"',
            'id="pos-coupon-remove"',
            'id="pos-coupon-feedback"',
            'id="pos-coupon-discount-row"',
            'onclick="applyCouponCode()"',
            'onclick="removeCouponCode()"',
            # Typing a new code drops the previous message instead of leaving a
            # stale warning pinned under the field.
            'oninput="clearCouponFeedback()"',
            'aria-describedby="pos-coupon-feedback"',
        ):
            self.assertIn(marker, self.source, f"missing checkout coupon marker: {marker}")
        for function_name in (
            "applyCouponCode", "removeCouponCode", "resetAppliedCoupon",
            "updateCouponUi", "computeCouponDiscountCents", "getCartSubtotalCents",
            "clearCouponFeedback",
        ):
            self.assertIn(f"function {function_name}(", self.source)
        # The sale payload must carry the applied code to the server.
        self.assertIn("saleData.coupon_code = couponCode;", self.source)

    def test_api_paths_are_accounted_for_in_the_cache_tables(self):
        self.assertIn('["/api/coupons", "promotions"]', self.source)
        self.assertIn('["/api/coupons", ["promotions", "pos"]]', self.source)


if NODE:
    class CouponCartMoneyTests(unittest.TestCase):
        def _run(self, cart, applied_coupon_js):
            with open(DASHBOARD, encoding="utf-8") as handle:
                source = handle.read()
            functions = "\n".join(
                _function(source, name)
                for name in ("toCents", "centsToNumber", "mulMoney",
                             "computeCouponDiscountCents", "getCartSubtotalCents",
                             "getCurrentCartTotal")
            )
            script = f"""
{functions}
let cart = {json.dumps(cart)};
let appliedCoupon = {applied_coupon_js};
console.log(JSON.stringify({{
  subtotalCents: getCartSubtotalCents(),
  discountCents: computeCouponDiscountCents(getCartSubtotalCents()),
  total: getCurrentCartTotal(),
}}));
"""
            result = subprocess.run(
                [NODE, "-e", script], check=True, capture_output=True, text=True,
            )
            return json.loads(result.stdout)

        def test_percent_coupon_reduces_the_cart_total(self):
            out = self._run(
                [{"product_id": 1, "price": 100, "quantity": 1, "tax_rate": 0}],
                '{code: "SAVE10", discountType: "percent", discountValue: 10, minSubtotalCents: 0}',
            )
            self.assertEqual(out, {"subtotalCents": 10000, "discountCents": 1000, "total": 90.0})

        def test_fixed_coupon_is_capped_at_the_subtotal(self):
            out = self._run(
                [{"product_id": 1, "price": 30, "quantity": 1, "tax_rate": 0}],
                '{code: "TAKE50", discountType: "fixed", discountValue: 50, minSubtotalCents: 0}',
            )
            self.assertEqual(out, {"subtotalCents": 3000, "discountCents": 3000, "total": 0.0})

        def test_tax_is_added_before_the_coupon_is_subtracted(self):
            out = self._run(
                [{"product_id": 1, "price": 100, "quantity": 1, "tax_rate": 5}],
                '{code: "SAVE10", discountType: "percent", discountValue: 10, minSubtotalCents: 0}',
            )
            # 10% of the 100.00 subtotal, then 5.00 tax is still charged.
            self.assertEqual(out, {"subtotalCents": 10000, "discountCents": 1000, "total": 95.0})

        def test_without_a_coupon_the_total_is_unchanged(self):
            out = self._run(
                [{"product_id": 1, "price": 100, "quantity": 2, "tax_rate": 0}],
                "null",
            )
            self.assertEqual(out, {"subtotalCents": 20000, "discountCents": 0, "total": 200.0})

        def _run_apply(self, response_body, ok=True):
            source = DASHBOARD.read_text(encoding="utf-8")
            functions = _function(source, "applyCouponCode")
            script = f"""
const assert = require('node:assert/strict');
let appliedCoupon = null;
const calls = [];
const feedback = [];
const elements = {{
  'pos-coupon-code': {{value: ' save10 ', disabled: false}},
  'pos-coupon-apply': {{disabled: false}},
}};
const document = {{getElementById: (id) => elements[id] || null}};
function getCartSubtotalCents() {{ return 10000; }}
function setCouponFeedback(message, kind) {{ feedback.push([message, kind]); }}
function updateCartDisplay() {{ calls.push('redraw'); }}
async function fetch(url, options) {{
  calls.push({{url, body: JSON.parse(options.body), method: options.method}});
  return {{ok: {str(ok).lower()}, json: async () => ({json.dumps(response_body)})}};
}}
{functions}
(async () => {{
  applyCouponCode();
  await new Promise(setImmediate);
  console.log(JSON.stringify({{ok: true, calls, feedback,
    appliedCoupon: appliedCoupon,
    inputValue: elements['pos-coupon-code'].value,
    applyDisabled: elements['pos-coupon-apply'].disabled}}));
}})().catch(e => {{console.error(e);process.exit(1)}});
"""
            result = subprocess.run([NODE, "-e", script], check=True, capture_output=True, text=True)
            return json.loads(result.stdout)

        def test_apply_coupon_validates_then_stores_the_discount_rules(self):
            out = self._run_apply({
                "success": True, "code": "SAVE10", "discount_type": "percent",
                "discount_value": 10, "discount": "1000.00", "min_subtotal": 0,
            })
            self.assertEqual(out["calls"][0]["url"], "/api/coupons/validate")
            self.assertEqual(out["calls"][0]["method"], "POST")
            self.assertEqual(out["calls"][0]["body"], {"code": "SAVE10", "subtotal": 100})
            self.assertEqual(out["appliedCoupon"]["code"], "SAVE10")
            self.assertEqual(out["appliedCoupon"]["discountType"], "percent")
            self.assertEqual(out["appliedCoupon"]["discountValue"], 10)
            self.assertEqual(out["inputValue"], "SAVE10")
            self.assertFalse(out["applyDisabled"])

        def test_apply_coupon_surfaces_the_server_rejection_reason(self):
            out = self._run_apply({"success": False, "message": "Coupon expired"}, ok=False)
            self.assertIsNone(out["appliedCoupon"])
            self.assertEqual(out["feedback"][-1][0], "Coupon expired")
            self.assertEqual(out["feedback"][-1][1], "danger")

        def _run_functions(self, names, body):
            source = DASHBOARD.read_text(encoding="utf-8")
            functions = "\n".join(_function(source, name) for name in names)
            result = subprocess.run(
                [NODE, "-e", functions + "\n" + body],
                check=True, capture_output=True, text=True,
            )
            return json.loads(result.stdout)

        def test_feedback_line_collapses_when_the_message_is_cleared(self):
            out = self._run_functions(("setCouponFeedback",), """
const element = {textContent: '', className: 'small mt-1 d-none'};
const document = {getElementById: () => element};
setCouponFeedback('Coupon expired', 'danger');
const shown = {text: element.textContent, className: element.className};
setCouponFeedback('', '');
console.log(JSON.stringify({shown, cleared: {text: element.textContent,
  className: element.className}}));
""")
            self.assertEqual(out["shown"]["text"], "Coupon expired")
            self.assertIn("text-danger", out["shown"]["className"])
            self.assertNotIn("d-none", out["shown"]["className"])
            self.assertEqual(out["cleared"]["text"], "")
            self.assertIn("d-none", out["cleared"]["className"])

        def test_removing_a_coupon_restores_the_apply_controls(self):
            out = self._run_functions(("setCouponFeedback", "resetAppliedCoupon"), """
function classListOf(initial) {
  const names = new Set(initial);
  return {
    add: (name) => names.add(name),
    remove: (name) => names.delete(name),
    contains: (name) => names.has(name),
  };
}
let appliedCoupon = {code: 'SAVE10'};
const elements = {
  'pos-coupon-code': {value: 'SAVE10', disabled: true},
  'pos-coupon-discount-row': {style: {display: 'flex'}},
  'pos-coupon-apply': {classList: classListOf(['d-none']), disabled: true},
  'pos-coupon-remove': {classList: classListOf([]), disabled: false},
  'pos-coupon-feedback': {textContent: 'Coupon applied.',
    className: 'small mt-1 text-success'},
};
const document = {getElementById: (id) => elements[id] || null};
resetAppliedCoupon();
console.log(JSON.stringify({
  appliedCoupon,
  value: elements['pos-coupon-code'].value,
  inputDisabled: elements['pos-coupon-code'].disabled,
  applyHidden: elements['pos-coupon-apply'].classList.contains('d-none'),
  applyDisabled: elements['pos-coupon-apply'].disabled,
  removeHidden: elements['pos-coupon-remove'].classList.contains('d-none'),
  discountRow: elements['pos-coupon-discount-row'].style.display,
  feedbackText: elements['pos-coupon-feedback'].textContent,
  feedbackClass: elements['pos-coupon-feedback'].className,
}));
""")
            self.assertIsNone(out["appliedCoupon"])
            self.assertEqual(out["value"], "")
            self.assertFalse(out["inputDisabled"])
            self.assertFalse(out["applyHidden"])
            self.assertFalse(out["applyDisabled"])
            self.assertTrue(out["removeHidden"])
            self.assertEqual(out["discountRow"], "none")
            self.assertEqual(out["feedbackText"], "")
            self.assertIn("d-none", out["feedbackClass"])


if __name__ == "__main__":
    unittest.main()
