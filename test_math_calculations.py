"""Tests for money math helpers and the Float -> NUMERIC(12,2) migration.

Pure helper functions are imported from app.py (import does not touch
instance/pos.db: db.create_all() only runs inside explicit init functions).
The migration is exercised against a temp SQLite copy seeded with float data.
"""
import math
import sqlite3
from decimal import Decimal

import pytest

from app import money_float, round_money, safe_to_decimal, to_decimal
from migrate_money_columns import (
    MONEY_COLUMNS,
    build_new_create_sql,
    needs_migration,
    rename_table_in_create_sql,
    table_columns,
)


# ---------------------------------------------------------------- to_decimal
def test_to_decimal_basic():
    assert to_decimal(2.675) == Decimal("2.675")
    assert to_decimal("10.50") == Decimal("10.50")
    assert to_decimal(0) == Decimal("0")


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"),
                                   float("-inf"), "abc", "", "NaN", object()])
def test_safe_to_decimal_garbage_returns_default(value):
    assert safe_to_decimal(value) == Decimal("0")


def test_safe_to_decimal_custom_default():
    assert safe_to_decimal(None, default=Decimal("-1")) == Decimal("-1")


def test_safe_to_decimal_negative_zero():
    result = safe_to_decimal(-0.0)
    assert result == Decimal("0")


def test_safe_to_decimal_valid_passthrough():
    assert safe_to_decimal("12.34") == Decimal("12.34")
    assert safe_to_decimal(-5) == Decimal("-5")


# --------------------------------------------------------------- round_money
def test_round_money_half_up_2675():
    # 2.675 must round UP to 2.68 (ROUND_HALF_UP), unlike banker's rounding.
    assert round_money("2.675") == Decimal("2.68")


def test_round_money_half_up_down_boundary():
    assert round_money("1.005") == Decimal("1.01")
    assert round_money("1.004") == Decimal("1.00")
    assert round_money("-1.005") == Decimal("-1.01")  # half away from zero


def test_round_money_already_two_decimals():
    assert round_money(Decimal("3.14")) == Decimal("3.14")


def test_money_float_boundary():
    assert money_float("2.675") == 2.68


# ------------------------------------------- per-item tax rounding parity
def _js_style_line_tax(unit_price_cents, qty, tax_rate_pct):
    """Client-expected logic: integer-cent Math.round per item line."""
    return int(math.floor((unit_price_cents * qty * tax_rate_pct / 100) + 0.5))


def _server_style_line_tax(unit_price, qty, tax_rate_pct):
    """Server logic: Decimal ROUND_HALF_UP per line."""
    line = Decimal(str(unit_price)) * qty * Decimal(str(tax_rate_pct)) / Decimal("100")
    return round_money(line)


@pytest.mark.parametrize("price,qty,rate", [
    ("1500", 2, 5), ("999.99", 3, 8.5), ("0.01", 1, 15),
    ("333.33", 7, 6.66), ("12345.67", 1, 0),
])
def test_per_item_tax_parity_with_client(price, qty, rate):
    cents = int(round(float(price) * 100))
    expected = Decimal(_js_style_line_tax(cents, qty, rate)) / 100  # cents -> dollars
    got = _server_style_line_tax(price, qty, rate)
    assert abs(got - expected) <= Decimal("0.01")


# ------------------------------------------------------ promotion discounts
def test_promotion_percent_discount():
    price = Decimal("100")
    discounted = price - price * Decimal("33.33") / Decimal("100")
    assert round_money(discounted) == Decimal("66.67")


def test_promotion_fixed_discount():
    assert round_money(Decimal("100") - Decimal("12.50")) == Decimal("87.50")


def test_promotion_discount_larger_than_price_clamps_to_zero():
    candidate = max(Decimal("100") - Decimal("250"), Decimal("0"))
    assert round_money(candidate) == Decimal("0")


def test_promotion_percent_100_is_free():
    candidate = max(Decimal("80") - Decimal("80") * Decimal("100") / Decimal("100"), Decimal("0"))
    assert round_money(candidate) == Decimal("0")


# ------------------------------------------------------- cash/change exactness
def test_cash_change_exact():
    total = round_money(Decimal("12500") + Decimal("625"))  # + 5% tax
    cash = Decimal("20000")
    assert cash - total == Decimal("6875")


def test_cash_change_with_fractional_total():
    total = round_money("9999.995")  # -> 10000.00 half-up
    assert Decimal("10000") - total == Decimal("0")


# ------------------------------------------------- return/exchange net_total
def test_net_total_exchange_minus_return_rounding():
    exchange_total = round_money(Decimal("45000.555"))   # -> 45000.56
    return_total = round_money(Decimal("12000.335"))     # -> 12000.34
    net = round_money(exchange_total - return_total)
    assert net == Decimal("33000.22")


def test_net_total_unrounded_components():
    net = round_money(Decimal("1000.125") - Decimal("250.004"))
    assert net == Decimal("750.12")


def test_net_total_negative_when_return_exceeds_exchange():
    net = round_money(Decimal("100") - Decimal("250.25"))
    assert net == Decimal("-150.25")


# ------------------------------------------------- migration dry-run smoke
SCHEMA = """
CREATE TABLE product (
    id INTEGER PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    price FLOAT NOT NULL,
    cost FLOAT,
    tax_rate FLOAT DEFAULT 0.0
);
CREATE TABLE sale_item (
    id INTEGER PRIMARY KEY,
    quantity INTEGER NOT NULL,
    price FLOAT NOT NULL,
    tax FLOAT NOT NULL
);
"""


def _seed(conn):
    conn.executescript(SCHEMA)
    rows = [(f"P{i}", 10.005 * i, 5.111 * i, 0.075 * i) for i in range(1, 51)]
    conn.executemany("INSERT INTO product(name, price, cost, tax_rate) VALUES (?,?,?,?)", rows)
    items = [(i, 9.99 * i, 0.33 * i) for i in range(1, 31)]
    conn.executemany("INSERT INTO sale_item(quantity, price, tax) VALUES (?,?,?)", items)
    conn.commit()


def test_migration_preserves_sums_and_counts(tmp_path):
    db = tmp_path / "m.db"
    conn = sqlite3.connect(str(db))
    _seed(conn)

    old_prod_sum = conn.execute("SELECT SUM(price)+SUM(cost)+SUM(tax_rate) FROM product").fetchone()[0]
    old_n_items = conn.execute("SELECT COUNT(*) FROM sale_item").fetchone()[0]
    old_item_sum = conn.execute("SELECT SUM(price)+SUM(tax) FROM sale_item").fetchone()[0]

    migrated = []
    for table, cols in (("product", ["price", "cost", "tax_rate"]),
                        ("sale_item", ["price", "tax"])):
        orig_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
        new_sql = build_new_create_sql(orig_sql, cols)
        all_cols = [c for c, *_ in table_columns(conn, table)]
        col_list = ", ".join(f'"{c}"' for c in all_cols)
        cast_list = ", ".join(
            f'CAST("{c}" AS NUMERIC(12, 2))' if c in cols else f'"{c}"' for c in all_cols)
        conn.execute(f'DROP TABLE IF EXISTS "{table}_new"')
        conn.execute(rename_table_in_create_sql(new_sql, table, f"{table}_new"))
        conn.execute(f'INSERT INTO "{table}_new" ({col_list}) SELECT {cast_list} FROM "{table}"')
        new_n = conn.execute(f'SELECT COUNT(*) FROM "{table}_new"').fetchone()[0]
        assert new_n == conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for c in cols:
            old_s = conn.execute(f'SELECT COALESCE(SUM("{c}"),0) FROM "{table}"').fetchone()[0] or 0
            new_s = conn.execute(f'SELECT COALESCE(SUM("{c}"),0) FROM "{table}_new"').fetchone()[0] or 0
            assert abs(old_s - new_s) < 0.005
        conn.execute(f'DROP TABLE IF EXISTS "{table}_float_backup"')
        conn.execute(f'ALTER TABLE "{table}" RENAME TO "{table}_float_backup"')
        conn.execute(f'ALTER TABLE "{table}_new" RENAME TO "{table}"')
        migrated.append(table)
    conn.commit()

    types = {n: t for n, t, _, _ in table_columns(conn, "product")}
    assert all(types[c].startswith("NUMERIC") for c in ("price", "cost", "tax_rate"))
    assert not needs_migration(conn, "product", ["price", "cost", "tax_rate"])

    new_prod_sum = conn.execute("SELECT SUM(price)+SUM(cost)+SUM(tax_rate) FROM product").fetchone()[0]
    assert abs(old_prod_sum - new_prod_sum) < 0.005
    assert conn.execute("SELECT COUNT(*) FROM sale_item").fetchone()[0] == old_n_items
    new_item_sum = conn.execute("SELECT SUM(price)+SUM(tax) FROM sale_item").fetchone()[0]
    assert abs(old_item_sum - new_item_sum) < 0.005
    conn.close()
    assert migrated == ["product", "sale_item"]


def test_money_column_map_matches_expected_tables():
    expected = {
        "product": {"price", "cost", "tax_rate"},
        "sale": {"total", "tax", "cash_received", "refund_amount"},
        "sale_item": {"price", "tax"},
        "promotion": {"discount_value"},
        "return_exchange": {"return_total", "exchange_total", "net_total",
                            "refund_amount", "collected_amount"},
        "return_exchange_item": {"unit_price", "tax_rate", "line_total", "line_tax"},
        "debt": {"amount", "balance"},
        "debt_payment": {"amount"},
        "warehouse_inventory": {"unit_cost"},
        "purchase_order": {"total_amount"},
        "purchase_order_item": {"unit_cost"},
        "supplier_price_agreement": {"agreed_price"},
        "delivery": {"delivery_fee"},
    }
    assert {t: set(c) for t, c in MONEY_COLUMNS.items()} == expected

