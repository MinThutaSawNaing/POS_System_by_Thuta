# Clothing catalog, migration and imports

## Identity and inventory plan

Clothing is an additive metadata layer, not a replacement product catalog.

- **ClothingStyle** describes a shared style: `id`, `name`, optional `code`,
  `brand`, `season`, `description`, `photo_filename`, and `archived`.
- **ClothingVariant** gives one logical catalog item a style, size and color.
  `catalog_id` is globally unique in this table and links logically to the
  existing `Product.catalog_id`. It is deliberately not a foreign key to Product:
  the same catalog identity can have one Product in each branch.
- **Product** remains the authority for exact sale/PO/transfer product IDs,
  barcode, price, cost and branch stock. No clothing columns are added to Product.
- A unique `(style_id, size_norm, color_norm)` constraint prevents duplicate
  variants. Normalization is Unicode NFKC, whitespace collapse and case folding.
  Archived variants retain their identity and uniqueness reservation.
- Existing Products are never inferred to be variants by similar names or
  barcodes. An operator must explicitly attach an existing current-branch
  Product. Attachment preserves its ID, catalog ID, name, price, barcode and
  stock, and also makes existing same-catalog branch Products resolve to the
  same clothing metadata.
- A barcode cannot identify different catalogs, even if the Products belong to
  different branches. Creation/import checks all Product barcodes, including
  non-clothing Products. Attachment additionally rejects barcode drift between
  existing same-catalog Products. These operations report conflicts rather than
  silently merging, renaming or repairing existing catalog identities.
- New clothing Products always start at **zero stock**. Stock is received
  through the existing purchase-order receiving workflow and transferred through
  the existing branch transfer workflow. Import is not a stock adjustment API.

Styles are shared metadata. Variant inventory endpoints return only exact
Products in the active branch, never an inferred substitute from another branch.
Style/variant archive is a reversible metadata flag, not product deletion and
not a financial/history rewrite. Variant style/size/color/catalog assignment is
immutable; archive and make a new explicit variant instead of reassigning one.
An attached Product without a barcode remains saleable through search and can
still have its non-identity fields edited, but it cannot be given a barcode later
because that would change the variant's physical identity. Archive it and create
a new barcoded variant if scanning is required.
Existing sale/display snapshots and the host's sale/archive guards remain the
responsibility of the host application integration.

## Registration and additive migration

`clothing.py` has no application import, environment/credential access, or
database work at module-import time. The host registers it after its Product and
auth helpers exist, before its normal startup `db.create_all()`:

```python
from clothing import register_clothing, migrate_clothing

clothing_models = register_clothing(app, db, {
    'Product': Product,
    'get_current_branch_id': get_current_branch_id,
    'login_required': login_required,
    'manager_or_boss_required': manager_or_boss_required,
})
globals().update(clothing_models)

with app.app_context():
    migrate_clothing(db, clothing_models)
```

The return value is a **dictionary**, with keys `ClothingStyle`,
`ClothingVariant`, and `ClothingImportCommit`. `migrate_clothing` uses SQLAlchemy's
`create_all(..., checkfirst=True)` limited to these three new tables. It can run
repeatedly, including on an existing populated POS database, without changing
Product columns, product IDs, catalog IDs or inventory. The ordinary startup
`db.create_all()` also creates registered new tables; do not register the models
twice on the same SQLAlchemy application. There is no destructive down migration.
An empty clothing catalog after upgrade is intentional: legacy Products remain
ordinary Products until an operator explicitly groups them.

The host owns audit registration for ClothingStyle and ClothingVariant, the
read-endpoint cashier allowlist, existing Product-edit/transfer identity guards,
sale snapshots, and POS/report serialization. Read endpoint names are
`clothing_styles`, `clothing_style`, `clothing_variants`, `clothing_variant`.
All writes require the existing manager/boss decorator; preview is also
manager/boss-only. Costs are omitted from cashier clothing responses.

Clothing metadata can reference an existing uploaded style photo using
`photo_filename`; it must be a basename of an existing image in the configured
upload directory. It cannot set arbitrary paths or upload files. Host image
cleanup must consider style references before deleting shared images.

## HTTP contract

All endpoints require a signed-in user. Requests with a `branch_id` that differs
from the trusted active branch are rejected; a caller cannot select another
branch by changing request data. JSON object request bodies are required for
writes except bodyless DELETE. Responses use `success` and validation failures
include `message`; row errors include `row` (1-based), `field`, and `message`.

| Endpoint | Behavior |
| --- | --- |
| `GET /api/clothing/styles` | `{success, styles}`; active styles by default; `include_archived=true` includes archived styles |
| `POST /api/clothing/styles` | Create explicit style; required `name`; optional metadata fields above; returns `{success, style}`, HTTP 201 |
| `GET /api/clothing/styles/<id>` | `{success, style}` |
| `PUT /api/clothing/styles/<id>` | Update style metadata and/or boolean `archived`; code unique after normalization |
| `DELETE /api/clothing/styles/<id>` | Archive style, retaining metadata and Products |
| `GET /api/clothing/styles/<id>/variants` | `{success, variants}` for active-branch Products; active styles/variants by default; `include_archived=true` includes archived |
| `POST /api/clothing/styles/<id>/variants` | Create one new zero-stock Product and variant; required `size`, `color`, `barcode`, `price`; optional `name` (defaults style name), `cost` (defaults zero) |
| `POST /api/clothing/styles/<id>/attach` | Explicit existing Product attachment; required `product_id`, `size`, `color`; cannot overwrite Product fields |
| `GET /api/clothing/variants/<variant_id>` | `{success, variant}` for exact active-branch Product, or 404 |
| `PUT /api/clothing/variants/<variant_id>` | Only `{archived: true/false}`; identity edits rejected |
| `DELETE /api/clothing/variants/<variant_id>` | Archive variant without touching stock/history |
| `POST /api/clothing/import/preview` | Validate explicit rows/CSV without writes; returns review token only for valid batch |
| `POST /api/clothing/import/commit` | Validate reviewed rows again and atomically apply entire batch with durable idempotency |

A variant response's `id` **is the Product ID**, not the metadata variant ID.
It includes `product_id`, `variant_id`, `catalog_id`, `style_id`, `style_name`,
`size`, `color`, `archived` (effective style-or-variant archive),
`variant_archived`, `name`, `display_name`, `barcode`, `price`, `stock`,
`branch_id`, `tax_rate`, `unit_id`, and `photo_filename`; managers/bosses also
receive `cost`. Use `product_id`/`id` for cart, PO and stock operations, and
`variant_id` only for metadata archive endpoints. `display_name` appends size and
color without rewriting Product.name or existing sale history.

## Preview-first CSV import

Create styles explicitly first (or use their existing IDs). This release imports
variants/explicit attachments into those styles; it does **not** auto-create or
auto-group styles from names, guessed codes or spreadsheet adjacency.

New-variant CSV example:

```csv
style_id,size,color,barcode,name,price,cost
12,S,Navy,SHIRT-S-NAVY,Cotton Shirt,25.00,9.50
12,M,Navy,SHIRT-M-NAVY,Cotton Shirt,25.00,9.50
```

Explicit legacy attachment CSV example:

```csv
style_id,size,color,product_id
12,L,White,401
12,XL,White,402
```

Mixed new/attachment rows are allowed. Empty `product_id` means create new.
Attachment fields other than identity can be omitted; any supplied barcode,
name, price or cost must match the existing Product, never overwrite it.
Headers must be unique and supported; unknown headers/fields fail validation.
The importer accepts at most 1,000 rows and at most 2 MB of CSV text. A UTF-8 BOM
is accepted. `size`, `color`, and new-product `barcode` are required. Price/cost
must be finite, nonnegative and have at most two decimal places. `stock` may be
omitted or explicitly zero only. Missing styles, archived styles, duplicate
normalized variants, duplicate barcodes, catalog attachment conflicts and
cross-branch barcode conflicts invalidate the batch. Every row is checked, and
all failing rows are reported (one primary error per failing row).

1. `POST /api/clothing/import/preview` with `{ "csv": "..." }` or
   `{ "rows": [...] }`, never both.
2. Review `valid`, `errors`, and canonical `rows`. Preview does not reserve stock,
   create styles/Products/variants, or write a preview database record. Only a
   wholly valid batch returns `preview_token`.
3. Commit the **returned canonical rows**, not the original CSV:

   ```json
   {
     "rows": [{"style_id":12,"size":"S","color":"Navy","barcode":"SHIRT-S-NAVY","name":"Cotton Shirt","price":"25.00","cost":"9.50"}],
     "preview_token": "token-returned-by-preview",
     "idempotency_key": "operator-generated-unique-request-key"
   }
   ```

4. A signed token binds the exact reviewed rows to the active branch and user.
   Changed rows, branch, user, or invalid signature require a fresh preview.
   New imports must commit within 30 minutes of preview. Commit acquires a SQLite
   writer lock **before** reading identity state and repeats full validation;
   a conflicting change since preview rejects the whole batch.
5. On success, HTTP 201 returns `{success, imported, variants, replayed:false}`.
   Products, metadata and the durable `ClothingImportCommit` receipt commit in
   one transaction. No partial batch or stock ledger entries are written.
6. Retry an uncertain response using the same canonical rows, token and key.
   A successful prior request returns the saved response with `replayed:true`
   (HTTP 200), even after the review window has elapsed. The receipt is scoped
   by trusted user and branch. Reusing a successful key for different reviewed
   rows gives HTTP 409. Failed validation does not consume the key. Catalog busy
   errors give HTTP 409 and can be retried with the same key.

Use existing PO receiving after import to record real incoming stock. Do not
import quantities as opening stock or bypass existing receiving authorization.

## Isolated verification

Run only the disposable runner, never import the development app against local
instance data or use deployment credentials:

```text
python run_tests.py -q test_clothing_catalog.py --tb=short
```

The catalog tests create their own Flask/SQLAlchemy fixture and temporary SQLite
database. They cover identity preservation, normalized uniqueness, archive,
branch isolation, cashier redaction, attachment, barcode conflicts across
branches, preview-first confirmation, invalid values, atomic revalidation,
durable replay, concurrent retry and repeatable new-table-only migration.

Other focused suites (all through the same disposable runner):

```text
python run_tests.py -q test_clothing_checkout.py test_clothing_returns.py test_clothing_receiving.py test_clothing_javascript.py test_clothing_tools_reports.py test_clothing_analytics.py test_clothing_receipt.py
```

## Store operation and demonstration

1. Open **Settings → Clothing** (managers/bosses) and create a style.
   Styles are explicit; a similar Product name never groups items automatically.
2. Create variants (size, color, unique barcode, price), or attach existing
   current-branch Products with an explicit CSV `product_id`. New Products start
   at zero stock; attachment keeps the Product's identity and stock.
3. Order the variants on a normal purchase order, approve it, receive it in the
   purchases tab, then transfer warehouse stock to the branch through the
   existing transfer workflow. The clothing stock grid is read-only and links to
   receiving; it never overwrites balances.
4. On the POS tab, choose a style and filter by size/color, then add the exact
   Product. The admin style/variant/CSV controls live under
   **Settings → Clothing**; ordinary barcode search and scanning still use
   Product identities.
5. Complete a sale with the existing payment, receipt and offline flows. New
   lines store an immutable description snapshot; editing or archiving the style
   later does not rewrite that receipt or its tax amounts.
6. Return or exchange through the existing Returns tab. Reason and
   `sellable`/`damaged` are optional; damaged goods are not restocked. Retry a
   failed request with the same idempotency key.
7. Managers and bosses can review `GET /api/reports/clothing`; stock is current
   stock, and line turnover is based on sale snapshots less original-sale
   returns. Legacy unsnapshotted lines stay unclassified. Settlement totals from
   adjustment sales are reported separately from merchandise turnover.

## Verification status and honest limits

Verified locally with `python run_tests.py` (disposable checkout): clothing
catalog/imports, synthetic six-variant receive/transfer/checkout journey,
promotions and immutable receipt snapshots, branch stock isolation, archived
variant rejection, ordinary legacy returns, offline duplicate handling, mocked
MMQR sandbox flow with checkout-hash protection, return/exchange concurrency and
retry, receiving retries, report mathematics, AI tool contracts and generated
reference, and JavaScript behavior under Node.

Not verified locally: a real browser session (layout, pointer interaction,
printing dialogue), a physical thermal/label printer, a real payment provider
sandbox or device, and an upgrade of an actual historical production database.
The migration test uses a model-shaped synthetic legacy schema, not a full
historical checkout. It should not be represented as proof that every old
database shape is covered. Rehearse the upgrade against a copy of the real
database before deployment, and keep the verified backup from
`MIGRATION_NOTES.md`.