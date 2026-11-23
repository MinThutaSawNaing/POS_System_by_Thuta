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

Existing products keep the stored reorder point (default `10`), so reminders work
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
