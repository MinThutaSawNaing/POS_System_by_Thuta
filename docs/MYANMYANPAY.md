# MyanMyanPay readiness and activation

## Scope

This integration replaces the **manual uploaded MMQR and cashier confirmation**
when `MMPAY_ENABLED=true`. It is disabled by default, so existing installations
continue working until merchant onboarding is complete. It uses the existing
`requests` and ReportLab libraries; no browser payment secret or external QR
rendering service is needed.

Provider references reviewed: the API reference and Python SDK linked from
the MyanMyanPay developer console (`docs.myanmyanpay.com/api/` and
`docs.myanmyanpay.com/sdk-python/`). The protocol uses a signed handshake,
signed create/get/cancel requests, and raw-body HMAC-SHA256 webhook verification.
The exact production base URL and keys must be obtained from your merchant
console. Do not assume the developer-console URL is an API host.

## Configure on the server

1. Back up the POS database and uploads; deploy this version. Startup creates
   `payment_intent` and `payment_webhook_event` tables without deleting sales.
2. Obtain merchant App ID, publishable key, secret key, approved API base URL,
   and any required outbound IP allowlisting from MyanMyanPay.
3. Configure the variables in the root `.env.example` in your untracked `.env`
   (or deployment secret store): `MMPAY_APP_ID`, `MMPAY_PUBLISHABLE_KEY`,
   `MMPAY_SECRET_KEY`, `MMPAY_API_BASE_URL`, `MMPAY_CALLBACK_URL`.
4. Set the payment branch currency to **MMK**. This implementation does not
   perform currency conversion.
5. Set `MMPAY_CALLBACK_URL=https://YOUR-POS-HOST/webhooks/myanmyanpay`.
   A real, reachable HTTPS endpoint is required: local-only POS machines need a
   secure public deployment/reverse proxy. Do not expose Flask's debug server.
   Route this path through the proxy without login redirects, HTML error pages,
   request-body rewriting, or a browser-only challenge. Limit requests to 64 KiB
   at the proxy too. TLS termination must use a valid trusted certificate.
6. Start with matching sandbox keys (`_test_` markers). The client selects the
   provider's `sandbox-handshake`, `sandbox-create`, `sandbox-get`, and
   `sandbox-cancel` endpoints. Do not mix test and live keys.
7. Set `MMPAY_ENABLED=true` and restart/recreate the application container.
   Compose explicitly passes these environment variables. Confirm checkout
   reports ready before asking a customer to pay.
8. Run the acceptance checklist below in sandbox, then repeat with the approved
   live credentials and a controlled small real transaction. Never reuse a
   sandbox database of payment intents as live transaction evidence.

Keys remain environment-only and are never returned by configuration endpoints.
Keep the same key pair while outstanding payment callbacks are being delivered.
Disabling new payments does not discard outstanding orders: the resume controls,
signed callbacks, and authenticated refresh/cancel/complete remain available with
the existing credentials. Do not remove credentials until orders are reconciled.
Coordinate key rotation with the provider; multiple simultaneous signing keys
are not supported. Do not change sandbox/live configuration with outstanding
orders. Preserve the database across restarts and run one writable POS database,
not independently replicated SQLite files.

## Checkout and trust boundary

* The server validates the cart, stock, tax, promotions, split breakdown and MMK
  amount. It persists the original checkout and a unique provider order before
  creating the QR. A split payment requests only its mobile-payment leg.
* Pending payment does not create a sale or reduce stock. Stock is **not reserved**.
  Staff should finish checkouts promptly; inventory/prices/promotions can change
  before the customer pays. Successful payments that cannot finalize remain
  visible and require reconciliation. Do not collect a second payment.
* QR images are generated locally from the provider's EMV string. Access and
  status routes are restricted to the creating user and branch.
* Webhooks authenticate the original request bytes with `X-Mmpay-Nonce` and
  `X-Mmpay-Signature`, then match order, exact amount, currency and provider
  references. Accepted event hashes are persisted for deduplication. Repeated
  or out-of-order callbacks do not create sales or reverse a confirmed payment.
  No undocumented timestamp expiry is imposed on nonce values; durable event
  deduplication and monotonic payment transitions handle replay/retries.
* The browser polls local stored state. Explicit refresh retrieves provider state
  over authenticated server-to-server requests, throttled to five seconds per
  order. This recovers missed callbacks without trusting a screenshot/browser flag.
* Completing a verified payment uses **only the saved checkout**, rechecks stock
  and prices, and atomically consumes that payment with the sale transaction.
  Retries return the existing sale. A payment cannot buy a different/new cart.
* Network ambiguity retains the original provider order. Resume/check it; never
  retry create with another reference just because a request timed out.
* Cancellation is a provider request followed by status lookup, not a local
  dismissal. Dismissing the modal does not cancel payment. Paid orders cannot
  be cancelled through this flow. A late success after expiry/cancellation is
  retained for recovery rather than silently discarded.
* Refund notifications change the payment state and block uncompleted sales;
  they do **not** automatically reverse inventory or create accounting refunds.
  Use the existing authorized returns/reconciliation process. Provider refund
  initiation, payout/disbursement reconciliation, recurring payments, and an
  unattended background finalization worker are outside this change.

The public webhook is `/webhooks/myanmyanpay`; all checkout routes are under
`/api/payments/myanmyanpay`. Payment POSTs require the custom same-origin header
`X-POS-Payment-Request: 1`. Do not enable permissive cross-origin credentials.
Successful callbacks return 200; invalid signatures return 401; invalid payment
matches return 409; unknown orders return 404; oversized events return 413.
Transient database/server failures return non-2xx for provider retry. Confirm
the provider's actual retry policy during onboarding; it is not assumed here.

## Acceptance checklist (required before live use)

- Create sandbox QR; scan/pay; confirm webhook reaches public HTTPS callback.
- Verify app automatically shows verified status; completing creates exactly one
  sale/receipt and decrements stock once. Repeat completion and resend callback.
- Reload/sign back in to the **same user and branch** and resume the order.
- Test cash/mobile split; verify only the mobile leg is charged.
- Test expiry, failure, cancellation, second scan/heartbeat and late callback.
- Block callback temporarily; use explicit server refresh to recover success.
- Interrupt create response; resume the same order without charging twice.
- Alter callback body/signature/amount/reference; ensure payment stays unpaid.
- Change stock after QR creation; verify paid order remains for reconciliation.
- Test branch/account switch while requests are in flight and another branch's
  QR/status cannot be fetched. Verify shared-device sign-out behavior.
- Test provider refund notifications; reconcile through authorized returns.
- Check outbound IP allowlist, TLS, reverse proxy limits and restart persistence.

Automated tests use mocked provider HTTP calls, not real merchant transactions.
Run `python run_tests.py` for an isolated database and no deployment credentials.
Passing local tests is **not** merchant certification or proof of live settlement.