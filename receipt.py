"""Receipt snapshot, formatting, and thermal paper helpers."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Mapping


RECEIPT_PAPER_58MM = "THERMAL_58MM"
RECEIPT_PAPER_80MM = "THERMAL_80MM"
RECEIPT_PAPER_OPTIONS = {RECEIPT_PAPER_58MM, RECEIPT_PAPER_80MM}
DEFAULT_RECEIPT_PAPER_SIZE = RECEIPT_PAPER_80MM
RECEIPT_SNAPSHOT_VERSION = 2
DEFAULT_RECEIPT_BRAND_NAME = "Parrot POS"
DEFAULT_RECEIPT_FOOTER = "Thank you for your purchase.\nPlease keep your receipt."
RECEIPT_IDENTITY_LIMITS = {
    "brand_name": 100,
    "email": 100,
    "phone": 30,
    "address": 300,
    "footer_message": 200,
}

PAPER_PROFILES = {
    RECEIPT_PAPER_58MM: {
        "width_mm": 58,
        "content_width_mm": 52,
        "label": "55/58 mm thermal",
    },
    RECEIPT_PAPER_80MM: {
        "width_mm": 80,
        "content_width_mm": 72,
        "label": "80 mm thermal",
    },
}


def normalize_receipt_paper_size(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    return normalized if normalized in RECEIPT_PAPER_OPTIONS else DEFAULT_RECEIPT_PAPER_SIZE


def get_paper_profile(value: Any) -> dict[str, Any]:
    return dict(PAPER_PROFILES[normalize_receipt_paper_size(value)])


def detect_receipt_logo_extension(header: bytes) -> str | None:
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "webp"
    return None


def normalize_receipt_identity(
    values: Mapping[str, Any] | None,
    branch: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Validate receipt identity and resolve blank contact overrides from a branch."""
    values = values or {}
    branch = branch or {}
    normalized = {
        "brand_name": str(values.get("brand_name") or DEFAULT_RECEIPT_BRAND_NAME).strip(),
        "logo_filename": str(values.get("logo_filename") or "").strip(),
        "email": str(values.get("email") or "").strip() or str(branch.get("email") or "").strip(),
        "phone": str(values.get("phone") or "").strip() or str(branch.get("phone") or "").strip(),
        "address": str(values.get("address") or "").strip() or str(branch.get("address") or "").strip(),
        "footer_message": str(
            values.get("footer_message")
            if "footer_message" in values
            else DEFAULT_RECEIPT_FOOTER
        ).strip(),
    }

    if not normalized["brand_name"]:
        raise ValueError("Receipt brand name is required")
    for field, limit in RECEIPT_IDENTITY_LIMITS.items():
        if len(normalized[field]) > limit:
            label = field.replace("_", " ").capitalize()
            raise ValueError(f"{label} must be {limit} characters or fewer")

    email = normalized["email"]
    if email and ("@" not in email or email.startswith("@") or email.endswith("@") or "." not in email.rsplit("@", 1)[-1]):
        raise ValueError("Invalid receipt email address")

    return normalized


def calculate_thermal_page_height_mm(content_height_px: Any, safety_mm: int = 3) -> int:
    """Convert browser CSS pixels to a cutter-safe whole millimetre height."""
    try:
        height_px = float(content_height_px)
    except (TypeError, ValueError):
        height_px = 0
    if height_px < 0 or height_px != height_px or height_px == float("inf"):
        height_px = 0
    return max(20, int(((height_px / 96) * 25.4) + safety_mm + 0.999999))


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _iso(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value or "")


def build_receipt_snapshot(
    *,
    transaction_id: str,
    sale_date: Any,
    pos_name: str,
    currency_code: str,
    currency_suffix: str,
    branch: Mapping[str, Any] | None,
    cashier_name: str,
    payment_method: str,
    cash_received: Any,
    change_given: Any,
    payment_breakdown: Mapping[str, Any] | None = None,
    items: Iterable[Mapping[str, Any]],
    subtotal: Any,
    tax: Any,
    total: Any,
    receipt_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot_items = []
    for item in items:
        quantity = int(item.get("quantity") or 0)
        unit_price = _money(item.get("unit_price"))
        tax_amount = _money(item.get("tax_amount"))
        line_subtotal = _money(unit_price * quantity)
        line_total = _money(line_subtotal + tax_amount)
        snapshot_items.append({
            "product_id": item.get("product_id"),
            "name": str(item.get("name") or "Unavailable item"),
            "quantity": quantity,
            "unit_price": unit_price,
            "tax_rate": _money(item.get("tax_rate")),
            "tax_amount": tax_amount,
            "line_subtotal": line_subtotal,
            "line_total": line_total,
        })

    branch = branch or {}
    identity = normalize_receipt_identity(
        receipt_identity or {"brand_name": pos_name},
        branch,
    )
    return {
        "version": RECEIPT_SNAPSHOT_VERSION,
        "transaction_id": str(transaction_id),
        "sale_datetime": _iso(sale_date),
        "pos_name": str(pos_name or "Parrot POS"),
        "receipt_identity": identity,
        "currency_code": str(currency_code or "USD"),
        "currency_suffix": str(currency_suffix or "$"),
        "branch": {
            "name": str(branch.get("name") or ""),
            "code": str(branch.get("code") or ""),
            "address": str(branch.get("address") or ""),
            "phone": str(branch.get("phone") or ""),
            "email": str(branch.get("email") or ""),
        },
        "cashier": {"name": str(cashier_name or "Unknown")},
        "payment": {
            "method": str(payment_method or "unknown"),
            "cash_received": None if cash_received is None else _money(cash_received),
            "change_given": _money(change_given),
            "breakdown": {
                str(method): _money(amount)
                for method, amount in (payment_breakdown or {}).items()
            },
        },
        "items": snapshot_items,
        "subtotal": _money(subtotal),
        "tax": _money(tax),
        "total": _money(total),
    }


def format_receipt_money(value: Any, suffix: str) -> str:
    return f"{_money(value):,.2f} {suffix}".strip()


def build_delivery_slip_view(slip: Mapping[str, Any], paper_size: Any) -> dict[str, Any]:
    """Build the print view for a driver's delivery slip from plain data.

    ``slip`` is a plain mapping assembled by the caller (delivery fields,
    packing items and money totals). Money values are quantized with the same
    helper used for sale receipts so both documents stay consistent.
    """
    slip = slip or {}
    profile = get_paper_profile(paper_size)
    suffix = str(slip.get("currency_suffix") or "$")
    branch = dict(slip.get("branch") or {})
    stored_identity = slip.get("receipt_identity")
    if stored_identity:
        identity = normalize_receipt_identity(dict(stored_identity), branch)
    else:
        identity = normalize_receipt_identity(
            {"brand_name": slip.get("brand_name") or DEFAULT_RECEIPT_BRAND_NAME},
            branch,
        )

    items = []
    for raw_item in slip.get("items") or []:
        item = dict(raw_item)
        try:
            quantity = int(item.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        items.append({
            "name": str(item.get("name") or "Item"),
            "quantity": quantity,
        })

    order_total = _money(slip.get("order_total"))
    delivery_fee = _money(slip.get("delivery_fee"))
    collect_total = _money(slip.get("collect_total"))

    created_at = str(slip.get("created_at") or "")
    try:
        created_at = datetime.fromisoformat(created_at).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        pass

    delivery_address = str(slip.get("delivery_address") or "")

    return {
        "paper_size": normalize_receipt_paper_size(paper_size),
        "paper": profile,
        "is_narrow": profile["width_mm"] == 58,
        "brand_name": identity["brand_name"],
        "logo_filename": identity["logo_filename"],
        "email": identity["email"],
        "phone": identity["phone"],
        "address_lines": identity["address"].splitlines() if identity["address"] else [],
        "branch": branch,
        "delivery_number": str(slip.get("delivery_number") or ""),
        "stage_label": str(slip.get("stage_label") or slip.get("stage") or ""),
        "priority": str(slip.get("priority") or "normal").capitalize(),
        "created_at": created_at,
        "sale_transaction_id": str(slip.get("sale_transaction_id") or ""),
        "payment_method": str(slip.get("payment_method") or "").replace("_", " ").title(),
        "recipient_name": str(slip.get("recipient_name") or ""),
        "recipient_phone": str(slip.get("recipient_phone") or ""),
        "delivery_address_lines": delivery_address.splitlines() if delivery_address else [],
        "township": str(slip.get("township") or ""),
        "instructions": str(slip.get("instructions") or ""),
        "courier_name": str(slip.get("courier_name") or ""),
        "courier_phone": str(slip.get("courier_phone") or ""),
        "tracking_code": str(slip.get("tracking_code") or ""),
        "items": items,
        "order_total_display": format_receipt_money(order_total, suffix),
        "delivery_fee_display": format_receipt_money(delivery_fee, suffix),
        "collect_total_display": format_receipt_money(collect_total, suffix),
    }


def build_receipt_view(snapshot: Mapping[str, Any], paper_size: Any) -> dict[str, Any]:
    profile = get_paper_profile(paper_size)
    suffix = str(snapshot.get("currency_suffix") or "$")
    transaction_id = str(snapshot.get("transaction_id") or "")
    payment = dict(snapshot.get("payment") or {})
    payment_breakdown = dict(payment.get("breakdown") or {})
    branch = dict(snapshot.get("branch") or {})
    cashier = dict(snapshot.get("cashier") or {})
    stored_identity = snapshot.get("receipt_identity")
    if stored_identity:
        identity = normalize_receipt_identity(dict(stored_identity))
    else:
        # Version-1 snapshots stored the title and contact details separately.
        identity = normalize_receipt_identity(
            {"brand_name": snapshot.get("pos_name") or DEFAULT_RECEIPT_BRAND_NAME},
            branch,
        )

    raw_date = snapshot.get("sale_datetime")
    display_date = str(raw_date or "")
    try:
        display_date = datetime.fromisoformat(display_date).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        pass

    items = []
    for raw_item in snapshot.get("items") or []:
        item = dict(raw_item)
        items.append({
            **item,
            "unit_price_display": format_receipt_money(item.get("unit_price"), suffix),
            "line_subtotal_display": format_receipt_money(item.get("line_subtotal"), suffix),
            "tax_amount_display": format_receipt_money(item.get("tax_amount"), suffix),
            "line_total_display": format_receipt_money(item.get("line_total"), suffix),
            "tax_rate_display": f"{_money(item.get('tax_rate')):g}%",
        })

    return {
        "paper_size": normalize_receipt_paper_size(paper_size),
        "paper": profile,
        "is_narrow": profile["width_mm"] == 58,
        "transaction_id": transaction_id,
        "receipt_number": transaction_id[-8:].upper() if transaction_id else "UNKNOWN",
        "date": display_date,
        "pos_name": identity["brand_name"],
        "brand_name": identity["brand_name"],
        "logo_filename": identity["logo_filename"],
        "email": identity["email"],
        "phone": identity["phone"],
        "address_lines": identity["address"].splitlines() if identity["address"] else [],
        "footer_lines": identity["footer_message"].splitlines() if identity["footer_message"] else [],
        "branch": branch,
        "cashier_name": str(cashier.get("name") or "Unknown"),
        "payment_method": str(payment.get("method") or "unknown").replace("_", " ").title(),
        "is_cash": str(payment.get("method") or "").lower() == "cash",
        "is_split": str(payment.get("method") or "").lower() == "split_payment",
        "payment_breakdown": [
            {
                "method": str(method).replace("_", " ").title(),
                "amount_display": format_receipt_money(amount, suffix),
            }
            for method, amount in payment_breakdown.items()
        ],
        "items": items,
        "subtotal_display": format_receipt_money(snapshot.get("subtotal"), suffix),
        "tax_display": format_receipt_money(snapshot.get("tax"), suffix),
        "total_display": format_receipt_money(snapshot.get("total"), suffix),
        "cash_received_display": format_receipt_money(payment.get("cash_received"), suffix),
        "change_display": format_receipt_money(payment.get("change_given"), suffix),
    }


def build_return_exchange_view(record: Mapping[str, Any], paper_size: Any) -> dict[str, Any]:
    """Build the thermal print view for a return/exchange workflow.

    ``record`` is a plain mapping assembled by the caller (workflow metadata,
    returned lines, exchanged lines and money totals). Money values are
    quantized with the same helper used for sale receipts so every document
    stays consistent. Like :func:`build_delivery_slip_view` this is built on the
    fly (no stored snapshot), so a reprint always reflects current catalog names
    and the current branch identity.
    """
    record = record or {}
    profile = get_paper_profile(paper_size)
    suffix = str(record.get("currency_suffix") or "$")
    branch = dict(record.get("branch") or {})
    stored_identity = record.get("receipt_identity")
    if stored_identity:
        identity = normalize_receipt_identity(dict(stored_identity), branch)
    else:
        identity = normalize_receipt_identity(
            {"brand_name": record.get("brand_name") or DEFAULT_RECEIPT_BRAND_NAME},
            branch,
        )

    def _lines(raw_lines: Any) -> list[dict[str, Any]]:
        lines = []
        for raw_line in raw_lines or []:
            line = dict(raw_line)
            try:
                quantity = int(line.get("quantity") or 0)
            except (TypeError, ValueError):
                quantity = 0
            unit_price = _money(line.get("unit_price"))
            tax_amount = _money(line.get("line_tax"))
            line_subtotal = _money(line.get("line_total"))
            tax_rate = _money(line.get("tax_rate"))
            lines.append({
                "name": str(line.get("name") or "Item"),
                "quantity": quantity,
                "unit_price_display": format_receipt_money(unit_price, suffix),
                "line_subtotal_display": format_receipt_money(line_subtotal, suffix),
                "tax_amount": tax_amount,
                "tax_amount_display": format_receipt_money(tax_amount, suffix),
                "tax_rate_display": f"{tax_rate:g}%",
                "line_total_display": format_receipt_money(line_subtotal + tax_amount, suffix),
            })
        return lines

    return_items = _lines(record.get("return_items"))
    exchange_items = _lines(record.get("exchange_items"))
    if return_items and exchange_items:
        document_title = "RETURN & EXCHANGE"
    elif exchange_items:
        document_title = "EXCHANGE"
    else:
        document_title = "RETURN"

    net_total = _money(record.get("net_total"))
    refund_amount = _money(record.get("refund_amount"))
    collected_amount = _money(record.get("collected_amount"))

    settlement = str(record.get("settlement_method") or "").strip().replace("_", " ")
    settlement_label = settlement.title() if settlement else ""

    workflow_id = str(record.get("workflow_id") or "")
    created_at = str(record.get("created_at") or "")
    try:
        created_at = datetime.fromisoformat(created_at).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        pass

    return {
        "paper_size": normalize_receipt_paper_size(paper_size),
        "paper": profile,
        "is_narrow": profile["width_mm"] == 58,
        "brand_name": identity["brand_name"],
        "logo_filename": identity["logo_filename"],
        "email": identity["email"],
        "phone": identity["phone"],
        "address_lines": identity["address"].splitlines() if identity["address"] else [],
        "footer_lines": identity["footer_message"].splitlines() if identity["footer_message"] else [],
        "branch": branch,
        "document_title": document_title,
        "workflow_number": workflow_id[-8:].upper() if workflow_id else "UNKNOWN",
        "mode": str(record.get("mode") or "").strip().lower(),
        "original_transaction_id": str(record.get("original_transaction_id") or ""),
        "adjustment_transaction_id": str(record.get("adjustment_transaction_id") or ""),
        "created_at": created_at,
        "processed_by": str(record.get("processed_by") or "Unknown"),
        "notes": str(record.get("notes") or ""),
        "settlement_label": settlement_label,
        "return_items": return_items,
        "exchange_items": exchange_items,
        "return_total_display": format_receipt_money(record.get("return_total"), suffix),
        "exchange_total_display": format_receipt_money(record.get("exchange_total"), suffix),
        "net_total": net_total,
        "net_total_display": format_receipt_money(net_total, suffix),
        "refund_amount": refund_amount,
        "refund_amount_display": format_receipt_money(refund_amount, suffix),
        "collected_amount": collected_amount,
        "collected_amount_display": format_receipt_money(collected_amount, suffix),
        "has_return": bool(return_items),
        "has_exchange": bool(exchange_items),
        "is_refund": refund_amount > 0,
        "is_collect": collected_amount > 0,
    }