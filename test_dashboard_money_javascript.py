"""Regression tests for the POS dashboard's client-side money calculations."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


DASHBOARD = Path(__file__).parent / "templates" / "dashboard.html"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is required to execute dashboard JavaScript")


def _javascript_function(source, name):
    """Extract one named function, including its balanced body, from the template."""
    match = re.search(rf"function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", source)
    assert match, f"JavaScript function {name} was not found"

    depth = 0
    for index in range(match.end() - 1, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[match.start():index + 1]
    raise AssertionError(f"JavaScript function {name} has an unbalanced body")


def _run_cart_scenario(cart, cash_received):
    source = DASHBOARD.read_text(encoding="utf-8")
    functions = "\n".join(
        _javascript_function(source, name)
        for name in ("toCents", "mulMoney", "getCurrentCartTotal")
    )
    script = f"""
{functions}
const cart = {json.dumps(cart)};
const totalCents = toCents(getCurrentCartTotal());
const cashReceivedCents = toCents({json.dumps(cash_received)});
console.log(JSON.stringify({{
  total: getCurrentCartTotal(),
  change: (cashReceivedCents - totalCents) / 100
}}));
"""
    result = subprocess.run(
        [NODE, "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_pos_change_matches_screenshot_scenario():
    result = _run_cart_scenario(
        [
            {"price": 3400, "quantity": 1, "tax_rate": 0},
            {"price": 5500, "quantity": 1, "tax_rate": 0},
        ],
        "9000",
    )

    assert result == {"total": 8900, "change": 100}


def test_pos_change_matches_original_report():
    result = _run_cart_scenario(
        [{"price": 5400, "quantity": 1, "tax_rate": 0}],
        "6000",
    )

    assert result == {"total": 5400, "change": 600}


def test_fixed_promotion_keeps_price_and_discount_in_cents():
    source = DASHBOARD.read_text(encoding="utf-8")

    assert "priceCents - toCents(activePromo.discount_value)" in source
    assert "subMoney(priceCents, activePromo.discount_value)" not in source