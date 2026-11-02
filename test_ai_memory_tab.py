"""Contract tests for the dashboard's AI Memory tab.

The tab is the user-facing half of the memory layer: it shows what Loli
remembers, whether each memory is always-on, and the chat commands that manage
them. It belongs to the AI assistant, which is a manager/owner capability, so
it must be admin-gated (cashiers have no assistant access) and it must stay
registered in the SPA section cache so opening it always re-reads.
"""

import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

DASHBOARD = Path(__file__).parent / "templates" / "dashboard.html"
SOURCE = DASHBOARD.read_text(encoding="utf-8")
NODE = shutil.which("node")


def render(role):
    return Environment(loader=FileSystemLoader(Path(__file__).parent / "templates")).from_string(SOURCE).render(
        session={"role": role, "username": "Operator"},
        pos_name="Parrot POS", currency_code="MMK", currency_suffix="MMK",
    )


class _Balance(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self.stack = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        else:
            self.errors.append(tag)


@pytest.mark.parametrize("role", ["manager", "boss"])
def test_memory_tab_renders_for_agent_roles_with_its_commands(role):
    html = render(role)
    assert 'id="memory-section"' in html
    assert "onclick=\"showSection('memory')\"" in html
    assert '<div data-capability="admin" id="memory-section"' in html
    assert "#remember &lt;detail&gt;" in html
    assert "#memory" in html
    assert "#forget &lt;id&gt;" in html
    assert 'id="memory-tab-always"' in html
    assert 'id="memory-tab-other"' in html
    assert 'id="memory-tab-form"' in html


def test_memory_tab_is_admin_gated_and_cashiers_cannot_navigate_to_it():
    html = render("cashier")
    # The section exists in the shared markup but is hidden for cashiers, and
    # the client-side section guard refuses to open it.
    assert '<div data-capability="admin" id="memory-section"' in html
    assert '<a class="nav-link" href="#" data-capability="admin" onclick="showSection(\'memory\')">' in html
    cashier_sections = re.search(
        r"const CASHIER_SECTIONS = new Set\(\[(.*?)\]\);", SOURCE, re.S).group(1)
    assert '"memory"' not in cashier_sections


@pytest.mark.parametrize("role", ["manager", "boss", "cashier"])
def test_ai_widget_is_only_rendered_for_agent_roles(role):
    html = render(role)
    shown = 'id="loli-widget"' in html or 'loli-chat' in html
    assert shown is (role in ("manager", "boss"))


def test_memory_section_markup_is_balanced():
    html = render("manager")
    start = html.index("<!-- AI Memory Section")
    end = html.index("<!-- Users Section -->", start)
    parser = _Balance()
    parser.feed(html[start:end])
    assert not parser.errors
    assert not parser.stack


def test_memory_tab_is_registered_in_the_section_cache():
    loaders = re.search(r"const SECTION_LOADERS = \{(.*?)\n\s*\};", SOURCE, re.S).group(1)
    resources = re.search(r"const SECTION_RESOURCES = \{(.*?)\n\s*\};", SOURCE, re.S).group(1)
    assert re.search(r"^\s*memory:", loaders, re.M)
    assert re.search(r"^\s*memory:", resources, re.M)
    # An empty resource list means "always re-render": memories change from chat.
    assert re.search(r"^\s*memory: \[\],", resources, re.M)
    assert "memory: () => loadMemoryTab()," in loaders


def test_memory_tab_shows_the_savable_agent_plan_limit():
    html = render("manager")
    assert 'id="ai-plan-limit-card"' in html
    assert 'id="ai-max-plan-steps"' in html
    assert 'id="save-ai-plan-limit-btn"' in html
    assert 'onclick="saveAIPlanLimit()"' in html
    # The card is filled and saved through the guarded agent endpoint.
    assert "fetch(\"/api/agent/plan-limit\")" in SOURCE
    assert 'body: JSON.stringify({ max_plan_steps: value })' in SOURCE
    # Opening the section reads the saved value.
    assert "memory: () => loadMemoryTab()," in SOURCE
    assert "loadAIPlanLimit();" in SOURCE.split("function loadMemoryTab()", 1)[1]


def test_memory_tab_uses_the_guarded_memory_endpoints():
    assert 'fetch("/api/agent/memories?limit=100")' in SOURCE
    assert 'method: "POST"' in SOURCE
    assert "`/api/agent/memories/${encodeURIComponent(memoryId)}`" in SOURCE
    assert 'method: "DELETE"' in SOURCE
    # Branch-shared writes stay manager-only, mirroring the API.
    assert 'hasCapability("admin")' in SOURCE
    # Memory text is never rendered as markup.
    assert "textContent = entry.summary" in SOURCE


def test_memory_branch_indicator_is_updated_with_the_branch():
    assert '"memory-branch-indicator"' in SOURCE


@pytest.mark.skipif(NODE is None, reason="Node.js required")
def test_memory_tab_javascript_parses(tmp_path):
    html = render("cashier")
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.S)
    path = tmp_path / "dashboard.js"
    path.write_text("\n".join(scripts), encoding="utf-8")
    result = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
