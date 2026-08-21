# MIGRATION NOTES — Float → NUMERIC(12,2) Money Columns

## Scope

All money columns stored as SQLite `FLOAT`/`REAL` are converted to
`NUMERIC(12, 2)` via `CAST(col AS NUMERIC(12, 2))`. Enumerated from the
SQLAlchemy models in `app.py` **and** confirmed by `PRAGMA table_info`
against a temp copy of `instance/pos.db`.

## Enumerated Float money columns (confirmed in live DB)

| Table | Columns |
|---|---|
| `product` | `price`, `cost`, `tax_rate` |
| `sale` | `total`, `tax`, `cash_received`, `refund_amount` |
| `sale_item` | `price`, `tax` |
| `promotion` | `discount_value` |
| `return_exchange` | `return_total`, `exchange_total`, `net_total`, `refund_amount`, `collected_amount` |
| `return_exchange_item` | `unit_price`, `tax_rate`, `line_total`, `line_tax` |
| `debt` | `amount`, `balance` |
| `debt_payment` | `amount` |
| `warehouse_inventory` | `unit_cost` |
| `purchase_order` | `total_amount` (stored as `REAL`) |
| `purchase_order_item` | `unit_cost` |
| `supplier_price_agreement` | `agreed_price` (model attr `SupplierPriceAgreement.agreed_price`; note: not `unit_price`) |
| `delivery` | `delivery_fee` |

Deliberately excluded (Float but NOT money): `supplier.quality_rating`,
`supplier.delivery_rating`.

## Migration procedure (`python migrate_money_columns.py [--dry-run]`)

Copy-and-swap per table:

1. Timestamped file backup: `instance/pos_pre_numeric_YYYYmmdd_HHMMSS.db`.
2. For each table whose money columns are not already NUMERIC:
   - Build `<table>_new` from the original `CREATE TABLE` SQL with money
     column types rewritten to `NUMERIC(12, 2)` (all constraints — PK, FK,
     UNIQUE, DEFAULT — preserved).
   - `INSERT INTO <table>_new SELECT ... CAST(money_col AS NUMERIC(12,2)) ...`
3. Verification gates per table:
   - Row counts of old vs new must be equal.
   - `SUM(col)` for every money column must differ by **< 0.005**.
4. Swap inside a single transaction:
   `ALTER TABLE <table> RENAME TO <table>_float_backup`;
   `ALTER TABLE <table>_new RENAME TO <table>`.
   The original Float table is kept as `<table>_float_backup`.
5. Full verification report printed at the end.

**Abort/rollback:** any check failure stops the run and restores the whole
database file from the timestamped backup.

**Idempotency:** tables whose target columns already report NUMERIC types are
skipped, so re-running is safe.

**Restore:** `python migrate_money_columns.py --restore` drops the converted
tables and renames each `<table>_float_backup` back to `<table>`.

## Verification gates summary

- Row count equality (old vs new) per table.
- Per-column SUM tolerance < 0.005.
- Atomic rename swap; failure ⇒ full file restore from backup.
- Dry-run mode performs no writes.

## Test coverage (`test_math_calculations.py`, 32 tests, all passing)

- `to_decimal` / `safe_to_decimal`: None, NaN, ±inf, garbage strings,
  negative zero, custom defaults.
- `round_money`: ROUND_HALF_UP (`2.675 -> 2.68`, `1.005 -> 1.01`,
  `-1.005 -> -1.01`), `money_float` boundary.
- Per-item tax rounding parity between client integer-cent `Math.round`
  logic and server Decimal ROUND_HALF_UP logic (≤ 0.01 tolerance).
- Promotion discounts: percent (33.33%), fixed, discount > price clamps to 0,
  100% = free.
- Cash/change exactness incl. fractional totals.
- Return/exchange `net_total = exchange_total - return_total` rounding,
  negative net when return exceeds exchange.
- Migration smoke test on a seeded tmp_path SQLite DB verifying counts and
  sums preserved within tolerance, plus idempotency check.

## Deployment recommendation

1. Run during **low traffic / maintenance window** (app stopped or idle);
   SQLite copy-and-swap is fast at this data size (~240 KB DB) but the app
   should not write mid-swap.
2. `python migrate_money_columns.py --dry-run` first; review the report.
3. Run the real migration; keep the timestamped backup file until the next
   business day closes without issue.
4. If anything looks wrong: `python migrate_money_columns.py --restore`,
   then restart the app.
5. Note: SQLAlchemy models in `app.py` still declare `db.Float`. SQLite does
   not enforce column types strictly, so runtime behavior is unchanged;
   updating the model declarations to `db.Numeric(12, 2)` can follow later.
