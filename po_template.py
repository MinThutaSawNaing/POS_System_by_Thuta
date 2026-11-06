"""Purchase order letterhead template: validation, tokens and PDF drawing.

Every branch (client) can print purchase orders on its own letterhead: logo,
company name, contact block, document title, rules and a footer note. The
Settings tab renders an A4 canvas editor that posts this dictionary back
(``PUT /api/settings/po-template``), and the purchase-order PDF endpoint draws
the very same layout, so what a manager arranges on screen is what the supplier
receives.

Presentation only: the normalizer takes plain dictionaries and the drawing
helper takes a ReportLab canvas, so both stay unit-testable without a database.
"""

from __future__ import annotations

import copy
import os
import re
from typing import Any, Mapping

from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import Flowable


PO_TEMPLATE_VERSION = 1
PO_PAGE_WIDTH_MM = 210
PO_PAGE_HEIGHT_MM = 297
PO_TEMPLATE_SETTING = "po_template_json"
PO_TEMPLATE_LOGO_SETTING = "po_template_logo_filename"
PO_MAX_ELEMENTS = 14
PO_HEADER_MAX_MM = 110
PO_TEXT_LIMIT = 200

# Tokens the editor offers; ``sample`` values only feed the on-screen preview.
PO_TEMPLATE_TOKENS = (
    {"token": "{{brand_name}}", "label": "Client / brand name", "sample": "Parrot Trading Co., Ltd."},
    {"token": "{{branch_name}}", "label": "Branch name", "sample": "Downtown Branch"},
    {"token": "{{branch_address}}", "label": "Branch address", "sample": "No. 12, Market Road, Yangon"},
    {"token": "{{branch_phone}}", "label": "Branch phone", "sample": "+95 9 111 222 333"},
    {"token": "{{branch_email}}", "label": "Branch email", "sample": "orders@parrot.example"},
    {"token": "{{po_number}}", "label": "PO number", "sample": "PO-2026-0142"},
    {"token": "{{po_date}}", "label": "Order date", "sample": "2026-11-06"},
    {"token": "{{po_status}}", "label": "Status", "sample": "DRAFT"},
    {"token": "{{expected_delivery}}", "label": "Expected delivery", "sample": "2026-11-20"},
    {"token": "{{supplier_name}}", "label": "Supplier name", "sample": "Golden Rice Wholesale"},
    {"token": "{{supplier_contact}}", "label": "Supplier contact", "sample": "Daw Mya"},
    {"token": "{{supplier_phone}}", "label": "Supplier phone", "sample": "+95 9 123 456 789"},
    {"token": "{{total}}", "label": "Order total", "sample": "1,240.00 $"},
)

# The default letterhead mirrors the old built-in PDF (title, supplier block and
# record-keeping footer) so an untouched install prints what it always printed.
DEFAULT_PO_TEMPLATE = {
    "version": PO_TEMPLATE_VERSION,
    "page_inset_mm": 15.0,
    "header_height_mm": 46.0,
    "elements": [
        {"id": "logo", "kind": "logo", "x_pct": 0.0, "y_mm": 0.0,
         "width_mm": 32.0, "height_mm": 16.0},
        {"id": "brand", "kind": "text", "text": "{{brand_name}}", "x_pct": 36.0,
         "y_mm": 0.0, "width_pct": 64.0, "font_size": 17.0, "bold": True,
         "align": "left", "color": "#111827"},
        {"id": "contact", "kind": "text",
         "text": "{{branch_address}}\n{{branch_phone}}  ·  {{branch_email}}",
         "x_pct": 36.0, "y_mm": 9.5, "width_pct": 64.0, "font_size": 8.5,
         "bold": False, "align": "left", "color": "#4b5563"},
        {"id": "title", "kind": "text", "text": "PURCHASE ORDER", "x_pct": 0.0,
         "y_mm": 26.0, "width_pct": 60.0, "font_size": 20.0, "bold": True,
         "align": "left", "color": "#111827"},
        {"id": "reference", "kind": "text",
         "text": "PO {{po_number}}  ·  {{po_date}}", "x_pct": 0.0,
         "y_mm": 36.0, "width_pct": 100.0, "font_size": 9.0, "bold": False,
         "align": "left", "color": "#4b5563"},
        {"id": "rule", "kind": "rule", "x_pct": 0.0, "y_mm": 41.0,
         "width_pct": 100.0, "thickness": 1.4, "color": "#0f172a"},
    ],
    "footer": {
        "text": "This is an internal document for record keeping purposes.",
        "font_size": 8.0,
        "color": "#6b7280",
        "show_page_number": True,
    },
}

_HEX_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")
_ALIGNMENTS = {"left", "center", "right"}
_KINDS = {"text", "logo", "rule"}



def default_po_template() -> dict[str, Any]:
    """A fresh copy of the shipped letterhead."""
    return copy.deepcopy(DEFAULT_PO_TEMPLATE)


def _number(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return round(float(default), 1)
    if number != number or number in (float("inf"), float("-inf")):
        return round(float(default), 1)
    return round(min(max(number, low), high), 1)


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _colour(value: Any, default: str) -> str:
    text = str(value or "").strip()
    return text.lower() if _HEX_COLOUR.match(text) else default


def _text(value: Any, limit: int = PO_TEXT_LIMIT) -> str:
    text = str(value if value is not None else "").replace("\r\n", "\n").replace("\r", "\n")
    return text.strip("\n")[:limit]


def normalize_po_element(
    raw: Any,
    index: int = 0,
    fallback: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate one canvas block, clamping positions and ignoring junk."""
    previous = fallback if isinstance(fallback, Mapping) else {}
    source = raw if isinstance(raw, Mapping) else {}

    kind = str(source.get("kind") or previous.get("kind") or "text").strip().lower()
    if kind not in _KINDS:
        kind = "text"

    identifier = re.sub(r"[^a-zA-Z0-9_-]", "", str(source.get("id") or previous.get("id") or ""))[:32]
    element: dict[str, Any] = {
        "id": identifier or f"block-{index + 1}",
        "kind": kind,
    }

    if kind == "logo":
        element["width_mm"] = _number(source.get("width_mm", previous.get("width_mm", 32)), 32, 6, 90)
        element["height_mm"] = _number(source.get("height_mm", previous.get("height_mm", 16)), 16, 5, 80)
    else:
        element["text"] = _text(source.get("text", previous.get("text", "")))
        element["font_size"] = _number(source.get("font_size", previous.get("font_size", 10)), 10, 6, 40)
        element["bold"] = _bool(source.get("bold", previous.get("bold", False)))
        align = str(source.get("align") or previous.get("align") or "left").strip().lower()
        element["align"] = align if align in _ALIGNMENTS else "left"
        if kind == "rule":
            element["thickness"] = _number(
                source.get("thickness", previous.get("thickness", 1)), 1, 0.25, 3)

    element["x_pct"] = _number(source.get("x_pct", previous.get("x_pct", 0)), 0, 0, 100)
    element["y_mm"] = _number(
        source.get("y_mm", previous.get("y_mm", 0)), 0, 0, PO_HEADER_MAX_MM)
    element["width_pct"] = _number(
        source.get("width_pct", previous.get("width_pct", 100)), 100, 5, 100)
    element["color"] = _colour(source.get("color", previous.get("color", "#111827")), "#111827")
    return element



def normalize_po_template(
    payload: Any,
    fallback: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a safe letterhead layout for storage or drawing.

    Unknown keys are dropped, numbers are clamped to printable ranges and text
    is trimmed, so an older editor, a stale tab or a hand-written request can
    never corrupt a saved template.
    """
    base = copy.deepcopy(fallback) if isinstance(fallback, Mapping) else default_po_template()
    if not isinstance(payload, Mapping):
        return base

    template: dict[str, Any] = {
        "version": PO_TEMPLATE_VERSION,
        "page_inset_mm": _number(payload.get("page_inset_mm", base.get("page_inset_mm", 15)), 15, 8, 30),
        "header_height_mm": _number(
            payload.get("header_height_mm", base.get("header_height_mm", 46)), 46, 20, PO_HEADER_MAX_MM),
    }

    raw_elements = payload.get("elements")
    base_elements = base.get("elements") if isinstance(base.get("elements"), list) else []
    if isinstance(raw_elements, list):
        elements: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(raw_elements[:PO_MAX_ELEMENTS]):
            previous = base_elements[index] if index < len(base_elements) else None
            element = normalize_po_element(raw, index, previous)
            if element["id"] in seen:
                element["id"] = f"{element['id'][:26]}-{index + 1}"
            seen.add(element["id"])
            elements.append(element)
        template["elements"] = elements
    else:
        template["elements"] = [
            normalize_po_element(item, index)
            for index, item in enumerate(base_elements[:PO_MAX_ELEMENTS])
        ]

    footer = payload.get("footer") if isinstance(payload.get("footer"), Mapping) else {}
    base_footer = base.get("footer") if isinstance(base.get("footer"), Mapping) else DEFAULT_PO_TEMPLATE["footer"]
    template["footer"] = {
        "text": _text(footer.get("text", base_footer.get("text", ""))),
        "font_size": _number(footer.get("font_size", base_footer.get("font_size", 8)), 8, 6, 14),
        "color": _colour(footer.get("color", base_footer.get("color", "#6b7280")), "#6b7280"),
        "show_page_number": _bool(
            footer.get("show_page_number", base_footer.get("show_page_number", True)), True),
    }
    # Blocks stay inside the band they were arranged in, so a stale tab can
    # never print a header over the supplier details.
    limit = max(template["header_height_mm"] - 2, 0)
    for element in template["elements"]:
        element["y_mm"] = min(element["y_mm"], limit)
    return template


def render_po_template_text(text: Any, context: Mapping[str, Any] | None = None) -> str:
    """Replace ``{{token}}`` placeholders with order values (unknown tokens stay)."""
    result = str(text if text is not None else "")
    for key, value in (context or {}).items():
        token = "{{" + str(key).strip("{} ") + "}}"
        if token in result:
            result = result.replace(token, str(value if value is not None else ""))
    return result


def po_template_sample_context() -> dict[str, str]:
    """Preview values used by every token chip."""
    return {
        token["token"].strip("{} "): token["sample"]
        for token in PO_TEMPLATE_TOKENS
    }


def _pdf_safe_text(value: Any) -> str:
    """Encode for the built-in Latin fonts so a foreign glyph cannot break a PDF."""
    return str(value if value is not None else "").encode("cp1252", "replace").decode("cp1252")


def _wrap_paragraph(paragraph: str, font_name: str, font_size: float, max_width: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in paragraph.split(" "):
        candidate = f"{current} {word}" if current else word
        if stringWidth(candidate, font_name, font_size) <= max_width or not current:
            current = candidate
            continue
        lines.append(current)
        current = word
    lines.append(current)
    return lines


class PurchaseOrderTemplateHeader(Flowable):
    """Draw the branch letterhead band at the top of a purchase order PDF."""

    def __init__(
        self,
        template: Mapping[str, Any] | None,
        context: Mapping[str, Any] | None = None,
        logo_path: str | None = None,
    ):
        super().__init__()
        self.template = normalize_po_template(template)
        self.context = dict(context or {})
        self.logo_path = logo_path or ""
        self._width = 0.0
        self._height = self.template["header_height_mm"] * mm

    def wrap(self, availWidth, availHeight):  # noqa: N803 - ReportLab API casing
        self._width = availWidth
        self._height = self.template["header_height_mm"] * mm
        return self._width, self._height

    def draw(self):
        canvas = self.canv
        canvas.saveState()
        try:
            for element in self.template["elements"]:
                if element["kind"] == "logo":
                    self._draw_logo(canvas, element)
                elif element["kind"] == "rule":
                    self._draw_rule(canvas, element)
                else:
                    self._draw_text(canvas, element)
        finally:
            canvas.restoreState()

    # -- block painters ---------------------------------------------------
    def _box_x(self, element: Mapping[str, Any]) -> float:
        return element["x_pct"] / 100.0 * self._width

    def _box_width(self, element: Mapping[str, Any]) -> float:
        return max(element["width_pct"] / 100.0 * self._width, 6 * mm)

    def _draw_logo(self, canvas, element: Mapping[str, Any]) -> None:
        if not self.logo_path or not os.path.exists(self.logo_path):
            return  # a saved template must never print a "LOGO" placeholder box
        width = element["width_mm"] * mm
        height = element["height_mm"] * mm
        bottom = self._height - element["y_mm"] * mm - height
        try:
            canvas.drawImage(
                self.logo_path, self._box_x(element), bottom,
                width=width, height=height,
                preserveAspectRatio=True, anchor="nw", mask="auto",
            )
        except Exception:  # noqa: BLE001 - a broken image must not fail the whole PO
            return

    def _draw_rule(self, canvas, element: Mapping[str, Any]) -> None:
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor(element["color"]))
        canvas.setLineWidth(element["thickness"])
        y = self._height - element["y_mm"] * mm
        x = self._box_x(element)
        canvas.line(x, y, x + self._box_width(element), y)
        canvas.restoreState()

    def _draw_text(self, canvas, element: Mapping[str, Any]) -> None:
        font_size = element["font_size"]
        font_name = "Helvetica-Bold" if element["bold"] else "Helvetica"
        text = _pdf_safe_text(render_po_template_text(element.get("text"), self.context))
        lines: list[str] = []
        for paragraph in text.split("\n"):
            if paragraph.strip():
                lines.extend(_wrap_paragraph(paragraph, font_name, font_size, self._box_width(element)))
            else:
                lines.append("")
        leading = font_size * 1.25
        x = self._box_x(element)
        width = self._box_width(element)
        top = self._height - element["y_mm"] * mm

        canvas.saveState()
        try:
            canvas.setFillColor(colors.HexColor(element["color"]))
            canvas.setFont(font_name, font_size)
            for index, line in enumerate(lines):
                baseline = top - font_size * 0.8 - index * leading
                if baseline < -leading:
                    break
                if element["align"] == "right":
                    canvas.drawRightString(x + width, baseline, line)
                elif element["align"] == "center":
                    canvas.drawCentredString(x + width / 2.0, baseline, line)
                else:
                    canvas.drawString(x, baseline, line)
        finally:
            canvas.restoreState()


def draw_po_template_footer(
    canvas,
    document,
    template: Mapping[str, Any] | None,
    context: Mapping[str, Any] | None = None,
) -> None:
    """Stamp the client footer note and page number on every PO page."""
    layout = normalize_po_template(template)
    footer = layout["footer"]
    text = _pdf_safe_text(render_po_template_text(footer.get("text"), context)).strip()
    if not text and not footer["show_page_number"]:
        return

    page_width, _ = document.pagesize
    margin = layout["page_inset_mm"] * mm
    canvas.saveState()
    try:
        canvas.setFillColor(colors.HexColor(footer["color"]))
        canvas.setFont("Helvetica", footer["font_size"])
        baseline = 10 * mm
        if text:
            canvas.drawString(margin, baseline, text)
        if footer["show_page_number"]:
            canvas.drawRightString(page_width - margin, baseline, f"Page {document.page}")
    finally:
        canvas.restoreState()
