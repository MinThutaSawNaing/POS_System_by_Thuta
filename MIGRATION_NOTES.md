# Migration notes

## October 2026 branch stabilization (v2)

This release changes database structure. Rehearse it on a copy of the existing
database before deploying, and use a maintenance window with only one starting
application process. Pushing `main` runs the regression-test gate before the
existing production deployment job.

### Backup and rollout

1. Stop writes and save a verified database backup outside the deployment volume:
   `python backup_database.py --database instance/pos.db --output /safe/path/pos-before-branch-v2.db`.
   This standalone command does **not** import the application or run migrations.
   It uses SQLite's backup API, includes committed WAL data, checks integrity,
   and refuses to overwrite an existing destination. Back up `uploads/` as well.
   For Docker, run the command against the mounted database inside the container,
   then copy the backup to independent storage before replacing the container.
2. Run the new version against a staging copy. Startup performs the migration;
   verify counts, category links, units, branch assignments, and representative
   sales/debt/warehouse records before allowing production writes.
3. Existing databases receive an additional automatic pre-upgrade snapshot next
   to the database, normally `pos.db.pre-branch-v2.bak`. An existing unmarked
   snapshot is never overwritten; a unique filename is used instead. Keep an
   external backup too: a snapshot on the same disk is not disaster recovery.
4. Startup records `branch_stabilization_v2` in `schema_migration` only after
   initialization succeeds. Restart the staging app a second time and verify
   that the migration is repeatable and records remain unchanged.
5. Run `python run_tests.py` before release. It copies source into a disposable
   directory, excludes local databases/uploads/secrets, and also isolates the
   live-server test subprocess. Python test dependencies plus Node are required.

### What changes

- Product barcodes are unique **within a branch**, not across all branches.
  Transfers preserve physical barcodes and never identify a product by its name.
  An internal `catalog_id` maintains transfer identity, including products with
  no barcode. Same-name products are not automatically merged.
- Legacy category/product uniqueness is rebuilt transactionally while retaining
  existing columns, indexes, triggers and record IDs. This avoids the previous
  hard-coded product rebuild dropping the unit-of-measure link or extra columns.
  New foreign-key errors abort the rebuild; pre-existing errors are not silently
  repaired by guessing relationships.
- Transfer metadata supports `idempotency_key` (or `Idempotency-Key` header).
  A retried identical request returns the recorded result; reuse for a different
  transfer is rejected. Stock deductions and destination increments commit
  together under a SQLite writer transaction. An independent `transfer_request`
  ledger retains deduplication results even after product/transfer-history
  cleanup. Existing keyed transfer records are backfilled on startup.
  The dashboard persists the outstanding request in per-user **tab session**
  storage: retry in that same tab after an uncertain result. Closing the tab or
  clearing its storage can lose that client key; check transfer history before
  starting a replacement transfer. External API clients must retain their keys.
  A catalog-linked destination whose physical barcode was changed is rejected
  until a manager explicitly reconciles its identity.
- Purchase receiving uses one shared transaction implementation for HTTP and AI:
  only approved/partially received orders can be received, concurrent receipts
  cannot exceed ordered quantities, duplicate lines are aggregated, and supplier
  order statistics count an order once rather than once per partial delivery.
- Product images shared by transferred catalog copies are retained while any
  branch still references them; failed edits no longer delete the old image
  before the database commit succeeds.
- Users and operational data remain branch-specific; suppliers, purchase orders,
  and warehouse stock are shared manager workflows. Existing unassigned users
  are attached to the active default/fallback branch; verify those assignments.
- Currency and AI configuration remain installation-wide. Receipt branding,
  logos, MMQR and printing geometry are branch overrides with legacy fallback.

### Rollback and existing data limitations

Stop **every** application process before restoring. Preserve a copy of the
failed/new database and uploads for diagnosis. Restore the verified pre-upgrade
database and matching uploads, remove/move aside that database's stale `-wal`
and `-shm` files while all connections are closed, and deploy the matching prior
application version. Restart and verify before reopening writes. A restore loses
transactions recorded after the backup; reconcile them before resuming trade.

This patch cannot reconstruct fields already lost by a previous migration. If
the October 19 update has already removed unit assignments, recover them from a
known-good backup. Likewise, existing branch-suffixed barcodes or same-name
products are **not** automatically merged or renamed: review their identity and
stock history explicitly before reconciling them. Do not infer identity from
names or barcode suffixes alone.

## Earlier Float → NUMERIC(12,2) money migration

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
