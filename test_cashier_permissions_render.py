"""Template render checks complement the extracted Node permission tests."""
from pathlib import Path
import re
import shutil
import subprocess

import pytest
from jinja2 import Environment, FileSystemLoader

SOURCE = (Path(__file__).parent / "templates" / "dashboard.html").read_text(encoding="utf-8")


def render(role):
    return Environment(loader=FileSystemLoader(Path(__file__).parent / "templates")).from_string(SOURCE).render(
        session={"role": role, "username": "Operator"},
        pos_name="Parrot POS", currency_code="MMK", currency_suffix="MMK",
    )


def test_cashier_shell_hides_admin_before_javascript_runs():
    html = render("cashier")
    assert '<body class="operational-workspace">' in html
    assert '[hidden] { display: none !important; }' in html
    for section in ("dashboard", "customers", "debts", "promotions", "suppliers",
                    "purchases", "warehouse", "users", "logs", "settings"):
        if section == "logs":
            assert 'id="logs-section"' not in html
        else:
            assert f'<div data-capability="admin" id="{section}-section"' in html
        if section != "logs":  # logs navigation is already server-rendered manager-only
            assert f'data-capability="admin" onclick="showSection(\'{section}\')"' in html
    assert 'data-capability="branches.switch" id="branch-selector"' in html
    assert 'Cashier workspace' in html
    assert 'Sales history and reports show your sales only.' in html
    assert 'data-capability="catalog.write" data-bs-target="#addProductModal"' in html
    assert 'data-capability="catalog.write" data-bs-target="#addCategoryModal"' in html
    assert 'data-capability="labels" class="btn btn-success"' in html


@pytest.mark.parametrize("role", ["manager", "boss"])
def test_manager_shell_does_not_use_cashier_css(role):
    html = render(role)
    assert '<body class="">' in html
    assert 'onclick="showSection(\'logs\')"' in html
    assert re.search(r'id="operational-scope-note"[^>]*hidden', html)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js required")
def test_full_rendered_inline_javascript_parses(tmp_path):
    html = render("cashier")
    scripts = re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>', html, re.S)
    path = tmp_path / "dashboard.js"
    path.write_text("\n".join(scripts), encoding="utf-8")
    result = subprocess.run([shutil.which("node"), "--check", str(path)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
