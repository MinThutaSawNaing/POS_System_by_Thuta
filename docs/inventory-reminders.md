# Per-item MOQ low-stock reminders

Each branch product has a **MOQ** — the minimum stock level the shop wants to
keep. When stock is at or below that number the item is *running low* and the
POS reminds managers two ways:

1. **Dashboard panel** — "Low stock at or below MOQ" lists the product, current
   stock, MOQ and suggested order quantity. The panel is live, dismissible for
   the current stock snapshot, and feeds the dashboard's low-stock count.
2. **Daily email** — the background poller emails the branch's managers plus
   every boss account that has a linked email address.

## Setting the MOQ

Edit a product in **Products -> Add/Edit Product**:

| Field | Meaning |
| --- | --- |
| MOQ (low-stock level) | Alert when stock is at or below this number. Stored in the product's existing `reorder_point` column, so AI restock suggestions and low-stock reports stay consistent. |
| Order quantity | Suggested quantity to buy when restocking. Stored in `reorder_quantity`. |
| Send low-stock reminders for this item | Per-item opt-out. Stored in `reorder_enabled`. |

For bulk work, open **Settings -> Inventory -> MOQ rules**:

- **One product** — search, inspect stock/MOQ/recent sales/suggestion, then apply.
- **Selected products** — tick any number of rows (selection survives paging), then apply.
- **All products** — one rule for the active branch, optionally limited to products
  whose MOQ is `0`/unset. Other branches are never touched.
- Optional overrides: order quantity and reminders On/Off. Leave them unchanged
  to keep each item's current value.
- Every apply is confirmed, audited, and reflected immediately in the Dashboard
  panel, Products table and reminder emails.

### Smart suggestions

**Use smart suggestions** sets each item from its own recent sales:

```
suggested MOQ = ceil(units sold in the sales window ÷ window days × days of cover)
```

- Sales window: 30 days by default (7/30/60/90 selectable).
- Days of cover: 7 by default (1–90).
- Result is at least `1`; items with **no sales in the window are left unchanged**
  rather than being set to `0`.

Example: 30 units sold in 30 days with 7 days of cover → `ceil(30 ÷ 30 × 7) = 7`.
Changing the cover to 14 gives `14`; using a 60-day window that includes 130
units gives `ceil(130 ÷ 60 × 7) = 16`.

Existing products keep their stored reorder point (default `10`), so reminders work
immediately after upgrading. Set MOQ to `0` to alert only when the item is out of
stock.

## Email behaviour

- The poller runs every `INVENTORY_REMINDER_INTERVAL` seconds (default `300`).
- Each product is emailed **at most once per store-local calendar day**
  (`Asia/Yangon`, matching report schedules); the `inventory_reminder_log` table
  records `(product_id, branch_id, date)`, so a restart never repeats a reminder.
- One email per branch per tick lists only the items not yet reminded that day.
- Recipients are branch `manager` accounts plus every `boss` account with an
  email. Cashiers never receive reminders.
- Email uses the existing Resend SMTP settings (`Settings -> Email` or the
  `RESEND_*` environment variables). If email is not configured the panel still
  works and nothing is marked as sent, so reminders begin once SMTP is saved.

## Settings and endpoint

Open **Settings -> Email -> Low-stock reminder emails** and choose **On** or
**Off**, then press **Save reminder setting**. The choice is stored in the
`inventory_reminder_email` app setting and applies to every branch.

- **On** (default): managers and the boss receive the daily email.
- **Off**: no reminder email is sent; the dashboard panel and low-stock count
  keep working.

The same card has **Send a test reminder**: enter an address and the app emails
the *real* reminder template for the active branch's current low-stock items to
that one address. It is a verification tool only — it does not change the daily
recipients and does not consume that day's scheduled reminder.

### MOQ management endpoints

- `GET /api/settings/moq/products` — paged MOQ editor rows (`page`, `per_page`,
  `q`, `filter=all|low|unset|disabled`, `product_id`, `cover_days`,
  `lookback_days`). Each row includes `stock`, `moq`, `sold` and
  `suggested_moq`; `summary` carries branch-wide `total_products`, `low_count`
  and `unset_count`.
- `POST /api/settings/moq` — apply a manual MOQ or smart suggestions. Body:
  `scope: "ids"|"all"`, `product_ids` (1–500, for `ids`), `mode:
  "manual"|"suggestions"`, `moq` (manual), optional `reorder_quantity`,
  optional `reminders_enabled`, optional `only_unset` (for `all`),
  `cover_days`, `lookback_days`, `branch_id`.
  Returns `updated`, `skipped` and a human-readable `message`. Updates are
  branch-scoped, capped, validated and written to the audit log.

| Name | Default | Purpose |
| --- | --- | --- |
| Settings -> Email -> Low-stock reminder emails | On | Store-wide switch for the daily email. |
| `INVENTORY_REMINDER_ENABLED` | `1` | Deployment-level kill switch; `0` disables email even if the setting is On. |
| `INVENTORY_REMINDER_INTERVAL` | `300` | Poller interval in seconds (minimum 30). |

`GET /api/inventory/reminders` (manager or boss) returns:

```json
{
  "summary": {"total_products": 4, "low_stock_count": 2, "out_of_stock_count": 1},
  "items": [
    {"product_id": 1, "name": "Cola", "sku": "COLA-1", "barcode": "C1",
     "category": "Drinks", "current_stock": 3, "moq": 3,
     "reorder_point": 3, "reorder_quantity": 12, "suggested_qty": 12}
  ],
  "email": {"enabled": true, "ready": true}
}
```

The older `GET /api/inventory/alerts` and suggested-purchase-order endpoint read
the same MOQ rows, so their output stays identical.

## Boundaries

- MOQ belongs to a **catalog product** (the sellable item). Warehouse rows are
  stock batches of that product and keep the warehouse tab's own low-stock
  filter.
- Branch Inventory's "Low stock (≤5 units)" filter is a fixed catalog threshold
  and is intentionally unchanged; the dashboard panel is the MOQ view.
- Reminders are not acknowledged or snoozed server-side. Dismissing the panel
  hides it for the current stock snapshot only; any stock change brings it back.
