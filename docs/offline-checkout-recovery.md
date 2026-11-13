# Browser checkout recovery (one central server)

## Deployment boundary

The checked-in application uses Flask/Waitress and **one central SQLite database**
(`instance/pos.db`). Compose provisions one application service with one database
volume. Branches are rows with scoped operational records, not independent
databases. A/B continue submitting ordinary requests while C/D upload browser
outboxes to the same server. No branch database replication is implemented.
The production-only `compose.server.yaml` referenced by the deployment script is
not in this repository; its actual configuration has not been verified.

The service worker caches public assets only. A disconnected register must keep
its authenticated POS screen open and have already cached its branch catalogue.
Offline reload/login, new catalogue downloads and provider payment verification
are not supported. Never distribute the SQLite file to branches or place it on
a shared network drive as an alternative synchronization mechanism.

The checkout button probes the central server with a three-second deadline, even
when Wi-Fi reports a connection. New eligible cash baskets save offline
automatically when the server is unreachable; there is no manual offline switch.
A background probe runs every 20 seconds and resumes synchronization when service
returns. Never recreate an uncertain prior checkout: automatic recovery resends
its original ID and unchanged payload instead. If checkout inputs change during
the probe, the cashier must confirm the updated basket with another click.

## Agreed financial policy

- Newly disconnected sales support **cash only**, with no coupons, deliveries,
  customer debt, external/card/mobile tender or split payments.
- Central stock must never become negative. An offline receipt is a provisional
  local record, not a central stock reservation or confirmation of settlement.
- Upload validates branch/account, current stock, price/promotion eligibility,
  tax snapshot and expected total. A conflict leaves the original cash, payload
  and receipt unchanged for manager review. There is no automatic repricing,
  tender conversion, debt forgiveness, stock adjustment or external verification.
- Client occurrence time is untrusted trace metadata. Existing server sale dates
  and reporting semantics remain unchanged: acceptance is recorded at upload time.
- A request originally sent online whose response is lost is **not** a new
  offline cash sale. Recovery must resend the exact original ID and payload,
  including its original tender. Provider checkout retains its separate saved
  payment-intent workflow.

## Server retry identity

Newly accepted client transaction IDs are written to `checkout_request` in the
same transaction as the sale, stock decrements, debt/delivery/coupon changes and
audit evidence. This independent ledger stores a SHA-256 canonical request
fingerprint, original account/branch, device and client occurrence time when
provided, server acceptance time, and the original response. It deliberately has
no cascading foreign keys to deletable sales or products.

A matching retry returns the recorded result without consulting mutable stock,
customer or promotion records and without repeating financial effects. A changed
payload or account/branch mismatch is not acknowledged as a successful replay.
An existing deletion tombstone prevents recreation after history deletion.
Sales predating the ledger cannot be retroactively fingerprinted reliably; their
retries return `legacy_checkout_review` for manual receipt reconciliation rather
than guessing that an ID match proves the same purchase.

SQLite writes remain short, serialized transactions using the existing
`BEGIN IMMEDIATE`, WAL, per-connection busy timeout and guarded stock decrements.
Lock retries are bounded; exhausted temporary failures return HTTP 503 and must
retain the same browser transaction identity. Uploads are individual transactions,
not a database-wide bulk synchronization lock. This is a correctness mechanism,
not evidence of unlimited four-branch throughput; stage representative load on
the actual server before rollout.

## Browser recovery and manager review

The outbox uses an append-only browser journal scoped to original user and branch.
It retains the immutable checkout/receipt, attempt starts, outcomes and errors;
accepted results are retained as local evidence rather than erased. Unique
per-entry/per-attempt keys avoid whole-array read/modify/write data loss between
tabs. Native Web Locks, when available, additionally serialize same-ID sends;
the server ledger still protects against duplicate transport without Web Locks.

New pending intents and uncertain attempts automatically upload/replay with their
original ID, payload and tender. Automatic retries wait at least one minute after
an attempt/result, exponentially backing off to five minutes. HTTP 408/429/5xx,
transport failures and crashes remain uncertain, not failed sales. HTTP 401 pauses
until the original session is usable again (with the same backoff); permission
denials, business rejections, stock/price conflicts and corrupt data require help.
Any definite rejection evidence blocks automatic retry even if a later attempt
has an uncertain result. A confirmed server acceptance takes precedence over another tab's
late failed response. Storage-write failure must not be reported as a saved sale.
Transport and response-body waits are bounded to 30 seconds; timeout/5xx/malformed
success responses are uncertain outcomes, not proof that no sale committed.
Corrupt or orphaned attempt records are exposed for export and reconciliation.

Use **Sales History → Manager help / saved sales** to inspect the transaction ID, attempt/error state,
original receipt and JSON export. Give the export to the manager to reconcile
cash taken, physical goods, central receipt and stock. If a stock conflict is
legitimately corrected, use the existing authorized stock workflow, then retry
the **unchanged** request. Price/tax conflicts that cannot be resolved without
rewriting the original need manual accounting reconciliation; do not fabricate
a second sale or modify the outbox to force acceptance.

Retry remains in the original user's authenticated branch/account. The review
confirmation records a manager-review attestation; it is an **operational control,
not separately authenticated manager approval**. It neither elevates a cashier's
permissions nor transfers a cashier's queue to a manager's login. A requirement
for technically enforced manager sign-off needs an additional approval workflow.
Old v2/ownerless queues are preserved without silent adoption or automatic upload;
export and reconcile them explicitly.

Do not clear site data, change browser profiles/origins or discard the device
before recovery. Browser storage is not an off-device backup: clearing/eviction,
profile or disk loss can destroy records never sent to the server. Export pending
records promptly, restrict access to exports containing customer/receipt details,
and retain them under the business's data-handling policy. The journal is not
tamper-proof evidence against a malicious browser user. Retaining accepted local
evidence consumes storage; there is intentionally no automatic destructive purge.

## Safe rollout and rollback

No production data or deployment commands are part of this implementation.
Startup creates the additive `checkout_request` table through the existing
`db.create_all()` convention. No existing sale/stock/tender columns are rewritten,
and no historical fingerprint backfill is attempted.

Before an authorized rollout, stop writes and make a verified independent SQLite
backup plus matching uploads using the standalone backup utility. Rehearse on a
staging copy, start one application process for schema creation, then restart it
again to verify idempotent initialization. Check pending browsers before upgrading;
already-open old JavaScript continues using its previous behavior until reloaded.
Keep the same server origin, browser profile and branch/account association.

An old application can ignore the new table, but rolling back code **removes the
new retry guarantees and browser recovery UI**. Do not permit pending new-browser
uploads against old code or delete the ledger. Restoring an older database loses
accepted transactions and their retry evidence after the backup, potentially
allowing duplicates if browser outboxes are subsequently retried. Stop every
process, preserve the newer database/outboxes for reconciliation, and coordinate
database, code and browser rollback before reopening writes. Do not automatically
drop tables or clear browser storage as a rollback strategy.

## Validation discipline

Run tests only with `python run_tests.py [pytest selectors]`. Application import
initializes/migrates a database, so direct application-importing pytest/unittest
commands in the working checkout are unsafe. The runner copies source, excludes
database/uploads/secrets and runs against disposable data. Node-based tests execute
the actual browser helpers with controlled DOM/network/storage doubles; these do
not replace real multi-tab browser and outage testing before production rollout.

### Additional failure simulations

`test_checkout_recovery_browser.py` runs four independent local Chromium profiles
against one live central Flask/SQLite application when Chrome/Chromium/Edge is
available (otherwise it explicitly skips). It reloads saved intents, overlaps
A/B online checkout with C/D offline uploads, discards responses after central
commit, and verifies reviewed same-ID retries and final central stock/ledger counts.
The harness injects local test sessions and uses production journal/sync functions;
it is not a full cashier UI or physical-register acceptance test.

Additional regressions cover stalled headers/body (30-second transport deadline),
malformed success replies, orphaned/corrupt events, failed storage writes before
and after transport, unfinished attempts with clock rollback, optional UI failures
after saving, duplicate-product stock audit transitions, bounded exhausted SQLite
locks and repeated 32-request four-branch duplicate bursts. Timeouts and HTTP 5xx
remain **unknown**, not proof that the server rolled back. Automatic recovery
replays the unchanged intent using the server ledger; never take payment again
on the assumption that the first attempt failed.

The production workflow currently deploys automatically on pushes to `main`.
A source-only push without rollout authorization must suppress that workflow
(for example, a GitHub-recognized `[skip ci]` commit marker); this also skips hosted
CI, so retain local disposable-runner evidence. A later authorized manual workflow
dispatch still runs tests before deployment. Inspect any other configured external
deployment integrations before assuming a push cannot affect production.

### Verification record — November 13, 2026

- Expanded disposable full suite and independent final push-candidate rerun:
  **1,680 passed, 173 skipped**, with 304 passing subtests and no failures on
  both runs. The final rerun took 339.27 seconds. A separate repeated
  backend/Node/real-browser/live-HTTP/delivery stress selection passed **159
  tests** without failures. The final `-rs` report identifies subprocess-isolation
  guards among the skips; their parent launchers execute the child checks.
- The original three delivery-image failures were reproduced from a clean
  `git archive HEAD`. The compatibility fix now bounds the individual proof
  request stream without assigning Flask's version-dependent
  `request.max_content_length` property or changing a global configuration.
  Regressions cover normal image/signature handling, oversized requests, and
  bounded input streams without a content-length header. No library was added.
- Four real local Chrome profiles completed the one-server simulation: each
  persisted three intents, reloaded the page, lost the first response after
  server commit, and recovered by replaying the original ID. Final assertions
  require exactly 12 sales and ledger rows, unchanged debt count and stock 37
  from initial stock 40 in every branch. This uses the production journal/sync
  functions and API with test-only session/harness plumbing, not physical POS
  terminals or a live payment provider.
- Node regressions cover concurrent tabs with/without Web Locks, quota failures,
  uncertain replies, stalled transport/body, original-account/branch isolation,
  corrupt/orphaned events, optional display failures, saved receipt labels,
  unsupported tender rejection and reviewed unchanged-payload retries.
- Backend regressions additionally cover repeated four-branch 32-request bursts,
  same-key payload conflicts, stock races, duplicate-product audit transitions,
  real exhausted/transient SQLite locks, database-write rollback, preserved debt,
  deleted-history tombstones and startup/replay across separate processes.
- Skips include parent/child isolation fixture guards and explicitly optional
  browser/dependency checks; skipped checks are not claimed as passes. Legacy
  SQLAlchemy/Python deprecation warnings remain and are not silently hidden.
- JavaScript syntax checks and `git diff --check` pass. All application-importing
  tests run through the disposable runner; production data and deployment remain
  untouched. Source publication is separate from an authorized rollout.