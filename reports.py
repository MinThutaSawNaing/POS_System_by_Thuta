"""Professional PDF and Excel report builders for stock and purchasing.

The Warehouse and Purchase & Receiving tabs hand their current filters to the
export endpoints; these builders turn the very same rows the tabs render into a
branded, print-ready document (letterhead, KPI summary, striped table, totals
and page numbers) or a filterable workbook.

Presentation only: every builder takes plain dictionaries, so rendering stays
unit-testable without a database and both tabs share one imported look.
"""

from __future__ import annotations

import io
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence
from xml.sax.saxutils import escape

import xlsxwriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


REPORT_FORMAT_PDF = "pdf"
REPORT_FORMAT_XLSX = "xlsx"
REPORT_FORMATS = (REPORT_FORMAT_PDF, REPORT_FORMAT_XLSX)
DEFAULT_REPORT_FORMAT = REPORT_FORMAT_PDF

PDF_CONTENT_TYPE = "application/pdf"
XLSX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)

# Low-stock threshold used by the warehouse APIs (quantity <= 5).
LOW_STOCK_THRESHOLD = 5
EMPTY_CELL = "—"

ACCENT = colors.HexColor("#0d6efd")
TABLE_HEADER_BG = colors.HexColor("#212529")
STRIPE_BG = colors.HexColor("#f2f4f7")
TOTAL_BG = colors.HexColor("#e9ecef")
BORDER = colors.HexColor("#c9cfd8")
MUTED = colors.HexColor("#5c636a")
KPI_BG = colors.HexColor("#f8f9fa")

CELL_FONT_SIZE = 7.5
HEADER_FONT_SIZE = 7.5
BODY_FONT = "Helvetica"
BOLD_FONT = "Helvetica-Bold"

PURCHASE_ORDER_STATUS_LABELS = {
    "draft": "Draft",
    "pending": "Pending approval",
    "approved": "Approved",
    "partially_received": "Partially received",
    "received": "Received",
    "cancelled": "Cancelled",
}


def normalize_report_format(value: Any) -> str:
    """Return a supported report format, defaulting to PDF."""
    normalized = str(value or "").strip().lower()
    return normalized if normalized in REPORT_FORMATS else DEFAULT_REPORT_FORMAT


def report_content_type(report_format: Any) -> str:
    return (
        PDF_CONTENT_TYPE
        if normalize_report_format(report_format) == REPORT_FORMAT_PDF
        else XLSX_CONTENT_TYPE
    )


def report_disposition(filename: str, report_format: Any) -> str:
    """PDFs open for review/printing; workbooks always download."""
    mode = (
        "inline"
        if normalize_report_format(report_format) == REPORT_FORMAT_PDF
        else "attachment"
    )
    return f"{mode}; filename={filename}"


def report_filename(stem: str, report_format: Any, generated_at: Any = None) -> str:
    stamp = (generated_at or datetime.now()).strftime("%Y-%m-%d")
    return f"{_slug(stem)}_{stamp}.{normalize_report_format(report_format)}"


def describe_filters(
    values: Mapping[str, Any],
    *,
    default: str = "No filters applied — all records",
) -> str:
    """Render the active tab filters as one human-readable line."""
    parts = [
        f"{label}: {str(value).strip()}"
        for label, value in values.items()
        if value is not None and str(value).strip() not in ("", "False", "None")
    ]
    return " · ".join(parts) if parts else default


def column(
    key: str,
    label: str,
    *,
    width: float,
    align: str = "left",
    kind: str = "text",
) -> dict[str, Any]:
    """Describe one report column: ``kind`` drives PDF/Excel number formats."""
    return {"key": key, "label": label, "width": width, "align": align, "kind": kind}


def purchase_order_status_label(status: Any) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in PURCHASE_ORDER_STATUS_LABELS:
        return PURCHASE_ORDER_STATUS_LABELS[normalized]
    return str(status or "unknown").replace("_", " ").strip().title() or "Unknown"


def format_report_cell(value: Any, kind: str = "text", currency_suffix: str = "$") -> str:
    """Format one cell for display (PDF) — never raises on odd money values."""
    if kind == "money":
        return f"{number_value(value):,.2f} {currency_suffix}".strip()
    if kind == "int":
        return f"{int(round(number_value(value))):,}"
    if kind == "percent":
        return f"{number_value(value):.0f}%"
    if kind == "date":
        text = str(value or "").strip()
        return text[:10] if text else EMPTY_CELL
    text = str(value or "").strip()
    return text or EMPTY_CELL


def number_value(value: Any, default: float = 0.0) -> float:
    """Coerce a value to a finite float (None/NaN/inf/unparseable → default)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number


def round_money_value(value: Any) -> float:
    return round(number_value(value), 2)


def _slug(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in str(value or "report").strip().lower()
    )
    return cleaned.strip("_") or "report"


WAREHOUSE_STOCK_COLUMNS = (
    column("product_name", "Product", width=34),
    column("barcode", "Barcode", width=18),
    column("location", "Location", width=14),
    column("batch_number", "Batch", width=16),
    column("quantity", "Qty", width=9, align="right", kind="int"),
    column("unit_cost", "Unit cost", width=14, align="right", kind="money"),
    column("total_value", "Stock value", width=16, align="right", kind="money"),
    column("main_stock", "Shop stock", width=11, align="right", kind="int"),
    column("received_date", "Received", width=13, align="center", kind="date"),
    column("expiry_date", "Expiry", width=13, align="center", kind="date"),
)

PURCHASE_ORDER_COLUMNS = (
    column("po_number", "PO number", width=24),
    column("supplier_name", "Supplier", width=26),
    column("status_label", "Status", width=16, align="center"),
    column("total_amount", "Amount", width=15, align="right", kind="money"),
    column("items_count", "Lines", width=8, align="right", kind="int"),
    column("total_ordered", "Ordered", width=10, align="right", kind="int"),
    column("total_received", "Received", width=10, align="right", kind="int"),
    column("expected_delivery_date", "Expected", width=13, align="center", kind="date"),
    column("created_at", "Created", width=14, align="center", kind="date"),
    column("created_by", "Created by", width=14),
    column("approved_by", "Approved by", width=14),
)

PURCHASE_ORDER_ITEM_COLUMNS = (
    column("po_number", "PO number", width=24),
    column("product_name", "Product", width=34),
    column("ordered_qty", "Ordered", width=10, align="right", kind="int"),
    column("received_qty", "Received", width=10, align="right", kind="int"),
    column("progress_percent", "Progress", width=12, align="right", kind="percent"),
    column("unit_cost", "Unit cost", width=14, align="right", kind="money"),
    column("line_total", "Line total", width=15, align="right", kind="money"),
)


def _common_report_fields(
    *,
    title: str,
    subtitle: str,
    sheet_name: str,
    file_stem: str,
    columns: Sequence[Mapping[str, Any]],
    rows: list[dict[str, Any]],
    totals: dict[str, Any] | None,
    summary: list[dict[str, str]],
    meta: list[dict[str, str]],
    notes: list[str],
    brand: Mapping[str, Any] | None,
    currency_suffix: str = "$",
) -> dict[str, Any]:
    return {
        "title": title,
        "subtitle": subtitle,
        "sheet_name": sheet_name,
        "file_stem": file_stem,
        "columns": [dict(entry) for entry in columns],
        "rows": rows,
        "totals": totals or {},
        "totals_label": "TOTAL",
        "summary": summary,
        "meta": meta,
        "notes": notes,
        "brand": _brand_block(brand),
        "currency_suffix": str(currency_suffix or "$"),
    }


def _report_meta(
    branch_name: str,
    generated_by: str,
    filters_text: str,
    generated_at: Any,
) -> list[dict[str, str]]:
    return [
        {"label": "Branch", "value": str(branch_name or "All branches")},
        {"label": "Generated", "value": _generated_label(generated_at)},
        {"label": "Prepared by", "value": str(generated_by or EMPTY_CELL)},
        {
            "label": "Filters",
            "value": filters_text or "No filters applied — all records",
        },
    ]


def _generated_label(generated_at: Any = None) -> str:
    moment = generated_at or datetime.now()
    if isinstance(moment, str):
        return moment
    return moment.strftime("%Y-%m-%d %H:%M")


def _brand_block(brand: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize a brand block, tolerating both raw settings and report output.

    ``address`` arrives from the receipt identity settings while ``address_lines``
    is what the report dict already carries, so builders can be re-normalized
    without dropping the address.
    """
    brand = dict(brand or {})
    raw_address = str(brand.get("address") or "")
    if raw_address.strip():
        address_lines = [line.strip() for line in raw_address.splitlines() if line.strip()]
    else:
        address_lines = [
            str(line).strip() for line in brand.get("address_lines") or [] if str(line).strip()
        ]
    return {
        "name": str(brand.get("name") or "").strip(),
        "address_lines": address_lines,
        "phone": str(brand.get("phone") or "").strip(),
        "email": str(brand.get("email") or "").strip(),
    }


def build_warehouse_stock_report(
    records: Iterable[Mapping[str, Any]],
    *,
    brand: Mapping[str, Any] | None = None,
    branch_name: str = "",
    generated_by: str = "",
    filters_text: str = "",
    currency_suffix: str = "$",
    generated_at: Any = None,
) -> dict[str, Any]:
    """Stock list for the Warehouse tab: quantity, cost and value per line."""
    rows: list[dict[str, Any]] = []
    product_ids: set[Any] = set()
    total_units = 0
    total_value = 0.0
    low_stock_lines = 0

    for record in records:
        quantity = int(round(number_value(record.get("quantity"))))
        unit_cost = round_money_value(record.get("unit_cost"))
        supplied_value = record.get("total_value")
        value = (
            round_money_value(supplied_value)
            if supplied_value is not None
            else round_money_value(quantity * unit_cost)
        )
        total_units += quantity
        total_value = round_money_value(total_value + value)
        if quantity <= LOW_STOCK_THRESHOLD:
            low_stock_lines += 1
        product_id = record.get("product_id")
        if product_id is not None:
            product_ids.add(product_id)

        rows.append({
            "product_name": str(record.get("product_name") or "Unknown"),
            "barcode": record.get("barcode"),
            "location": record.get("location"),
            "batch_number": record.get("batch_number"),
            "quantity": quantity,
            "unit_cost": unit_cost,
            "total_value": value,
            "main_stock": int(round(number_value(record.get("main_stock")))),
            "received_date": record.get("received_date"),
            "expiry_date": record.get("expiry_date"),
        })

    summary = [
        {"label": "Products in stock", "value": f"{len(product_ids):,}"},
        {"label": "Stock lines", "value": f"{len(rows):,}"},
        {"label": "Units in warehouse", "value": f"{total_units:,}"},
        {
            "label": "Stock value",
            "value": format_report_cell(total_value, "money", currency_suffix),
        },
        {
            "label": f"Low stock lines (≤ {LOW_STOCK_THRESHOLD})",
            "value": f"{low_stock_lines:,}",
        },
    ]
    return _common_report_fields(
        title="Warehouse Stock List",
        subtitle="Stock held in the warehouse before restocking the shop floor",
        sheet_name="Stock List",
        file_stem="warehouse_stock_list",
        columns=WAREHOUSE_STOCK_COLUMNS,
        rows=rows,
        totals={"quantity": total_units, "total_value": total_value},
        summary=summary,
        meta=_report_meta(branch_name, generated_by, filters_text, generated_at),
        notes=[
            f"Low stock lines hold {LOW_STOCK_THRESHOLD} units or fewer.",
            "Stock value is warehouse quantity × unit cost.",
        ],
        brand=brand,
        currency_suffix=currency_suffix,
    )


def build_purchase_order_report(
    records: Iterable[Mapping[str, Any]],
    *,
    brand: Mapping[str, Any] | None = None,
    branch_name: str = "",
    generated_by: str = "",
    filters_text: str = "",
    currency_suffix: str = "$",
    generated_at: Any = None,
) -> dict[str, Any]:
    """Purchase order register for the Purchase & Receiving tab."""
    rows: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    total_amount = 0.0
    total_ordered = 0
    total_received = 0

    for record in records:
        status = str(record.get("status") or "unknown").strip().lower()
        status_counts[status] = status_counts.get(status, 0) + 1
        amount = round_money_value(record.get("total_amount"))
        ordered = int(round(number_value(record.get("total_ordered"))))
        received = int(round(number_value(record.get("total_received"))))
        total_amount = round_money_value(total_amount + amount)
        total_ordered += ordered
        total_received += received

        rows.append({
            "po_number": str(record.get("po_number") or EMPTY_CELL),
            "supplier_name": str(record.get("supplier_name") or "Unknown supplier"),
            "status": status,
            "status_label": purchase_order_status_label(status),
            "total_amount": amount,
            "items_count": int(round(number_value(record.get("items_count")))),
            "total_ordered": ordered,
            "total_received": received,
            "expected_delivery_date": record.get("expected_delivery_date"),
            "created_at": record.get("created_at"),
            "created_by": record.get("created_by"),
            "approved_by": record.get("approved_by"),
        })

    summary = [
        {"label": "Purchase orders", "value": f"{len(rows):,}"},
        {"label": "Pending approval", "value": f"{status_counts.get('pending', 0):,}"},
        {"label": "Approved", "value": f"{status_counts.get('approved', 0):,}"},
        {
            "label": "Partially received",
            "value": f"{status_counts.get('partially_received', 0):,}",
        },
        {"label": "Received", "value": f"{status_counts.get('received', 0):,}"},
        {"label": "Cancelled", "value": f"{status_counts.get('cancelled', 0):,}"},
        {"label": "Ordered units", "value": f"{total_ordered:,}"},
        {
            "label": "Order value",
            "value": format_report_cell(total_amount, "money", currency_suffix),
        },
    ]
    return _common_report_fields(
        title="Purchase Order Report",
        subtitle="Purchase orders with approval and receiving progress",
        sheet_name="Purchase Orders",
        file_stem="purchase_orders",
        columns=PURCHASE_ORDER_COLUMNS,
        rows=rows,
        totals={
            "total_amount": total_amount,
            "items_count": sum(row["items_count"] for row in rows),
            "total_ordered": total_ordered,
            "total_received": total_received,
        },
        summary=summary,
        meta=_report_meta(branch_name, generated_by, filters_text, generated_at),
        notes=[
            "Ordered and received quantities are summed across every line item.",
            "Order value is the sum of the purchase order totals on this report.",
        ],
        brand=brand,
        currency_suffix=currency_suffix,
    )


def build_purchase_order_item_rows(
    records: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Flatten purchase order line items for the workbook's detail sheet."""
    rows: list[dict[str, Any]] = []
    for record in records:
        po_number = str(record.get("po_number") or EMPTY_CELL)
        for item in record.get("items") or []:
            ordered = int(round(number_value(item.get("ordered_qty"))))
            received = int(round(number_value(item.get("received_qty"))))
            unit_cost = round_money_value(item.get("unit_cost"))
            supplied_total = item.get("line_total")
            line_total = (
                round_money_value(supplied_total)
                if supplied_total is not None
                else round_money_value(ordered * unit_cost)
            )
            rows.append({
                "po_number": po_number,
                "product_name": str(item.get("product_name") or "Unknown product"),
                "ordered_qty": ordered,
                "received_qty": received,
                "progress_percent": round(received / ordered * 100) if ordered else 0,
                "unit_cost": unit_cost,
                "line_total": line_total,
            })
    return rows


def build_purchase_order_item_sheet(
    records: Iterable[Mapping[str, Any]],
    *,
    currency_suffix: str = "$",
) -> dict[str, Any]:
    """Extra workbook sheet: one row per purchase order line item."""
    rows = build_purchase_order_item_rows(records)
    return {
        "name": "PO Line Items",
        "title": "Purchase Order Line Items",
        "columns": [dict(entry) for entry in PURCHASE_ORDER_ITEM_COLUMNS],
        "rows": rows,
        "totals": {
            "ordered_qty": sum(row["ordered_qty"] for row in rows),
            "received_qty": sum(row["received_qty"] for row in rows),
            "line_total": round_money_value(sum(row["line_total"] for row in rows)),
        },
        "currency_suffix": currency_suffix,
    }



# ---------------------------------------------------------------------------
# PDF rendering
# ---------------------------------------------------------------------------

def build_report_pdf(report: Mapping[str, Any], *, page_size=A4, compress: bool = True) -> bytes:
    """Render a report dict as a branded, print-ready PDF."""
    brand = _brand_block(report.get("brand"))
    styles = _pdf_styles()
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=page_size,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=13 * mm,
        bottomMargin=20 * mm,
        title=str(report.get("title") or "Report"),
        author=brand["name"] or "Parrot POS",
        subject=str(report.get("subtitle") or ""),
        pageCompression=1 if compress else 0,
    )

    story: list[Any] = []
    story.extend(_pdf_letterhead(brand, styles, document.width))
    story.append(Spacer(1, 4 * mm))
    story.append(_pdf_heading_band(report, styles, document.width))
    subtitle = str(report.get("subtitle") or "").strip()
    if subtitle:
        story.append(Spacer(1, 1.6 * mm))
        story.append(Paragraph(escape(subtitle), styles["subtitle"]))
    story.append(Spacer(1, 3 * mm))
    story.append(_pdf_meta_table(report, styles, document.width))
    if report.get("summary"):
        story.append(Spacer(1, 4 * mm))
        story.append(_pdf_kpi_table(report["summary"], styles, document.width))
    story.append(Spacer(1, 5 * mm))
    story.append(_pdf_data_table(report, styles, document.width))

    notes = [str(note).strip() for note in report.get("notes") or [] if str(note).strip()]
    if notes:
        story.append(Spacer(1, 4 * mm))
        story.append(_pdf_notes(notes, styles))

    footer_left = " · ".join(
        part for part in [
            brand["name"] or "Parrot POS",
            str(report.get("title") or "").strip(),
        ] if part
    )
    document.build(
        story,
        canvasmaker=lambda *args, **kwargs: _ReportCanvas(
            *args, footer_left=footer_left, **kwargs
        ),
    )
    return buffer.getvalue()


class _ReportCanvas(pdf_canvas.Canvas):
    """Two-pass canvas that stamps the footer (page numbers) on every page."""

    def __init__(self, *args, footer_left: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self._footer_left = footer_left
        self._page_states: list[dict[str, Any]] = []

    def showPage(self):
        self._page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        page_count = len(self._page_states)
        for state in self._page_states:
            self.__dict__.update(state)
            self._draw_footer(page_count)
            super().showPage()
        super().save()

    def _draw_footer(self, page_count: int) -> None:
        width, _ = self._pagesize
        self.saveState()
        self.setStrokeColor(BORDER)
        self.setLineWidth(0.4)
        self.line(14 * mm, 13.5 * mm, width - 14 * mm, 13.5 * mm)
        self.setFont(BODY_FONT, 7)
        self.setFillColor(MUTED)
        self.drawString(14 * mm, 10 * mm, self._footer_left)
        self.drawRightString(
            width - 14 * mm,
            10 * mm,
            f"Page {self._pageNumber} of {page_count}",
        )
        self.restoreState()



def _pdf_styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    styles: dict[str, ParagraphStyle] = {
        "brand": ParagraphStyle(
            "ReportBrand", parent=base["Normal"], fontName=BOLD_FONT,
            fontSize=15, leading=18, textColor=colors.HexColor("#111111"),
        ),
        "brand_contact": ParagraphStyle(
            "ReportBrandContact", parent=base["Normal"], fontName=BODY_FONT,
            fontSize=8, leading=10.5, textColor=MUTED,
        ),
        "title": ParagraphStyle(
            "ReportTitle", parent=base["Normal"], fontName=BOLD_FONT,
            fontSize=12.5, leading=15, textColor=colors.white,
        ),
        "subtitle": ParagraphStyle(
            "ReportSubtitle", parent=base["Normal"], fontName=BODY_FONT,
            fontSize=8.5, leading=11, textColor=MUTED,
        ),
        "meta_label": ParagraphStyle(
            "ReportMetaLabel", parent=base["Normal"], fontName=BOLD_FONT,
            fontSize=7.5, leading=10, textColor=MUTED, alignment=TA_RIGHT,
        ),
        "meta_value": ParagraphStyle(
            "ReportMetaValue", parent=base["Normal"], fontName=BODY_FONT,
            fontSize=7.5, leading=10, textColor=colors.HexColor("#212529"),
        ),
        "kpi_label": ParagraphStyle(
            "ReportKpiLabel", parent=base["Normal"], fontName=BODY_FONT,
            fontSize=6.8, leading=8.5, textColor=MUTED,
        ),
        "kpi_value": ParagraphStyle(
            "ReportKpiValue", parent=base["Normal"], fontName=BOLD_FONT,
            fontSize=10.5, leading=13, textColor=colors.HexColor("#111111"),
        ),
        "empty": ParagraphStyle(
            "ReportEmpty", parent=base["Normal"], fontName=BODY_FONT,
            fontSize=8.5, leading=11, textColor=MUTED,
        ),
        "note": ParagraphStyle(
            "ReportNote", parent=base["Normal"], fontName=BODY_FONT,
            fontSize=7, leading=9.5, textColor=MUTED,
        ),
    }
    for align, alignment in (
        ("left", TA_LEFT), ("center", TA_CENTER), ("right", TA_RIGHT),
    ):
        styles[f"header_{align}"] = ParagraphStyle(
            f"ReportHeader{align.title()}", parent=base["Normal"],
            fontName=BOLD_FONT, fontSize=HEADER_FONT_SIZE, leading=9.5,
            textColor=colors.white, alignment=alignment,
        )
        styles[f"cell_{align}"] = ParagraphStyle(
            f"ReportCell{align.title()}", parent=base["Normal"],
            fontName=BODY_FONT, fontSize=CELL_FONT_SIZE, leading=9.5,
            textColor=colors.HexColor("#212529"), alignment=alignment,
        )
        styles[f"total_{align}"] = ParagraphStyle(
            f"ReportTotal{align.title()}", parent=base["Normal"],
            fontName=BOLD_FONT, fontSize=CELL_FONT_SIZE, leading=9.5,
            textColor=colors.HexColor("#111111"), alignment=alignment,
        )
    return styles


def _pdf_letterhead(
    brand: Mapping[str, Any],
    styles: Mapping[str, ParagraphStyle],
    width: float,
) -> list[Any]:
    """Branded letterhead: business name, contact details, accent rule."""
    block: list[Any] = [
        Paragraph(escape(brand["name"] or "Parrot POS"), styles["brand"]),
    ]
    contact = list(brand["address_lines"])
    if brand["phone"]:
        contact.append(f"Tel {brand['phone']}")
    if brand["email"]:
        contact.append(str(brand["email"]))
    for line in contact:
        block.append(Paragraph(escape(line), styles["brand_contact"]))

    table = Table([[block]], colWidths=[width])
    table.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -1), 1.1, ACCENT),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return [table]


def _pdf_heading_band(
    report: Mapping[str, Any],
    styles: Mapping[str, ParagraphStyle],
    width: float,
) -> Table:
    band = Table(
        [[Paragraph(escape(str(report.get("title") or "Report")), styles["title"])]],
        colWidths=[width],
    )
    band.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), TABLE_HEADER_BG),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return band


def _pdf_meta_table(
    report: Mapping[str, Any],
    styles: Mapping[str, ParagraphStyle],
    width: float,
) -> Table:
    entries = [dict(entry) for entry in report.get("meta") or []]
    rows: list[list[Any]] = []
    for index in range(0, len(entries), 2):
        row: list[Any] = []
        for entry in entries[index:index + 2]:
            row.append(Paragraph(escape(str(entry.get("label") or "")), styles["meta_label"]))
            row.append(Paragraph(escape(str(entry.get("value") or "")), styles["meta_value"]))
        while len(row) < 4:
            row.append("")
        rows.append(row)
    table = Table(
        rows or [[""]],
        colWidths=[width * 0.13, width * 0.37, width * 0.13, width * 0.37],
    )
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
    ]))
    return table



def _pdf_kpi_table(
    summary: Sequence[Mapping[str, Any]],
    styles: Mapping[str, ParagraphStyle],
    width: float,
    per_row: int = 3,
) -> Table:
    """Summary metrics as a row of boxed KPI cards."""
    entries = [dict(entry) for entry in summary]
    rows: list[list[Any]] = []
    for index in range(0, len(entries), per_row):
        row: list[Any] = []
        for entry in entries[index:index + per_row]:
            row.append([
                Paragraph(escape(str(entry.get("label") or "")), styles["kpi_label"]),
                Paragraph(escape(str(entry.get("value") or "")), styles["kpi_value"]),
            ])
        while len(row) < per_row:
            row.append("")
        rows.append(row)
    table = Table(rows or [[""]], colWidths=[width / per_row] * per_row)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), KPI_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def _pdf_data_table(
    report: Mapping[str, Any],
    styles: Mapping[str, ParagraphStyle],
    width: float,
) -> Any:
    """Striped data table with a repeating header and a bold totals row."""
    columns = [dict(entry) for entry in report.get("columns") or []]
    rows = list(report.get("rows") or [])
    currency_suffix = str(report.get("currency_suffix") or "$")
    if not columns:
        return Spacer(1, 0)
    if not rows:
        return Paragraph("No records match the selected filters.", styles["empty"])

    data: list[list[Any]] = [[
        Paragraph(escape(str(entry.get("label") or "")), styles[f"header_{entry['align']}"])
        for entry in columns
    ]]
    for row in rows:
        data.append([
            Paragraph(
                escape(format_report_cell(row.get(entry["key"]), entry["kind"], currency_suffix)),
                styles[f"cell_{entry['align']}"],
            )
            for entry in columns
        ])

    totals = dict(report.get("totals") or {})
    has_totals = any(entry["key"] in totals for entry in columns)
    if has_totals:
        total_row: list[Any] = []
        for index, entry in enumerate(columns):
            if index == 0:
                total_row.append(Paragraph(
                    escape(str(report.get("totals_label") or "TOTAL")),
                    styles["total_left"],
                ))
            elif entry["key"] in totals:
                total_row.append(Paragraph(
                    escape(format_report_cell(
                        totals.get(entry["key"]), entry["kind"], currency_suffix
                    )),
                    styles[f"total_{entry['align']}"],
                ))
            else:
                total_row.append(Paragraph("", styles["cell_left"]))
        data.append(total_row)

    table = Table(data, colWidths=_pdf_column_widths(columns, width), repeatRows=1)
    style: list[tuple[Any, ...]] = [
        ("BACKGROUND", (0, 0), (-1, 0), TABLE_HEADER_BG),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
    ]
    for index in range(1, len(data)):
        if index % 2 == 0:
            style.append(("BACKGROUND", (0, index), (-1, index), STRIPE_BG))
    if has_totals:
        last = len(data) - 1
        style.extend([
            ("BACKGROUND", (0, last), (-1, last), TOTAL_BG),
            ("LINEABOVE", (0, last), (-1, last), 0.9, TABLE_HEADER_BG),
        ])
    table.setStyle(TableStyle(style))
    return table


def _pdf_notes(notes: Sequence[str], styles: Mapping[str, ParagraphStyle]) -> Table:
    block = [Paragraph(f"• {escape(note)}", styles["note"]) for note in notes]
    table = Table([[block]], colWidths=[None])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), KPI_BG),
        ("BOX", (0, 0), (-1, -1), 0.4, BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def _pdf_column_widths(
    columns: Sequence[Mapping[str, Any]],
    available_width: float,
) -> list[float]:
    declared = [float(entry.get("width") or 1) for entry in columns]
    total = sum(declared) or 1.0
    return [available_width * value / total for value in declared]



# ---------------------------------------------------------------------------
# Excel rendering
# ---------------------------------------------------------------------------

_XLSX_FORMATS = {
    "text": None,
    "int": "#,##0",
    "money": "#,##0.00",
    "percent": '0"%"',
    "date": None,
}


def build_report_xlsx(
    report: Mapping[str, Any],
    *,
    extra_sheets: Sequence[Mapping[str, Any]] | None = None,
) -> bytes:
    """Render a report dict as a filterable workbook with a Report Info sheet."""
    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output, {"in_memory": True})
    try:
        _write_worksheet(workbook, str(report.get("sheet_name") or "Report"), report)
        for sheet in extra_sheets or []:
            _write_worksheet(workbook, str(sheet.get("name") or "Details"), sheet)
        _write_info_sheet(workbook, report)
        workbook.close()
    except Exception:
        workbook.close()
        raise
    return output.getvalue()


def escape_xlsx_footer(value: str) -> str:
    """Excel headers/footers use & as a control character."""
    return str(value).replace("&", "&&")



def _write_worksheet(
    workbook: Any,
    name: str,
    report: Mapping[str, Any],
) -> None:
    """One workbook sheet: heading block, frozen header row, data, totals."""
    columns = [dict(entry) for entry in report.get("columns") or []]
    rows = list(report.get("rows") or [])
    totals = dict(report.get("totals") or {})
    currency_suffix = str(report.get("currency_suffix") or "$")
    if not columns:
        return

    worksheet = workbook.add_worksheet(name[:31])
    worksheet.set_landscape()
    worksheet.set_paper(9)  # A4
    worksheet.set_margins(0.5, 0.5, 0.6, 0.6)
    worksheet.fit_to_pages(1, 0)
    worksheet.hide_gridlines(2)

    title_format = workbook.add_format({"bold": True, "font_size": 13, "font_color": "#111111"})
    subtitle_format = workbook.add_format({"font_size": 9, "font_color": "#5c636a"})
    header_format = workbook.add_format({
        "bold": True, "font_size": 9, "font_color": "#ffffff", "bg_color": "#212529",
        "border": 1, "border_color": "#212529", "align": "center", "valign": "vcenter",
        "text_wrap": True,
    })
    body_formats = {
        align: workbook.add_format({
            "font_size": 9, "valign": "vcenter", "align": align,
            "border": 1, "border_color": "#dee2e6", "text_wrap": align == "left",
        })
        for align in ("left", "center", "right")
    }
    number_formats = {}
    total_formats = {}
    for align in ("left", "center", "right"):
        for kind, number_format in _XLSX_FORMATS.items():
            body = {
                "font_size": 9, "valign": "vcenter", "align": align,
                "border": 1, "border_color": "#dee2e6",
            }
            if number_format:
                body["num_format"] = number_format
            number_formats[(align, kind)] = workbook.add_format(body)
            total_formats[(align, kind)] = workbook.add_format({
                **body, "bold": True, "bg_color": "#e9ecef",
                "border_color": "#adb5bd", "top": 2, "top_color": "#212529",
            })

    # Heading block keeps the sheet readable on its own (exports get emailed).
    worksheet.write(0, 0, str(report.get("title") or "Report"), title_format)
    meta_line = " · ".join(
        f"{entry.get('label')}: {entry.get('value')}" for entry in report.get("meta") or []
    )
    subtitle = " · ".join(
        part for part in [str(report.get("subtitle") or "").strip(), meta_line] if part
    )
    if subtitle:
        worksheet.write(1, 0, subtitle, subtitle_format)

    header_row = 3
    for index, entry in enumerate(columns):
        worksheet.write(header_row, index, str(entry.get("label") or ""), header_format)

    for offset, row in enumerate(rows):
        excel_row = header_row + 1 + offset
        for index, entry in enumerate(columns):
            kind = str(entry.get("kind") or "text")
            align = str(entry.get("align") or "left")
            cell_format = number_formats[(align, kind)]
            value = row.get(entry.get("key"))
            if kind in ("money", "int", "percent"):
                if kind == "int":
                    worksheet.write_number(excel_row, index, int(round(number_value(value))), cell_format)
                else:
                    worksheet.write_number(excel_row, index, number_value(value), cell_format)
            else:
                worksheet.write_string(
                    excel_row, index,
                    format_report_cell(value, kind, currency_suffix), cell_format,
                )

    first_data_row = header_row + 1
    last_data_row = header_row + len(rows)
    if rows:
        worksheet.autofilter(header_row, 0, last_data_row, len(columns) - 1)
    worksheet.freeze_panes(first_data_row, 0)
    worksheet.repeat_rows(header_row, header_row)

    if rows and any(entry["key"] in totals for entry in columns):
        # A zero-total row under an empty table would be noise: KPIs live on the
        # cover sheet, so an empty report simply has no totals row.
        totals_row = last_data_row + 1
        for index, entry in enumerate(columns):
            key = entry["key"]
            align = str(entry.get("align") or "left")
            kind = str(entry.get("kind") or "text")
            if index == 0:
                worksheet.write(totals_row, index,
                                str(report.get("totals_label") or "TOTAL"),
                                total_formats[(align, "text")])
            elif key in totals:
                if kind in ("money", "int", "percent"):
                    worksheet.write_number(
                        totals_row, index, number_value(totals.get(key)),
                        total_formats[(align, kind)],
                    )
                else:
                    worksheet.write_string(
                        totals_row, index, str(totals.get(key)),
                        total_formats[(align, kind)],
                    )
            else:
                worksheet.write_blank(totals_row, index, None,
                                      total_formats[(align, "text")])

    for index, entry in enumerate(columns):
        worksheet.set_column(index, index, float(entry.get("width") or 12))

    footer_parts = [
        part for part in [
            (report.get("brand") or {}).get("name") or "Parrot POS",
            str(report.get("title") or ""),
        ] if part
    ]
    worksheet.set_footer(
        f"&L{' · '.join(escape_xlsx_footer(part) for part in footer_parts)}"
        "&RPage &P of &N"
    )



def _write_info_sheet(workbook: Any, report: Mapping[str, Any]) -> None:
    """Cover sheet: run details, KPI summary and the notes behind the report."""
    worksheet = workbook.add_worksheet("Report Info")
    worksheet.set_column(0, 0, 26)
    worksheet.set_column(1, 1, 62)

    title_format = workbook.add_format({"bold": True, "font_size": 13})
    subtitle_format = workbook.add_format({"font_size": 9, "text_wrap": True})
    label_format = workbook.add_format({"bold": True, "font_size": 9, "font_color": "#5c636a"})
    value_format = workbook.add_format({"font_size": 9, "text_wrap": True})
    section_format = workbook.add_format({
        "bold": True, "font_size": 10, "font_color": "#ffffff", "bg_color": "#212529",
    })
    metric_format = workbook.add_format({
        "bold": True, "font_size": 10, "bg_color": "#f8f9fa",
        "border": 1, "border_color": "#dee2e6",
    })

    worksheet.write(0, 0, str(report.get("title") or "Report"), title_format)
    worksheet.write(1, 0, str(report.get("subtitle") or ""), subtitle_format)

    row = 3
    worksheet.write(row, 0, "Report details", section_format)
    worksheet.write(row, 1, "", section_format)
    row += 1
    for entry in report.get("meta") or []:
        worksheet.write(row, 0, str(entry.get("label") or ""), label_format)
        worksheet.write(row, 1, str(entry.get("value") or ""), value_format)
        row += 1

    summary = report.get("summary") or []
    if summary:
        row += 1
        worksheet.write(row, 0, "Key figures", section_format)
        worksheet.write(row, 1, "", section_format)
        row += 1
        for entry in summary:
            worksheet.write(row, 0, str(entry.get("label") or ""), label_format)
            worksheet.write(row, 1, str(entry.get("value") or ""), metric_format)
            row += 1

    notes = [str(note).strip() for note in report.get("notes") or [] if str(note).strip()]
    if notes:
        row += 1
        worksheet.write(row, 0, "Notes", section_format)
        worksheet.write(row, 1, "", section_format)
        row += 1
        for note in notes:
            worksheet.write(row, 0, "•", label_format)
            worksheet.write(row, 1, note, value_format)
            row += 1

