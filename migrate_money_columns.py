#!/usr/bin/env python3
"""SQLite Float -> NUMERIC(12,2) migration for POS money columns.

Strategy: copy-and-swap per table.
  1. Timestamped file backup of the database.
  2. For each table with target money columns still declared FLOAT:
     - create <table>_new with identical schema except NUMERIC(12,2) money cols
     - INSERT INTO <table>_new SELECT ... CAST(money_col AS NUMERIC(12,2)) ...
     - verify row counts equal AND every money-column SUM differs < 0.005
     - inside one transaction: rename original -> <table>_float_backup,
       rename <table>_new -> <table>
  3. Any check failure -> abort and restore from the file backup.

Idempotent: tables whose money columns are already NUMERIC are skipped.
Restore:   python migrate_money_columns.py --restore
Dry run:   python migrate_money_columns.py --dry-run
"""
import argparse
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "instance" / "pos.db"
TOL = 0.005

# table -> list of money columns to convert
MONEY_COLUMNS = {
    "product": ["price", "cost", "tax_rate"],
    "sale": ["total", "tax", "cash_received", "refund_amount"],
    "sale_item": ["price", "tax"],
    "promotion": ["discount_value"],
    "return_exchange": ["return_total", "exchange_total", "net_total",
                        "refund_amount", "collected_amount"],
    "return_exchange_item": ["unit_price", "tax_rate", "line_total", "line_tax"],
    "debt": ["amount", "balance"],
    "debt_payment": ["amount"],
    "warehouse_inventory": ["unit_cost"],
    "purchase_order": ["total_amount"],
    "purchase_order_item": ["unit_cost"],
    "supplier_price_agreement": ["agreed_price"],
    "delivery": ["delivery_fee"],
}


def table_columns(conn, table):
    return [(r[1], (r[2] or "").upper(), r[3], r[5]) for r in
            conn.execute(f'PRAGMA table_info("{table}")')]


def create_sql(conn, table):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()
    return row[0] if row else None


def needs_migration(conn, table, cols):
    """True if at least one target column is not already NUMERIC."""
    types = {name: typ for name, typ, _, _ in table_columns(conn, table)}
    return any(not types.get(c, "").startswith("NUMERIC") for c in cols)


def rename_table_in_create_sql(sql, old, new):
    """Rename the table in a CREATE TABLE statement (quoted or unquoted)."""
    pattern = rf'(?is)(CREATE\s+TABLE\s+"?)({re.escape(old)})("?\s*\()'
    result, n = re.subn(pattern, lambda m: m.group(1) + new + m.group(3), sql, count=1)
    if n != 1:
        raise ValueError(f"could not locate table name {old!r} in CREATE statement")
    return result


def build_new_create_sql(orig_sql, cols):
    """Rewrite the CREATE TABLE statement, declaring money cols NUMERIC(12,2)."""
    new_sql = orig_sql
    for c in cols:
        pattern = (rf'(?i)(?<![\w"])(("?{re.escape(c)}"?))(\s+)'
                   r'(FLOAT|REAL|DOUBLE)(\s*\(\s*\d+\s*\))?')
        new_sql, n = re.subn(pattern, r'\1\3NUMERIC(12, 2)', new_sql, count=1)
        if n != 1:
            raise ValueError(f"column {c!r} not found with FLOAT/REAL type in schema")
    return new_sql


def migrate_table(conn, table, cols, report, dry_run):
    orig_sql = create_sql(conn, table)
    if not orig_sql:
        report.append(f"[SKIP] {table}: not found in database")
        return False
    new_sql = build_new_create_sql(orig_sql, cols)
    if "NUMERIC(12, 2)" not in new_sql:
        report.append(f"[FAIL] {table}: could not rewrite CREATE TABLE for {cols}")
        return False

    col_list = ", ".join(f'"{c}"' for c in
                         [c for c, *_ in table_columns(conn, table)])
    cast_list = ", ".join(
        f'CAST("{c}" AS NUMERIC(12, 2))' if c in cols else f'"{c}"'
        for c, *_ in table_columns(conn, table))

    if dry_run:
        report.append(f"[DRY] would migrate {table} columns {cols}")
        return True

    conn.execute(f'DROP TABLE IF EXISTS "{table}_new"')
    conn.execute(rename_table_in_create_sql(new_sql, table, f"{table}_new"))
    conn.execute(f'INSERT INTO "{table}_new" ({col_list}) SELECT {cast_list} FROM "{table}"')

    # --- verification gates ---
    old_n = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    new_n = conn.execute(f'SELECT COUNT(*) FROM "{table}_new"').fetchone()[0]
    if old_n != new_n:
        report.append(f"[FAIL] {table}: row count {old_n} != {new_n}")
        return False
    for c in cols:
        old_s = conn.execute(f'SELECT COALESCE(SUM("{c}"), 0) FROM "{table}"').fetchone()[0] or 0
        new_s = conn.execute(f'SELECT COALESCE(SUM("{c}"), 0) FROM "{table}_new"').fetchone()[0] or 0
        if abs(old_s - new_s) >= TOL:
            report.append(f"[FAIL] {table}.{c}: SUM {old_s} vs {new_s} (diff >= {TOL})")
            return False
        report.append(f"[OK] {table}.{c}: rows={new_n} sum {old_s} -> {new_s}")

    # --- swap inside one transaction, keep original as backup ---
    conn.execute("BEGIN")
    try:
        conn.execute(f'DROP TABLE IF EXISTS "{table}_float_backup"')
        conn.execute(f'ALTER TABLE "{table}" RENAME TO "{table}_float_backup"')
        conn.execute(f'ALTER TABLE "{table}_new" RENAME TO "{table}"')
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return True

    """Rewrite the CREATE TABLE statement, declaring money cols NUMERIC(12,2)."""
    out_lines = []
    target = set(cols)
    for line in orig_sql.splitlines():
        stripped = line.strip().rstrip(",")
        first = stripped.split("(")[0].split()[0].strip('"').lower() if stripped else ""
        if first in target and re.match(r'(?i)^\s*"?\w+"?\s+(FLOAT|REAL|DOUBLE)\b', line):
            line = re.sub(r'(?i)\b(FLOAT|REAL|DOUBLE)\b(\s*\(\s*\d+\s*\))?',
                          "NUMERIC(12, 2)", line, count=1)
        out_lines.append(line)
    return "\n".join(out_lines)


def run_migration(dry_run=False):
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        return 1
    backup = DB_PATH.with_name(
        DB_PATH.stem + f"_pre_numeric_{datetime.now():%Y%m%d_%H%M%S}.db")
    if not dry_run:
        shutil.copy2(DB_PATH, backup)
        print(f"Backup written: {backup}")

    conn = sqlite3.connect(str(DB_PATH), isolation_level=None)  # explicit txn control
    report = []
    failures = []
    try:
        for table, cols in MONEY_COLUMNS.items():
            if not create_sql(conn, table):
                report.append(f"[SKIP] {table}: table absent")
                continue
            if not needs_migration(conn, table, cols):
                report.append(f"[SKIP] {table}: money columns already NUMERIC")
                continue
            try:
                ok = migrate_table(conn, table, cols, report, dry_run)
            except Exception as exc:  # noqa: BLE001
                report.append(f"[FAIL] {table}: {exc}")
                ok = False
            if not ok and not dry_run:
                failures.append(table)
                break  # restore from file backup below
        if failures and not dry_run:
            print("\n!!! FAILURE DETECTED — restoring from backup !!!")
            conn.close()
            shutil.copy2(backup, DB_PATH)
            print(f"Database restored from {backup}")
            print("\n".join(report))
            return 2
        conn.commit()
    finally:
        conn.close()

    print("\n===== VERIFICATION REPORT =====")
    print("\n".join(report))
    print("===============================")
    print("RESULT: SUCCESS" if not dry_run else "RESULT: DRY RUN OK")
    return 0


def run_restore():
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        return 1
    conn = sqlite3.connect(str(DB_PATH), isolation_level=None)
    restored = []
    for table in MONEY_COLUMNS:
        if create_sql(conn, f"{table}_float_backup"):
            conn.execute("BEGIN")
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')
            conn.execute(f'ALTER TABLE "{table}_float_backup" RENAME TO "{table}"')
            conn.execute("COMMIT")
            restored.append(table)
    conn.close()
    print(f"Restored tables: {restored or 'none found'}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore", action="store_true",
                    help="swap back from <table>_float_backup tables")
    args = ap.parse_args()
    if args.restore:
        sys.exit(run_restore())
    sys.exit(run_migration(dry_run=args.dry_run))


if __name__ == "__main__":
    main()

