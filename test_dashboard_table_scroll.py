"""Guard the per-table scroll frames used across every dashboard tab.

Each tab paints a long table (sales, returns, debts, logs, ...). Without its own
scroll frame the rows push the tab's filters, pagination and the page itself out
of view, so every literal <table> must sit inside a scroll wrapper and the shared
CSS must keep the header row pinned while the rows scroll.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

DASHBOARD = Path(__file__).parent / "templates" / "dashboard.html"
SCROLL_CLASSES = (
    "table-responsive",
    "products-table-scroll",
    "barcode-products-scroll",
    "audit-table-scroll",
)
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "source", "track", "wbr",
}


class TableWrapperAudit(HTMLParser):
    """Collect every <table> and the nearest scroll wrapper above it.

    Script and style bodies are raw text to html.parser, so tables that are only
    produced inside JS template strings (modal bodies) are not counted here.
    """

    def __init__(self):
        super().__init__()
        self.stack = []
        self.tables = []

    def handle_starttag(self, tag, attrs):
        classes = dict(attrs).get("class") or ""
        if tag == "table":
            wrapper = None
            for _parent_tag, parent_classes in reversed(self.stack):
                wrapper = next((name for name in SCROLL_CLASSES if name in parent_classes), None)
                if wrapper:
                    break
            self.tables.append({"line": self.getpos()[0], "wrapper": wrapper})
        if tag not in VOID_TAGS:
            self.stack.append((tag, classes))

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return


def _rule(source, selector):
    start = source.index(selector)
    start = source.index("{", start)
    end = source.index("}", start)
    return source[start + 1:end]


def test_every_tab_table_has_its_own_scroll_frame():
    audit = TableWrapperAudit()
    audit.feed(DASHBOARD.read_text(encoding="utf-8"))

    assert audit.tables, "no tables were parsed from the dashboard template"
    unwrapped = [table["line"] for table in audit.tables if not table["wrapper"]]
    assert not unwrapped, f"tables without a scroll frame (line numbers): {unwrapped}"


def test_the_shared_frame_scrolls_rows_and_pins_the_header():
    source = DASHBOARD.read_text(encoding="utf-8")

    frame = _rule(source, ".table-responsive {")
    assert "max-height: min(62vh, 620px)" in frame
    assert "overflow: auto" in frame
    assert "overscroll-behavior: contain" in frame
    assert "scrollbar-gutter: stable" in frame

    header = _rule(source, ".table-responsive thead th {")
    assert "position: sticky" in header
    assert "top: 0" in header
    assert "background: #fff" in header


def test_modal_tables_are_wrapped_too():
    """The supplier and purchase-order modals build their tables in JS."""
    source = DASHBOARD.read_text(encoding="utf-8")
    script = source[source.index("<script>"):]
    fragments = re.findall(r"<div class=\"table-responsive\">\s*<table", script)
    assert len(fragments) >= 2
