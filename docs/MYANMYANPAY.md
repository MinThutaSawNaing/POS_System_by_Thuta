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

## Configure

### Option A - Settings screen (no server access needed)

A manager can open **Settings -> Payments** in the POS and enter the App ID,
publishable key, secret key, API base URL, callback URL and the enable switch.

* The one merchant configuration applies to **all branches**; it is not
  branch-scoped, because a single merchant account issues every branch's QRs.
* The two keys are encrypted at rest with the same `SECRET_KEY`-derived cipher
  as the AI API key. `GET /api/settings/myanmyanpay` returns only booleans, the
  source of each value, and the non-secret App ID/URLs - never a credential.
* Saving validates the values with the provider adapter itself (HTTPS base URL,
  matching `_test_`/`_live_` keys, callback path `/webhooks/myanmyanpay`) and
  then applies them to the running server for every branch immediately.
* Stored values override the deployment environment. **Clear saved credentials**
  deletes them and falls back to `.env`, so container installs keep working.
* Only `manager` and `boss` may read or change these values; a cashier receives
  `403` from the deny-by-default API allowlist. Changes are written to the audit
  trail with field names only (never values).
* Entering credentials in the browser means TLS is required in production, and
  `SECRET_KEY` must stay stable: changing it makes stored ciphertext
  undecryptable (the screen then reports the configuration as incomplete and the
  keys must be re-entered).

### Option B - Environment variables

If you prefer provisioning by deployment (or need values before first launch),
set the variables in the root `.env.example` copy in your untracked `.env` (or
deployment secret store): `MMPAY_APP_ID`, `MMPAY_PUBLISHABLE_KEY`,
`MMPAY_SECRET_KEY`, `MMPAY_API_BASE_URL`, `MMPAY_CALLBACK_URL`.

## Activation checklist

1. Back up the POS database and uploads; deploy this version. Startup creates
   `payment_intent` and `payment_webhook_event` tables without deleting sales.
2. Obtain merchant App ID, publishable key, secret key, approved API base URL,
   and any required outbound IP allowlisting from MyanMyanPay.
3. Apply the configuration with **Option A** (Settings -> Payments) or
   **Option B** (.env) above. Both produce the same effective configuration;
   Settings values win until they are cleared.
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

## Merchant compliance (MMQR rules)

| Rule | Where it is enforced |
| --- | --- |
| MMQR logo | `public/photos/mmqr-logo.png` is served byte-for-byte and displayed unmodified (no crop, recolor, distortion or overlay) in the checkout modal and Settings -> Payments. A "Download MMQR logo" action exists in both places. |
| MMK only | The server pins every intent to `MMK` and refuses checkout under another currency. The payment surface formats through `formatMMK()` / `formatMMKAmount()`; a non-MMK register shows "MMK only" instead of a foreign amount. |
| Attribution | `PAYMENT POWERED BY MYANMYANPAY` is rendered directly below the QR. |
| QR integrity | The QR is rendered from the provider payload by ReportLab at `/api/payments/myanmyanpay/intents/<id>/qr` inside a white quiet zone; nothing is drawn over it and the image is never rounded or bordered. |
| 15-minute timer | `expires_at` = creation + 15 minutes, returned with the order. The browser derives an absolute deadline from `expires_at`/`server_now` so a skewed POS clock cannot change it. Legacy uploaded QR images use a 15-minute session window. The timer never reloads, cancels or replaces an order. |
| Download QR | `Download QR` fetches the exact server-rendered SVG (or the uploaded image in legacy mode) and saves it as `MMQR-<order>.svg|png|jpg`. |
| Seamless return | No payment path calls `location.reload()`. Returning to the tab re-reads the saved order in place via `visibilitychange`. |
| State caching | The open order is cached per user+branch in `localStorage` (`mmpay_active:*`) together with the rendered QR (`mmpay_qr:*`). After a reload the order is re-validated with the server and displayed again; no new order is created. |
| Strict re-request | `POST /intents` refuses a second order while an open `CREATING`/`PENDING`/`SUCCESS` order exists for that user and branch (409 plus the existing payment). A new QR requires an explicit provider cancellation that ends the order. |
| Spam prevention | The same server-side guard, the five-second refresh throttle and per-user/per-branch scoping mean refreshing or replaying a browser cannot mint transactions. |

## Platform and KYC obligations

These are merchant obligations; this deployment is a single-merchant checkout and
never acts as an intermediary.

* **Fund holding.** Without a verified CBM permit, the POS must not hold customer
  funds on behalf of sub-merchants. Settlement stays with MyanMyanPay.
* **KYC.** Every business entity processing payments needs an officially verified
  profile. Shadow onboarding is prohibited.
* **Prohibited uses.** Illegal goods, unregulated gambling, adult content,
  financial scams (pyramid schemes, unregulated crypto) and political/religious
  fundraising must never be enabled for MMQR checkout.

## Acceptance checklist (required before live use)

### Recovering an older checkout stuck at CREATING

The provider handshake accepts order IDs of at most 32 characters. New orders
use a 32-character UUID hex string and the adapter validates this before I/O.
Older `POS-` plus UUID-hex IDs were 36 characters and were rejected before QR
creation. Choose **Cancel payment** explicitly to close an old overlength
`CREATING` order with no QR or provider references, then create a new checkout.

Valid-length uncertain orders are never released automatically. A **definitive**
provider rejection (HTTP 400/401/403/404/405/422) proves nothing was created, so
it closes the local order instead of blocking the register with "retry later":
create returns `422` with a `FAILED` order, and refresh/cancel releases a
`CREATING` order with no QR. Transport errors, timeouts, 408/409/429 and 5xx stay
ambiguous and keep the order for reconciliation.

Provider errors remain visible while local status polling runs, and the checkout
explains when no QR has been issued instead of showing a blank frame.

### Amount formatting (verified against the sandbox provider)

The sandbox rejects whole Kyat sent as a JSON float: `6400.0` returned **403**
while `6400` returned `PENDING` and `6400.5` returned `PENDING`. The adapter
therefore serializes integral amounts as JSON integers and keeps only real
subunits as decimals. Never send `Decimal`/float amounts straight to the API.

### Settings persistence across restarts

Stored Settings values are applied by the module-level `apply_payment_settings()`
call that runs during application import, so they survive a container restart for
both entrypoints (`python app.py` and Waitress `app:app`). This was verified on
the server with `docker exec ... python -c "import app"` reporting the saved
switch and keys. After any restart, confirm
`GET /api/payments/myanmyanpay/config` reports `configured: true` and the
expected `sandbox` flag before accepting a payment.

### Do not hand-edit the deployed checkout

`/opt/parrot-pos` is a git checkout guarded by `parrot-pos-deploy`. Copying files
into it makes the tree dirty, so the next guarded deploy refuses to run. The
script performs that check **before** arming its rollback, and reports
`Refusing to deploy: /opt/parrot-pos has uncommitted changes` with the offending
files and the `git reset --hard <origin/main>` fix, exiting `78` without touching
the running release. Exit codes: `64` invalid SHA, `75` deploy already running,
`78` refused pre-flight. Deploy by pushing a commit (the workflow runs the script
for you) or by resetting the checkout to the target commit and rebuilding.

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