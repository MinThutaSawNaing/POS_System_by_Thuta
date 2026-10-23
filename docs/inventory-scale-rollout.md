# Bounded inventory registers and rollout

Implementation date: October 23, 2026. This document describes the changes after
the original source review in `inventory-scalability-review.md`. Synthetic test
results are not a certification of a production deployment's throughput.

## API and interaction changes

- Products management, shared Warehouse, and the shared Purchase register use
  descending-ID seek pagination: 30 rows in the dashboard, a hard API maximum of
  100. Responses contain `items`, `has_more`, and `next_cursor`, not exact totals.
  Previous navigation reuses a client-side cursor stack. Search/filter/identity
  changes start a new stack; obsolete responses cannot overwrite the active view.
- Cursor tokens are authenticated and bound to endpoint, user, active branch,
  page size, and filters. They are continuation tokens, not arbitrary page
  numbers or a snapshot: concurrent inserts/deletions can change refreshed pages.
  Clients must retain all original query parameters when requesting another page.
- Scale-view searches are **literal prefixes**, case-insensitive according to
  SQLite's default LIKE behavior (ASCII folding, not full Unicode case folding).
  `%` and `_` are literal input. Name/barcode/category and PO-number/supplier
  prefix probes use matching indexes. Page candidates are bounded inside each
  probe rather than materializing the union of every matching ID. Category prefix
  matches go through the canonical `category` rows (and `idx_product_category_id`);
  the legacy free-text `product.category` column is not probed on the bounded
  path because it has no usable prefix index.
- Purchase product choices and supplier lookups are remote, debounced, bounded
  searches, with exact-ID lookup for retaining selections outside current results.
  A local searchable control is no longer fed the complete product catalog.
  Branch-specific promotion/exchange/label choices use the branch-scoped
  `/api/products?view=lookup` contract; purchase choices intentionally span all
  catalog branches. Dashboard catalog counts come from a SQL summary rather than
  the length of a partial product cache. Offline caches are bounded snapshots,
  not a promise that the complete million-row catalog is available offline.
- Legacy array endpoints remain arrays, but Products, Warehouse, and Purchase
  lists return at most 100 records. External integrations that previously assumed
  complete arrays must migrate to the cursor APIs. The separate POS-grid cursor
  contract is unchanged. Legacy numbered Products requests are limited to the
  first 100 pages and at most 100 rows per page.
- Warehouse quantities and purchase-line counts/totals are grouped in SQL for
  just the selected page. Summary queries aggregate in SQL without hydrating
  inventory objects. Warehouse valuation retains per-batch Decimal half-up
  rounding through an integer-cent SQLite function, rather than changing money
  semantics to binary floating-point summation.
- Category selectors can request `counts=false`; normal category counts use SQL
  groups, not relationship collection loads.

## Startup cost

Startup used to repeat legacy NULL-branch/category backfills and other
upgrade-only `UPDATE ... WHERE ... IS NULL` statements on **every** boot. On a
million-row catalogue that is a full-table write per table per restart; the
synthetic benchmark could not finish booting until this was fixed. Those
backfills now run only while `branch_stabilization_v2` is absent from
`schema_migration`, i.e. during the one-time upgrade, and the marker is still
written only after every startup migration succeeds so an interrupted upgrade
repeats safely. Index creation remains `IF NOT EXISTS` and is also one-time.

Remaining startup work that still scales with retained history: the audit-detail
compaction walks the audit table in bounded batches looking for un-migrated rows.
Measure it on staging with real audit volume; if it becomes material, gate it the
same way once its migration is known complete.

## Measured synthetic results (October 23, 2026)

`benchmark_inventory_scale.py --rows 1000000 --repeats 3` on a disposable
checkout: one million rows in **each** of `product`, `purchase_order`,
`purchase_order_item`, and `warehouse_inventory` (seed ~114 s), planner on
SQLite defaults, median of three requests through the real HTTP routes. This is
one machine with a slow disk — evidence of shape and hot spots, not a
production SLA.

| Route | Median | Notes |
| --- | ---: | --- |
| Products page 1 | 8.5 ms | 30 rows, next page 8.1 ms |
| Warehouse page 1 | 8.2 ms | 30 rows, next page 15.4 ms |
| Purchase register page 1 | 17.5 ms | 30 rows, next page 31.2 ms |
| Warehouse (low stock) | 9.7 ms | filter in SQL |
| Purchase search (PO no.) | 7.8 ms | indexed prefix |
| Purchase search (supplier) | 5.4 ms | indexed prefix |
| Warehouse search (rare name) | 5.5 ms | indexed prefix |
| Product/supplier remote lookups | 3.8 / 4.3 ms | 30-row cap |
| Categories without counts | 4.3 ms | no catalog loads |
| **Products search (rare name)** | **564 ms** | planner prefers the ID index for ordering |
| **Products search (rare barcode)** | **752 ms** | same cause |
| **Products search (broad, ~1M matches)** | **993 ms** | bounded to 30 rows, but the match set is huge |
| **Warehouse search (broad)** | **867 ms** | same |
| **Purchase search (broad)** | **484 ms** | same |
| **Warehouse summary** | **1.85 s** | four aggregates over 1M batches |
| **Purchase summary** | **1.01 s** | status/month CASE aggregates over 1M orders |

Browsing, filtering, lookups and paging stay in the low-millisecond range. Two
areas remain visibly expensive at this scale and are the next optimization
targets, not current guarantees:

- **Product search ordering.** `ORDER BY id DESC LIMIT 30` lets the planner walk
  the branch/ID index and test the prefix on each row instead of seeking the
  name/barcode index, so a low-hit search costs roughly a scan. Servable and
  bounded, but ~0.5–1 s here. The fix is an index-ordered search cursor
  (`(name, id)` or `(barcode, id)`), which changes result ordering and cursor
  encoding and therefore needs its own design and regression pass.
- **Register summaries.** Exact status counts and money totals over a million
  orders/batches are inherently aggregate scans. Making them feel instant needs a
  maintained summary/rollup table (or cached daily totals), which must be updated
  inside the same transactions that create sales, receipts and transfers.

Both numbers are honest measurements from this environment; they are not
estimates taken from production.

## Explicit work limits

- Synchronous PDF/Excel exports accept at most **5,000 rows**, and purchase
  exports also accept at most **5,000 line items**. Oversize reports return HTTP
  413 with a request to narrow filters, before ORM graphs/reports are built.
  There is no background export queue in this change.
- New purchase orders, receipt payloads, general detail modals, and single-order
  prints have a **500-line limit**. Oversized legacy general details/prints are
  rejected, never silently truncated. Receiving has a separate bounded line
  cursor (`/api/purchase_orders/{id}?view=receiving&per_page=30`) so legacy large
  orders can be processed in smaller receipts without downloading all lines.
- Warehouse transfers sum availability in SQL, read FIFO batches in chunks of
  100, and reject transfers spanning more than 500 batches. Rejection rolls back
  the entire transfer; split the requested quantity into smaller operations.
- These limits bound request memory/work; they do not make broad searches,
  catalog-wide summaries, index building, or SQLite writer locks constant-time.

## Receipt retries

The HTTP receipt endpoint accepts `idempotency_key` in JSON or the
`Idempotency-Key` header. Use a fresh, unpredictable key for each intended
receipt and retain the exact request for retry. Successful effects and the
response ledger commit in one transaction. Identical retries replay that result,
even after the order is fully received; reusing a key with a different actor,
order, receipt mode, or normalized quantity map returns HTTP 409. Validation
failures do not reserve keys. Old unkeyed callers retain atomicity but **cannot
claim retry deduplication**.

The dashboard stores the pending exact receipt body in per-user/per-order tab
session storage before sending. An ambiguous failure retains it. Reopening the
order allows retrying the original receipt rather than inventing a new key.
Closing the tab/clearing storage loses that client retry record: reconcile the
order's received quantities before beginning a replacement receipt. The durable
server ledger must remain intact across database backups/restores and cleanup.

## Deployment checklist

1. Stop production writes and take a verified external backup of the SQLite
   database and uploads, following `MIGRATION_NOTES.md`. Do not run tests or the
   synthetic benchmark against production data.
2. Deploy to a staging copy first. Startup creates the independent
   `receipt_request` ledger and query-aligned indexes with `IF NOT EXISTS`.
   Creating indexes on million-row tables needs disk headroom and a maintenance
   window; allow startup to finish before admitting traffic. Existing row IDs
   and historical quantities are not rewritten by these additions.
3. Verify `PRAGMA index_list`/`EXPLAIN QUERY PLAN` on the actual staging database.
   Run `PRAGMA optimize` during an approved maintenance window so SQLite has
   useful planner statistics after large data changes/index creation.
4. Execute `python run_tests.py -q --tb=short --disable-warnings`. The runner
   copies source to a disposable checkout and excludes local data and secrets.
5. Run the following from the repository root:

   ```text
   python benchmark_inventory_scale.py --rows 1000000 --repeats 3 --output .scratch/inventory-scale.json
   ```

   This launcher copies allowlisted source to a
   secret-free temporary checkout, creates a fresh synthetic database with one
   million rows in **each** of product/order/order-line/warehouse tables, records
   request timings/query plans, and deletes the database. Output must be a new
   JSON file; no production DB option exists. Statistics collection is opt-in
   (`--analyze`) because a full `ANALYZE` scans index pages and can dominate
   setup on a slow disk. By default SQLite plans with its built-in ~1,048,576-row
   estimate — the scale under test — so treat the numbers as a **pessimistic
   planning** signal, not production statistics.
6. Repeat with realistic production hardware, category/supplier skew, long batch
   histories, cold OS caches, and concurrent receiving/transfers/sales. Define
   latency, memory, and write-contention budgets before approving the rollout.
7. Check desktop/mobile scrolling, sticky headers, keyboard selection, filter
   resets, Previous/Next navigation, export rejection messages, and lost-response
   receipt replay in a real browser. Source/Node tests do not replace this check.

SQLite remains a single-writer database. The new bounds and idempotency protect
correctness and resource usage; measured write contention, not row count alone,
should determine whether a database-engine migration is required.

## Validation status for this change

- Isolated suite (`python run_tests.py -q --tb=short --disable-warnings`):
  **1113 passed, 161 skipped**, including the new cursor/scale, summary/FIFO,
  idempotent-receipt, browser-harness and legacy-upgrade regressions.
- Synthetic million-row benchmark: numbers in the table above.
- **Not** performed here: a real browser pass, production-shaped skew, cold
  caches, or concurrent write load. Those remain release gates.
