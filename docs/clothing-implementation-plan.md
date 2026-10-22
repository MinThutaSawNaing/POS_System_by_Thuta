# Clothing extension implementation plan

Date: 2026-10-22. This is an additive extension, not a replacement inventory system.

## Existing seams and affected workflows

- Flask/SQLAlchemy application; `Product` stores branch-local sellable stock,
  barcode, prices, tax, units and reorder rules. `catalog_id` links branch copies.
- Shared warehouse receives purchase-order items, then transfers quantities into
  branch Products. Preserve this flow and its movement/audit records.
- Checkout validates Product IDs, stock and allowed promotional prices. Durable
  payment intents save/hash the checkout. Offline sales use transaction IDs.
- Sale receipt snapshots preserve original display and financial data. Legacy
  lines must not acquire new clothing meaning merely because a product is grouped.
- Existing returns create adjustment sales and return/exchange records; extend
  those records with reasons/disposition and lock validation before changing stock.
- Product search, labels, reports, promotions and Loli use Product identities.
  Clothing must provide structured metadata without changing those identities.

## Principal risks and decisions

1. Keep Products as sellable items; add optional shared style/variant metadata
   keyed by catalog identity. Sizes/colors are never measurement units.
2. Explicit grouping only. Never infer styles from similar names. Reject duplicate
   normalized combinations and conflicting physical barcodes.
3. Preserve old payloads and receipts. New sale descriptions are snapshots;
   historical financial amounts and existing snapshots remain untouched.
4. Never replace stock from a grid/import. Receive through PO and transfer workflows;
   retries require durable request identity and payload matching.
5. Serialize return validation and stock movement. Damaged returns do not increase
   sellable inventory. Preserve existing mobile-refund restrictions.
6. Keep branch defaults and existing manager/boss boundaries; no new unguarded
   cross-branch lookup. Loli answers must come from structured database queries.
7. Use only disposable checkouts/databases with synthetic fixtures. Do not open
   local credential files or connect to production/demo services.

## Reviewable stages

1. Establish existing full-test baseline; add catalog models/APIs, repeatable
   migration and explicit variant grouping with validation tests.
2. Integrate POS selection/scanning, immutable line descriptions, labels,
   promotions, reports and structured read-only Loli inventory queries.
3. Extend existing returns with reasons/dispositions, retry protection and atomic
   validation; add preview-first spreadsheet CSV import and safe stock grid.
4. Run targeted and complete regression suites, review changes, document recovery,
   demonstration steps and any checks or workflows not verified locally.

## Recorded results

Baseline on the untouched tree (`python run_tests.py`, before implementation):
**1,046 passed, 161 skipped, 1 failed in 700.07s**. The only failure was
`test_cashier_permissions.py::test_isolated_backend_permissions`, whose nested
subprocess exceeded its fixed 240-second timeout. Its child tests are therefore
skipped by the launcher in this environment.

Final feature tree: **1,138 passed, 161 skipped, 1 failed in 663.71s**. The
permission-launcher timeout was the only failure. A post-review fix suite
(display-name consistency, barcode-less attachment editing, AI price updates with
omitted stock, analytics lock portability) passed **56 tests**.

Release check: the isolated permission child was then run directly against a
disposable source copy. It passed **155 tests (1 skipped) in 360.73s**, proving
the authorization behavior itself was intact and the parent's fixed 240-second
cap was the problem. That cap was raised to 600 seconds (still bounded so a real
hang fails); the targeted launcher then passed **1 test (155 skipped) in
358.08s**. No application code changed after the 1,138-pass suite, so the clean
permission result closes the only remaining red check without altering behavior.

Focused evidence collected during implementation:

- catalog/import: 133 passed in a combined clothing/returns/receiving/receipt/
  barcode/mobile-payment run;
- returns: 81 passed across clothing and existing return/exchange, money and
  split-payment suites;
- reports/tools/contracts after the PO branch payload fix: 45 passed;
- receiving retries plus analytics: 5 passed;
- synthetic checkout journey: 9 passed;
- UI JavaScript: 254 passed across new and existing Node-only suites;
- the isolated permission child suite could not be completed locally because of
  the launcher timeout above. Clothing authorization and cashier redaction are
  covered by the catalog and analytics suites instead.

Not verified locally: real browser interaction and printing, physical printers,
real payment-provider sandbox/device, and upgrade of an actual historical
production database. See `docs/clothing-store.md`.

## Baseline

The repository provides `python run_tests.py`, which copies source while excluding
credentials, instance databases and uploads before importing the application.
Baseline started before implementation. Results and limitations will be recorded
in the delivery documentation. A foreground attempt exceeded the command tool's
30-second timeout; the complete run was restarted with captured output.