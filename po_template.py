"""Purchase-order letterhead: fixed text layout and freely positioned logo.

Clients can update header information without changing document structure.
Legacy canvas layouts are normalized to the same fixed print layout.

Presentation only: the normalizer takes plain dictionaries and the drawing
helper takes a ReportLab canvas, so both stay unit-testable without a database.
"""

from __future__ import annotations

import copy
import os
from typing import Any, Mapping
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import Flowable, LongTable, Paragraph, Spacer, Table


PO_TEMPLATE_VERSION = 3
PO_TEMPLATE_SETTING = "po_template_json"
PO_TEMPLATE_LOGO_SETTING = "po_template_logo_filename"
PO_MAX_ELEMENTS = 14
PO_TEXT_LIMIT = 200
PO_PAGE_SIZES = {"A4": (210, 297), "Letter": (215.9, 279.4)}
PO_HEADER_FONT_LIMITS = {"brand": (10, 20), "contact": (6, 10), "title": (12, 22)}

# Legacy header tokens; ``sample`` values only feed the on-screen preview.
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
    "page_size": "A4",
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


def default_po_template() -> dict[str, Any]:
    """A fresh copy of the shipped letterhead."""
    return copy.deepcopy(DEFAULT_PO_TEMPLATE)


def _number(value: Any, default: float, low: float, high: float, precision: int | None = 1) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    if number != number or number in (float("inf"), float("-inf")):
        number = float(default)
    number = min(max(number, low), high)
    return round(number, precision) if precision is not None else number


def _text(value: Any, limit: int = PO_TEXT_LIMIT) -> str:
    text = str(value if value is not None else "").replace("\r\n", "\n").replace("\r", "\n")
    return text.strip("\n")[:limit]


def normalize_po_template(
    payload: Any,
    fallback: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Allow header text, font sizes, paper sizes and logo geometry only.

    Rebuild non-logo geometry from shipped defaults on every read and write.
    Legacy clients cannot restore unrestricted text layouts.
    Header values with known IDs and the separately stored logo are preserved.
    """
    template = default_po_template()
    for source in (fallback, payload):
        if not isinstance(source, Mapping):
            continue
        page_size = source.get("page_size", template["page_size"])
        template["page_size"] = page_size if isinstance(page_size, str) and page_size in PO_PAGE_SIZES else "A4"
        raw_elements = source.get("elements")
        if not isinstance(raw_elements, list):
            continue
        by_id = {}
        for raw in raw_elements[:PO_MAX_ELEMENTS]:
            if isinstance(raw, Mapping) and isinstance(raw.get("id"), str):
                by_id.setdefault(raw["id"], raw)
        for element in template["elements"]:
            limits = PO_HEADER_FONT_LIMITS.get(element["id"])
            raw = by_id.get(element["id"])
            if not limits or not raw or raw.get("kind", "text") != "text":
                continue
            element["text"] = _text(raw.get("text", element["text"]))
            element["font_size"] = _number(raw.get("font_size", element["font_size"]), element["font_size"], *limits)
        logo = template["elements"][0]
        raw_logo = by_id.get("logo")
        if raw_logo and raw_logo.get("kind", "logo") == "logo":
            for key in ("x_pct", "y_mm", "width_mm", "height_mm"):
                logo[key] = _number(raw_logo.get(key, logo[key]), logo[key], -1000, 1000, precision=None)
    # Keep the logo on the selected sheet, but do not reserve text space for it.
    page_width, page_height = PO_PAGE_SIZES[template["page_size"]]
    logo = template["elements"][0]
    margin = template["page_inset_mm"]
    content_width = page_width - 2 * margin
    logo["width_mm"] = _number(logo["width_mm"], 32, 5, page_width, precision=None)
    logo["height_mm"] = _number(logo["height_mm"], 16, 5, page_height, precision=None)
    logo["x_pct"] = _number(logo["x_pct"], 0, -margin / content_width * 100,
                            (page_width - margin - logo["width_mm"]) / content_width * 100, precision=None)
    logo["y_mm"] = _number(logo["y_mm"], 0, -margin, page_height - margin - logo["height_mm"], precision=None)
    return template


def po_template_page_size(template: Mapping[str, Any]) -> tuple[float, float]:
    """Return the selected standard paper dimensions in millimetres."""
    return PO_PAGE_SIZES[normalize_po_template(template)["page_size"]]


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
            for element in sorted(self.template["elements"], key=lambda item: item["kind"] == "logo"):
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
                preserveAspectRatio=False, anchor="nw", mask="auto",
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
            # Long client text must never cover another fixed header field.
            next_top_mm = {"brand": 9.5, "contact": 26, "title": 36, "reference": 41}.get(element["id"], 46)
            clip = canvas.beginPath()
            clip.rect(x, self._height - next_top_mm * mm, width,
                      max(next_top_mm - element["y_mm"], 0) * mm)
            canvas.clipPath(clip, stroke=0, fill=0)
            canvas.setFillColor(colors.HexColor(element["color"]))
            canvas.setFont(font_name, font_size)
            for index, line in enumerate(lines):
                baseline = top - font_size * 0.8 - index * leading
                if baseline - font_size * 0.2 < self._height - next_top_mm * mm:
                    break
                if element["align"] == "right":
                    canvas.drawRightString(x + width, baseline, line)
                elif element["align"] == "center":
                    canvas.drawCentredString(x + width / 2.0, baseline, line)
                else:
                    canvas.drawString(x, baseline, line)
        finally:
            canvas.restoreState()


def draw_po_template_logo(canvas, document, template, logo_path) -> None:
    """Overlay the freely positioned logo above first-page text and tables."""
    if document.page != 1:
        return
    header = PurchaseOrderTemplateHeader(template, logo_path=logo_path)
    margin = header.template["page_inset_mm"] * mm
    page_width, page_height = document.pagesize
    header.wrap(page_width - 2 * margin, page_height)
    canvas.saveState()
    try:
        canvas.translate(margin, page_height - margin - header._height)
        header._draw_logo(canvas, header.template["elements"][0])
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


def purchase_order_body(width, *, details, buyer, supplier, invoice, lines, currency, total, notes):
    """Build the fixed PO body; labels are trusted and all stored values are plain text."""
    ink = colors.HexColor('#111827')
    muted = colors.HexColor('#475569')
    rule = colors.HexColor('#cbd5e1')
    shade = colors.HexColor('#f1f5f9')
    band = colors.HexColor('#1e293b')
    body = ParagraphStyle('PO body', fontName='Helvetica', fontSize=9, leading=12, textColor=ink)
    field = ParagraphStyle('PO field', parent=body, spaceBefore=3)
    heading = ParagraphStyle('PO heading', parent=body, fontName='Helvetica-Bold', keepWithNext=True,
                             spaceAfter=5)
    numeric = ParagraphStyle('PO number', parent=body, alignment=TA_RIGHT)
    column = ParagraphStyle('PO column', parent=body, fontName='Helvetica-Bold', fontSize=8,
                            textColor=colors.white)
    column_right = ParagraphStyle('PO right column', parent=column, alignment=TA_RIGHT)
    total_style = ParagraphStyle('PO total', parent=numeric, fontName='Helvetica-Bold', fontSize=10)

    def plain_text(value):
        plain = str(value).replace('\r\n', '\n').replace('\r', '\n')
        return escape(plain).replace('\n', '<br/>')

    def text(value, style=body):
        return Paragraph(plain_text(value), style)

    def block(title, fields):
        cells = [text(title, heading)]
        for name, value in fields:
            if value is not None and str(value).strip():
                cells.append(Paragraph(f'<font color="{muted.hexval()}"><b>{escape(name)}:</b></font> '
                                       f'{plain_text(value)}', field))
        return cells

    def panels(left, right):
        return Table([[left, '', right]], colWidths=[(width - 10) / 2, 10, (width - 10) / 2], style=[
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('BACKGROUND', (0, 0), (0, 0), shade),
            ('BACKGROUND', (2, 0), (2, 0), shade),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ('LEFTPADDING', (0, 0), (-1, -1), 10),
            ('RIGHTPADDING', (0, 0), (-1, -1), 10),
        ])

    headers = ('Line', 'Item description', 'SKU', 'Qty', 'Unit', 'Unit price', 'Line total')
    rows = [[text(name, column if index < 3 or index == 4 else column_right)
             for index, name in enumerate(headers)]]
    for number, (name, sku, quantity, unit, unit_price, line_total) in enumerate(lines, 1):
        rows.append([text(number), text(name), text(sku or '-'), text(quantity, numeric),
                     text(unit or '-'), text(unit_price, numeric), text(line_total, numeric)])
    rows.append([text(f'Order total ({currency})', total_style), '', '', '', '', '', text(total, total_style)])
    table = LongTable(rows, colWidths=[width * fraction for fraction in (.065, .30, .15, .095, .09, .15, .15)],
                      repeatRows=1, style=[
        ('BACKGROUND', (0, 0), (-1, 0), band),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('ROWBACKGROUNDS', (0, 1), (-1, -2), [colors.white, shade]),
        ('LINEBELOW', (0, 1), (-1, -2), .35, rule),
        ('SPAN', (0, -1), (-2, -1)),
        ('BACKGROUND', (0, -1), (-1, -1), shade),
        ('LINEABOVE', (0, -1), (-1, -1), 1, band),
        ('NOSPLIT', (0, -2), (-1, -1)),
    ])
    elements = [panels(block('ORDER DETAILS', details), block('INVOICE REFERENCE', invoice)), Spacer(1, 8),
                panels(block('BUYER / ORDERING BRANCH', buyer), block('SUPPLIER', supplier)), Spacer(1, 12),
                text(f'ORDER LINE ITEMS  |  {currency}', heading), table]
    if notes and notes.strip():
        elements.extend((Spacer(1, 12), text('ORDER NOTES', heading), text(notes)))
    return elements


def draw_po_continuation(canvas, document, po_number, label='PURCHASE ORDER'):
    """Identify detached continuation pages without repeating the full letterhead."""
    canvas.saveState()
    try:
        canvas.setFillColor(colors.HexColor('#475569'))
        canvas.setFont('Helvetica-Bold', 9)
        canvas.drawString(document.leftMargin + 6, document.pagesize[1] - 11 * mm,
                          f'{_pdf_safe_text(label)}  |  {_pdf_safe_text(po_number)}  |  Continued')
    finally:
        canvas.restoreState()


def stock_verification_body(width, *, details, columns, rows, note):
    """Shared, ink-friendly count table for GRNs and branch inventory sheets."""
    body = ParagraphStyle('Stock count body', fontName='Helvetica', fontSize=8,
                          leading=11, textColor=colors.HexColor('#111827'))
    small = ParagraphStyle('Stock count heading', parent=body, fontName='Helvetica-Bold',
                           fontSize=9, spaceAfter=8)
    header = ParagraphStyle('Stock count column', parent=body, fontName='Helvetica-Bold',
                            textColor=colors.white)
    safe = lambda value, style=body: Paragraph(escape(str(value if value is not None else '-')), style)
    band, shade = colors.HexColor('#0f172a'), colors.HexColor('#f1f5f9')
    meta = Table([[safe(label, small), safe(value)] for label, value in details],
                 colWidths=[width * .27, width * .73], style=[
                     ('BACKGROUND', (0, 0), (-1, -1), shade),
                     ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                     ('TOPPADDING', (0, 0), (-1, -1), 5),
                     ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
                 ])
    grid = [[safe(label, header) for label in columns]]
    grid.extend([safe(value) for value in row] for row in rows)
    table = LongTable(grid, colWidths=[width / len(columns)] * len(columns), repeatRows=1,
                      style=[('BACKGROUND', (0, 0), (-1, 0), band),
                             ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, shade]),
                             ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                             ('TOPPADDING', (0, 0), (-1, -1), 7),
                             ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
                             ('LINEBELOW', (0, 1), (-1, -1), .3, colors.HexColor('#cbd5e1'))])
    return [meta, Spacer(1, 14), safe(note, small), table, Spacer(1, 24),
            safe('Counted by: ____________________    Checked by: ____________________    Date: __________')]
