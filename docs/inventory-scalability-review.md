# Inventory tabs: million-row readiness review

> Historical baseline: subsequent remediation is described in
> `inventory-scale-rollout.md`. The findings below describe the code before that
> remediation, not the current cursor-register implementation.

Reviewed October 23, 2026. This is a source-code assessment, not a million-row
load-test certification or a verification of the deployed production database.
The scrollbar/page-size update does not resolve the backend limitations below.

## Verdict

| Screen | Current position |
| --- | --- |
| Products management | Partially prepared: bounded server pages, but remaining catalog-wide work and deep-page costs. Not certified for millions. |
| Warehouse | Not ready: full-result downloads and Python-side aggregation/filtering. |
| Purchase & Receiving | Not ready: full-register downloads, full-catalog selectors, and costly relationship queries. |

## Products management

- `api_products` in `D:\POS\POS_System_by_Thuta\app.py` applies server pagination
  when both `page` and `per_page` are supplied, with a 100-row cap. The Products
  management UI now requests 30 products per page, not the entire catalog.
- That path uses numbered OFFSET pagination and exact counts. Deep pages and
  `%query%` searches across name/barcode/category remain scaling risks.
- The legacy request without pagination still returns every matching product.
- `Category.to_dict` uses `len(self.products)` and `len(self.suppliers)`. Category
  dropdown requests therefore materialize related catalog rows just to count
  them. Product-row serialization also accesses lazy unit/category relationships.
- The POS grid is a different path (`view=pos`): it already uses bounded cursor
  pagination without total counts. This must not be confused with management.

## Warehouse

- `loadWarehouseInventory` in
  `D:\POS\POS_System_by_Thuta\templates\dashboard.html` fetches the full array
  from `/api/warehouse`, then uses `paginateArray` in the browser. Displaying ten
  rows does **not** mean only ten rows were queried or transferred.
- `warehouse_inventory_records` calls `.all()`, accesses lazy product/batch
  relationships, and recomputes a product's total batch quantity for each batch.
  A product with many batches can incur quadratic Python summation work.
- The low-stock filter runs after full retrieval and serialization.
- `api_warehouse_summary` loads all positive-stock inventory objects and computes
  counts, sums, and values in Python instead of SQL aggregates.

## Purchase & Receiving

- `loadPurchaseOrders` likewise downloads all matching orders and paginates only
  in the browser. `purchase_orders_for_filters` ends in `.all()`.
- `serialize_purchase_order` loads supplier/users/lines/products/branches through
  lazy relationships, including lines needed for register counts and totals.
  This can create many extra queries and large ORM object graphs.
- `/api/purchase_orders/products` downloads all products from all branches.
  `addPurchaseOrderItemRow` builds a complete product selector for each order
  line. A searchable local selector does not make that download bounded.
- The PO-line model has no declared index on `purchase_order_id`, and the startup
  performance-index list does not add one. Loading an order's lines can therefore
  scan the line table. Existing PO `(status, created_at)` and warehouse
  `(product_id, quantity)` indexes do not cover every register/FIFO sort.
- `receive_purchase_order` in
  `D:\POS\POS_System_by_Thuta\purchase_receiving.py` uses an atomic transaction
  with `BEGIN IMMEDIATE`, validates remaining quantities, and rolls back failures.
  Existing tests cover concurrent over-receiving. These are useful correctness
  protections, not throughput evidence: SQLite writers serialize, all lines are
  loaded while holding the writer lock, and receipt requests have no durable
  idempotency key for retrying a partial receipt after a lost response.

## Cross-cutting concerns and recommended order

1. Add capped server-side pagination to Warehouse and Purchase lists. Prefer
   deterministic cursor ordering with an ID tie-breaker for deep browsing.
2. Replace full-catalog/supplier selectors with capped remote search and exact-ID
   lookup. Return lightweight register payloads; load details on demand.
3. Push filtering/counting/summing into SQL, preserve existing money-rounding
   semantics, and remove per-row lazy queries/repeated batch collection scans.
4. Add the PO-line ownership index and query-aligned date/filter/FIFO indexes;
   verify actual deployed indexes and query plans before claiming readiness.
5. Add durable receipt idempotency, shorten write transactions, and measure
   concurrent receiving/transfers/sales on the configured SQLite deployment.
6. Bound or background exports: current product/warehouse/purchase exports
   materialize complete filtered datasets and build reports in request workers.
7. Benchmark a disposable staging database with at least one million products,
   orders/lines, and warehouse batches, including skewed batch histories. Measure
   cold loads, search, deep browsing, memory/payload/query counts, exports, index
   migrations, and concurrent writes. Define latency/memory budgets first.

The frontend's response cache improves repeat visits but does not fix unbounded
cold requests. No claim is made that adding rows alone requires changing the
database engine: deployment resources and measured write contention should guide
that decision.

## Validation scope

Regression coverage exercises 30-row management requests/rendering, next-page
replacement and scroll reset, accessible scroll-region markup/CSS, and a real
31-product API result split across two pages. Tests run through the disposable
checkout runner to avoid modifying local POS data. These small fixtures establish
behavior only; no million-row benchmark or live-production test was performed.