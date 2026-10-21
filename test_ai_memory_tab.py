"""Contract tests for the dashboard's AI Memory tab.

The tab is the user-facing half of the memory layer: it shows what Loli
remembers, whether each memory is always-on, and the chat commands that manage
them. It must render for every role (a cashier's memories are their own) and it
must stay registered in the SPA section cache so opening it always re-reads.
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


@pytest.mark.parametrize("role", ["manager", "boss", "cashier"])
def test_memory_tab_renders_for_every_role_with_its_commands(role):
    html = render(role)
    assert 'id="memory-section"' in html
    assert "onclick=\"showSection('memory')\"" in html
    # The tab is not manager-only: every user may review their own memories.
    assert '<div data-capability="admin" id="memory-section"' not in html
    assert "#remember &lt;detail&gt;" in html
    assert "#memory" in html
    assert "#forget &lt;id&gt;" in html
    assert 'id="memory-tab-always"' in html
    assert 'id="memory-tab-other"' in html
    assert 'id="memory-tab-form"' in html


def test_memory_section_markup_is_balanced():
    html = render("manager")
    start = html.index("<!-- AI Memory Section -->")
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
    # Cashiers use Loli too, so their own memory tab stays reachable.
    cashier_sections = re.search(r"const CASHIER_SECTIONS = new Set\(\[(.*?)\]\);", SOURCE, re.S).group(1)
    assert '"memory"' in cashier_sections


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
