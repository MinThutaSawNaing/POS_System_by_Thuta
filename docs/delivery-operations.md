# Delivery operations

The Delivery tab supports dispatch, delivery accountability, and cash-on-delivery
(COD) handover as separate workflows. Recording delivery does **not** record a
payment, and recording a collection does **not** change the original sale total.

## Dispatch workbench

- Each row has one **Manage delivery** control (**View delivery** for read-only
  users). It opens a workspace with a **What would you like to do?** selector;
  only the selected task's form is shown. Switching tasks keeps unsaved fields
  within that workspace; closing or reloading it clears those drafts.
- The workspace suggests the next task: package, start delivery, record an
  attempt, retry a failed attempt, collect COD, or remit collected COD. Completed
  workflows open in review mode. Selecting a task never writes a change by
  itself; use its explicitly labelled button to save or confirm the action.
- Editing, printing, manual carrier updates, photo/signature capture and eligible
  deletion remain in the same selector. Expand **Carrier & tracking**, **Saved
  delivery evidence**, or **Delivery history** to review supporting records.
- Search and filter deliveries by stage, priority, payment state, courier, and
  operational attention. Results are paginated on the server.
- An open delivery is overdue after its scheduled time, or after 48 hours when
  no schedule has been set. Delivered and cancelled deliveries are not overdue.
- Unassigned deliveries have no courier name. Assign an individual delivery in
  its edit dialog, or select active deliveries and assign a courier together.
- Schedule times entered in the browser use the operator's local timezone and
  are sent as ISO timestamps. Check the device timezone before scheduling.

## Delivery attempts and retries

Use the operations dialog to record an unsuccessful attempt with a reason or a
successful attempt with a proof note. An unsuccessful attempt is not a
cancellation and does not settle COD. Retry returns an unsuccessful delivery to
the dispatch queue. The timeline records the operator and time of each action.

Proof notes are operational evidence, not verified signatures or photographs.
Existing stage-update integrations remain supported; legacy stage changes must
not be mistaken for independently verified proof of delivery.

## COD collection and remittance

1. Complete the delivery.
2. Record the exact expected COD collection and its reference.
3. Record the exact handover/remittance amount and its reference when the
   courier hands the money to the business.

Already-paid deliveries do not require COD collection. Collection and remittance
are separate records, so a delivered order can still have money outstanding.
These records are operational reconciliation only: they do not post a new sale,
repay a customer debt, integrate with a bank, or move money automatically.
Partial collections and partial remittances are intentionally not supported.

Keep the collection reference tied to the courier's receipt or handover document.
If a recorded financial action is incorrect, preserve its evidence and escalate
to a manager; do not edit the sale to disguise a reconciliation discrepancy.

## Access and rollout

- Managers and owners operate the workflows in the active branch.
- Cashiers retain read-only delivery access limited to their own sales in their
  active branch. They cannot assign couriers, record attempts, or reconcile COD.
- Back up the database using the project's backup procedure before deploying.
  Stop old application workers before starting the upgraded application.
- Existing deliveries remain intact. Historical collections are not inferred
  from delivery stages or automatically backfilled as paid.
- Restart the application after deploying the new code so its new tables are
  created by the normal schema initialization.
- Restoring an older backup through Settings creates missing delivery-event,
  carrier/profile, and proof tables without inferring previous collections,
  remittances, or evidence.

## Manual carriers and tracking

In delivery operations, enter the carrier name, tracking number, HTTPS tracking
link, and a manually reported carrier status or note. Tracking links open the
carrier website; the POS does not fetch them. Carrier status is operator-entered,
not independently verified by a carrier. A carrier update does not automatically
complete a delivery or settle COD.

Live carrier booking, label purchase, carrier webhooks, automatic tracking
updates, GPS tracking, route optimization, customer notifications, and accounting
transfers remain deferred until a provider and its account configuration are
selected.

## Photos and signatures

Managers and owners can capture a delivery photo using a device camera or upload
a JPEG/PNG image. Signatures are drawn in the delivery dialog and saved with the
signer's name. These are operational proof records, not cryptographically verified
digital signatures or independent identity verification.

Proof images are stored in the database rather than the public uploads directory.
They are available only through authenticated, branch-scoped delivery access.
The server verifies and re-encodes image contents; an image extension alone is
not sufficient. Backups now include these images and may be larger. Collect only
necessary evidence with the recipient's knowledge, and follow your business's
privacy and retention procedures.

## Deleting delivery records

Managers and owners can delete a completed or cancelled delivery using an
explicit confirmation and reason. Cancel active deliveries first. Deletion is
soft deletion: the record disappears from active delivery operations, while its
linked sale, audit timeline, proof, and financial evidence remain stored.
Delivered COD must be collected and fully remitted before deletion; cancelled
deliveries may be deleted without collecting money. Deletion never refunds a
sale, reverses stock, changes customer debt, or cancels a carrier booking.

## AI assistant workflows

The assistant's delivery tools use the same business rules as the dashboard.
Ask it to inspect delivery details and proof metadata, update manual carrier
tracking, record an actual delivery attempt, schedule a retry, or record COD
collection/remittance with the exact amount and reference. Deletion requires an
explicit confirmation and reason.

Photos and signatures must come from actual capture/upload in the delivery
dialog. The assistant cannot invent proof, sign for a recipient, automatically
verify a carrier status, book a shipment, or claim that recording a remittance
transferred money.

## Verification

Run tests through the disposable-checkout runner, never directly against the
local business database:

```powershell
python run_tests.py -q --tb=short test_delivery_operations.py test_delivery_extensions.py test_delivery_operations_javascript.py test_delivery_extensions_javascript.py test_delivery_actions_javascript.py test_delivery_actions_browser.py test_ai_delivery_tools.py test_delivery_restore.py test_delivery_fee_charge.py test_delivery_slip.py test_delivery_report.py
```

The browser checks use a locally installed Chrome, Chromium, or Edge (otherwise
they skip). They exercise the actual dialog, Bootstrap and delivery JavaScript
at desktop and mobile widths using fixture responses, without accessing POS data.