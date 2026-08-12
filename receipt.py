"""Receipt snapshot, formatting, and thermal paper helpers."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Mapping


RECEIPT_PAPER_58MM = "THERMAL_58MM"
RECEIPT_PAPER_80MM = "THERMAL_80MM"
RECEIPT_PAPER_OPTIONS = {RECEIPT_PAPER_58MM, RECEIPT_PAPER_80MM}
DEFAULT_RECEIPT_PAPER_SIZE = RECEIPT_PAPER_80MM
RECEIPT_SNAPSHOT_VERSION = 1

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


def calculate_thermal_page_height_mm(content_height_px: Any, safety_mm: int = 3) -> int:
    """Convert browser CSS pixels to a cutter-safe whole millimetre height."""
    try:
        height_px = float(content_height_px)
    except (TypeError, ValueError):
        height_px = 0
    if height_px < 0 or height_px != height_px or height_px == float("inf"):
        height_px = 0
    return max(20, int(((height_px / 96) * 25.4) + safety_mm + 0.999999))


def _money(value: Any) -> float:
    return float(Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


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
    items: Iterable[Mapping[str, Any]],
    subtotal: Any,
    tax: Any,
    total: Any,
) -> dict[str, Any]:
    snapshot_items = []
    for item in items:
        quantity = int(item.get("quantity") or 0)
        unit_price = _money(item.get("unit_price"))
        tax_amount = _money(item.get("tax_amount"))
        line_subtotal = _money(Decimal(str(unit_price)) * quantity)
        snapshot_items.append({
            "product_id": item.get("product_id"),
            "name": str(item.get("name") or "Unavailable item"),
            "quantity": quantity,
            "unit_price": unit_price,
            "tax_rate": _money(item.get("tax_rate")),
            "tax_amount": tax_amount,
            "line_subtotal": line_subtotal,
            "line_total": _money(Decimal(str(line_subtotal)) + Decimal(str(tax_amount))),
        })

    branch = branch or {}
    return {
        "version": RECEIPT_SNAPSHOT_VERSION,
        "transaction_id": str(transaction_id),
        "sale_datetime": _iso(sale_date),
        "pos_name": str(pos_name or "Parrot POS"),
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
        },
        "items": snapshot_items,
        "subtotal": _money(subtotal),
        "tax": _money(tax),
        "total": _money(total),
    }


def format_receipt_money(value: Any, suffix: str) -> str:
    return f"{_money(value):,.2f} {suffix}".strip()


def build_receipt_view(snapshot: Mapping[str, Any], paper_size: Any) -> dict[str, Any]:
    profile = get_paper_profile(paper_size)
    suffix = str(snapshot.get("currency_suffix") or "$")
    transaction_id = str(snapshot.get("transaction_id") or "")
    payment = dict(snapshot.get("payment") or {})
    branch = dict(snapshot.get("branch") or {})
    cashier = dict(snapshot.get("cashier") or {})

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
        "pos_name": str(snapshot.get("pos_name") or "Parrot POS"),
        "branch": branch,
        "cashier_name": str(cashier.get("name") or "Unknown"),
        "payment_method": str(payment.get("method") or "unknown").replace("_", " ").title(),
        "is_cash": str(payment.get("method") or "").lower() == "cash",
        "items": items,
        "subtotal_display": format_receipt_money(snapshot.get("subtotal"), suffix),
        "tax_display": format_receipt_money(snapshot.get("tax"), suffix),
        "total_display": format_receipt_money(snapshot.get("total"), suffix),
        "cash_received_display": format_receipt_money(payment.get("cash_received"), suffix),
        "change_display": format_receipt_money(payment.get("change_given"), suffix),
    }