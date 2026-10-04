# Cashier permission policy

## Operational access

| Area | Cashier access |
| --- | --- |
| POS | Current-branch checkout with cash, card, mobile or split payment; delivery details may be recorded with the sale |
| Products/categories | Read-only current-branch catalog; no product costs, reorder configuration or supplier counts |
| Customers | Checkout lookup of current-branch ID, name and phone only; no customer management, balances or credit accounts |
| Sales/receipts | Own sales in the current branch, including receipt printing and idempotent replay |
| Returns/exchanges | Own current-branch original sales only; exchange products must belong to that branch and use catalog/valid promotion prices |
| Sales reports | Own sales only; company-wide and other-branch requests cannot widen this scope |
| Returns export | Own-sale scoped register only |
| Deliveries | Read/print/report deliveries attached to own current-branch sales; no standalone creation, editing or stage changes |
| AI | Safe current-branch inventory lookup only; costs and supplier information are removed before model/history exposure |
| AI memories | Existing private-memory ownership rules still apply; branch-shared writes remain privileged |

Cashiers cannot access company dashboard aggregates, promotion administration,
customer/debt administration, suppliers, purchasing, warehouse administration,
users, audit logs, settings administration, label printing or branch switching.
Currency, receipt and payment settings remain readable for checkout operation.

## Enforcement

- `CASHIER_API_METHODS` in `app.py` is an explicit endpoint-and-method allowlist.
  New APIs are denied to cashiers unless deliberately added and tested.
- Frontend capability checks hide unauthorized navigation/actions and block stale
  handlers. They are usability controls, not the authorization boundary.
- Receipt, return, delivery and replay requests enforce sale ownership.
- Missing/inactive cashier branch scope fails closed. Foreign product/customer
  IDs cannot be used to mutate stock or attach a sale to another branch.
- AI schemas, tool execution and direct tool calls enforce role policy. Sales,
  return and delivery AI tools remain unavailable to cashiers until they can
  safely implement the same own-sale rules.
- Dashboard API requests carry their rendered account identity. If another tab
  changes the browser's sign-in, the server refuses mismatched requests with
  `409 stale_session`; the old screen blocks requests and offline checkout.
  These headers are a consistency check, not an authentication credential.

## Shared-device/offline behavior

- Browser snapshots are scoped by account and role; product snapshots and sale
  queues are also branch-scoped where needed.
- Legacy unscoped sale queues are preserved but never automatically assigned to
  the current user. A warning requests manager reconciliation. **Do not clear
  browser data until pending sales have been reconciled.**
- The service worker caches explicitly allowed public assets only. API,
  receipt/report and authenticated HTML responses are not persistently cached.
  Updating the worker deletes its old caches.
- An already-open POS screen may use its scoped saved data offline. Reloading
  the authenticated app requires connectivity: this intentionally prevents
  restoring a previous manager's cached HTML on a cashier session.
- Account-scoped localStorage is not encrypted storage or OS-user isolation.
  Shared terminals require trusted device users, protected browser profiles and
  a lock/sign-out procedure. Old sensitive tabs should be closed at handover.

## Deployment limitations and release checks

The existing User model has **no per-user branch membership**. Login chooses the
default/first active branch. Cashiers cannot switch branches afterward, but
administrators must not interpret this as a configured user-to-branch assignment
system. Multi-branch cashier assignment requires a separate schema/policy change.

Manager and boss permissions are otherwise retained, including existing routes
that deliberately accept managers only. This audit does not certify every
manager workflow, infrastructure control, CSRF defense or financial concurrency
rule in the application.

Before release, test with real cashier and manager accounts on a staging copy:
checkout/receipt, mobile and split payments, delivery checkout, own-sale returns,
denied admin URLs, offline queue recovery, account handover in two tabs and the
service-worker upgrade. Back up the database and reconcile legacy offline queues.
Use HTTPS and perform a visual/mobile smoke test; automated template/Node tests
do not replace that browser validation.