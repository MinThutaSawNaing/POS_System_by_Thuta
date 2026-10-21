/* Server-authoritative checkout. No payment state or sale payload is trusted from storage. */
(function (root) {
  "use strict";
  const BASE = "/api/payments/myanmyanpay";
  // Compliance: the payment surface only ever shows MMK, regardless of any
  // currency field that arrives from storage or the provider.
  function formatMMK(value) {
    const amount = Number(value);
    if (!Number.isFinite(amount)) return "MMK —";
    return "MMK " + amount.toLocaleString("en-US", {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
  }
  function createCheckout(env) {
    let config = null, configScope = null, active = null, quote = null;
    let busy = false, visible = false, timer = null, revision = 0, reconciliation = false;
    let terminalNotice = "";
    let operationError = "";
    let view = "checkout", operation = "", lastText = "";
    let pendingCheckoutScope = null;
    let readinessScope = null;
    let pollRevision = 0, pendingPoll = null, lastProviderCheckAt = Date.now();
    const scope = () => env.scope();
    const valid = (s, r) => s !== null && s === scope() && (r === undefined || r === revision);
    const mode = () => configScope !== scope() || !config ? "loading" : config.enabled === false && config.manual_allowed === true ? "legacy" : config.enabled === true && config.configured === true ? "provider" : "blocked";
    // Pausing new payments must not strand already-issued provider orders.
    const canRecover = () => ["provider", "legacy"].includes(mode()) || (configScope === scope() && config?.enabled === false);
    // Absolute validity deadline on the browser clock. The server sends its own
    // clock with the order so a skewed POS clock cannot shorten or extend the
    // mandatory 15-minute window.
    function deadline() {
      if (!active || !active.expires_at || !active.server_now) return null;
      return Date.now() + (Number(active.expires_at) - Number(active.server_now));
    }
    // Cache only identifiers and amounts; the server remains the source of truth.
    function remember() {
      if (!active) return;
      env.remember?.({
        id: active.id, order_id: active.order_id, status: active.status,
        amount: active.amount, currency: active.currency, created_at: active.created_at,
        expires_at: active.expires_at, server_now: active.server_now,
        qr_url: active.qr_url || null,
      });
    }
    const status = (text) => {
      lastText = text;
      env.render({mode: mode(), sandbox: config?.sandbox === true, recovery: canRecover(), payment: active, busy, view, operation, text: terminalNotice + (operationError ? operationError + " " : "") + text, deadline: deadline()});
    };
    async function completeIfVerified() {
      // Called only after a successful server response, never from cached/listed
      // status. Stop automatic retries after a recording failure: keep the paid
      // order for explicit retry/reconciliation instead of posting every poll.
      if (visible && active?.status === "SUCCESS" && !busy && !reconciliation) await action("complete");
    }
    async function request(path, method = "GET", body) {
      if (!env.online()) throw new Error("Mobile payments require an online connection. Do not pay again if a request is uncertain.");
      const options = {method, cache: "no-store", credentials: "same-origin"};
      if (method === "POST") {
        options.headers = {"Content-Type": "application/json", "X-POS-Payment-Request": "1"};
        if (body !== undefined) options.body = JSON.stringify(body);
      }
      const response = await env.fetch(BASE + path, options);
      const data = await response.json();
      if (!response.ok || data.success === false) {
        const error = new Error(data.message || "Payment request failed. Retain this payment and retry; do not pay again.");
        // An uncertain provider operation can still have a persisted order.
        // Callers adopt it only after validating their captured scope/revision.
        if (data.payment?.id != null) error.payment = data.payment;
        throw error;
      }
      return data;
    }
    function stop() { if (timer) env.clearTimeout(timer); timer = null; }
    function schedule() {
      stop();
      if (visible && active && scope() !== null) timer = env.setTimeout(poll, 3000);
    }
    function display() {
      if (active && ["FAILED", "EXPIRED", "CANCELLED", "CANCELED", "REFUNDED"].includes(active.status)) {
        const reference = active.order_id || active.id;
        terminalNotice = active.status === "REFUNDED"
          ? `REFUNDED · Order ${reference}: manager reconciliation required. Confirm the refund before accepting a new payment for this order. `
          : `${active.status} · Order ${reference}: original payment closed; checkout can create a new order. `;
        active = null; quote = null; reconciliation = false;
        env.forget?.();
        stop();
      }
      remember();
      const changed = quote && quote.snapshot !== env.snapshot();
      status(active ? `${reconciliation ? "Payment received; sale needs reconciliation, do not pay again. " : ""}${active.status} · Order ${active.order_id || active.id}${changed ? " — Cart changed: this QR pays ONLY the original frozen order, not the current cart." : ""}` : "Ready. Validate checkout to create a payment QR, or find pending payments.");
      schedule();
    }
    async function loadConfig() {
      const s = scope();
      if (s === null) return;
      if (configScope !== s) { active = null; quote = null; busy = false; reconciliation = false; terminalNotice = ""; operationError = ""; view = "checkout"; operation = ""; revision++; pollRevision++; pendingPoll = null; lastProviderCheckAt = Date.now(); stop(); }
      config = null; configScope = s;
      readinessScope = s;
      try {
        const data = await request("/config");
        if (!valid(s)) return;
        config = data;
        env.ready?.(mode(), canRecover());
        // A reload must restore the open order and its QR instead of minting a
        // new one. restore() only re-reads an order the server already owns.
        if (!active && canRecover()) await restore();
        if (!valid(s)) return;
        readinessScope = null;
        if (visible && mode() === "legacy" && !active && view !== "discovery") env.legacy?.();
        else if (visible) preview();
        if (pendingCheckoutScope === s) {
          pendingCheckoutScope = null;
          if (visible && mode() === "provider" && !active) env.checkoutReady?.();
        }
      } catch (error) { if (valid(s)) status("Cannot verify payment configuration. Mobile checkout is blocked. " + error.message); }
      finally { if (readinessScope === s) readinessScope = null; }
    }
    function preview(checkoutView) {
      if (checkoutView === "checkout") view = "checkout";
      visible = true;
      env.open();
      if (mode() === "loading") { status("Checking mobile payment readiness. No payment can be accepted yet."); if (configScope !== scope()) loadConfig(); }
      else if (view === "discovery" && !active) status("Find unfinished payments for your account in this branch. Searching does not create a new payment.");
      else if (mode() === "blocked") status(config.message || "MyanMyanPay is enabled but not configured. Mobile checkout is blocked; contact a manager.");
      else display();
      return false;
    }
    async function list() {
      if (busy || !canRecover()) return;
      if (active) { preview(); return; }
      const s = scope(), r = revision;
      view = "discovery"; operation = "search"; operationError = "";
      env.list([], () => {});
      busy = true; status("Finding unfinished payments for your account in this branch…");
      try {
        const data = await request("/intents");
        if (!valid(s, r)) return;
        const payments = data.payments || [];
        env.list(payments, payment => { if (valid(s, r)) resume(payment); });
        status(payments.length
          ? "Select an existing payment to resume. A paid payment must be completed, not paid again."
          : "No unfinished payments found for your account in this branch. Close this window to continue checkout.");
      } catch (error) { if (valid(s, r)) status(error.message); }
      finally { if (valid(s, r)) { busy = false; operation = ""; status(lastText); env.busy(false); } }
    }
    function resume(payment) {
      if (busy || scope() === null || !canRecover()) return;
      view = "checkout";
      revision++; active = payment; quote = null; reconciliation = false;
      pollRevision++; lastProviderCheckAt = Date.now();
      display();
      poll();
    }
    async function restore() {
      const cached = env.restore?.();
      if (!cached || cached.id == null || active || busy || !canRecover()) return false;
      const s = scope(), r = revision;
      if (s === null) return false;
      try {
        const data = await request("/intents/" + encodeURIComponent(cached.id));
        if (!valid(s, r) || active) return false;
        const payment = data.payment;
        if (!payment || !["CREATING", "PENDING", "SUCCESS"].includes(payment.status)) {
          env.forget?.();
          return false;
        }
        active = payment; visible = true; env.open();
        status("Restored the saved order after reload. This QR belongs to that order; do not create another payment.");
        poll();
        return true;
      } catch (error) {
        // A cached order the server no longer returns must not be shown again.
        if (valid(s, r) && !error.payment) env.forget?.();
        return false;
      }
    }
    async function start(saleData) {
      view = "checkout";
      env.list([], () => {});
      if (mode() === "loading" || readinessScope === scope()) {
        pendingCheckoutScope = scope();
        preview();
        return;
      }
      preview();
      if (busy || mode() !== "provider") return;
      const snapshot = env.snapshot();
      if (active) {
        status("An existing payment is selected. It belongs only to its original order. Complete or cancel it on the server before starting another payment.");
        return;
      }
      if (quote && quote.snapshot !== snapshot) {
        status("The cart changed after an uncertain create request. Find pending payments and reconcile/cancel the old order before creating another payment.");
        return;
      }
      const s = scope(), r = revision;
      // Keep this ID and frozen payload on all retries, including create timeouts.
      if (!quote) quote = {snapshot, payload: JSON.parse(JSON.stringify({...saleData, transaction_id: env.uuid()}))};
      busy = true; operation = "search"; status("Checking for existing payments…");
      try {
        const pending = await request("/intents");
        if (!valid(s, r)) return;
        if ((pending.payments || []).length) {
          env.list(pending.payments, payment => { if (valid(s, r)) resume(payment); });
          status("Resume or cancel an existing payment before creating a new one. Do not pay again.");
          return;
        }
        if (snapshot !== env.snapshot()) { status("Cart changed. Checkout again after resolving this order."); return; }
        operation = "generate"; status("Generating your payment QR. Please wait…");
        const data = await request("/intents", "POST", quote.payload);
        if (!valid(s, r)) return;
        active = data.payment;
        lastProviderCheckAt = Date.now();
        operationError = "";
        display();
      } catch (error) {
        if (valid(s, r)) {
          if (error.payment) active = error.payment;
          operationError = error.message;
          status(error.message + " Find pending payments or refresh this same order; do not pay again.");
        }
      }
      finally { if (valid(s, r)) { busy = false; operation = ""; env.busy(false); display(); } }
      if (valid(s, r)) await completeIfVerified();
    }
    async function poll() {
      stop();
      if (mode() === "loading" && visible) { status("Payment context changed. Reload readiness before resuming this order."); return; }
      if (!visible || !active || busy || !canRecover()) { schedule(); return; }
      if (pendingPoll) { schedule(); return; }
      const s = scope(), r = revision, id = active.id;
      const token = {revision: pollRevision};
      pendingPoll = token;
      try {
        const data = await request("/intents/" + encodeURIComponent(id));
        if (!valid(s, r) || active?.id !== id || token.revision !== pollRevision || !visible) return;
        active = data.payment;
        if (active?.status === "SUCCESS" && !reconciliation) operationError = "";
        display();
        await completeIfVerified();
        // Webhooks normally arrive first. If delivery is delayed or missed,
        // reconcile the SAME order via the authenticated server-side provider
        // lookup, no more than once every 15 seconds (server throttle is 5s).
        if (active && ["CREATING", "PENDING"].includes(active.status)
            && Date.now() - lastProviderCheckAt >= 15000) await action("refresh");
      } catch (error) { if (valid(s, r) && token.revision === pollRevision && visible) status(error.message); }
      finally {
        if (pendingPoll === token) pendingPoll = null;
        if (valid(s, r)) schedule();
      }
    }
    async function action(name) {
      if (busy || !active || !canRecover()) return;
      if (name === "complete" && active.status !== "SUCCESS") return;
      if (!["complete", "refresh", "cancel"].includes(name)) return;
      const s = scope(), r = revision, id = active.id;
      let verified = false;
      // Invalidate an older GET so it cannot replace a just-verified payment.
      pollRevision++;
      if (name === "refresh") lastProviderCheckAt = Date.now();
      busy = true; operation = name; status(name === "complete" ? "Payment verified. Recording the sale…" : "Contacting payment server…");
      try {
        const data = await request("/intents/" + encodeURIComponent(id) + "/" + name, "POST");
        if (!valid(s, r) || active?.id !== id) return;
        if (name === "complete") {
          if (!data.success || !data.transaction_id) throw new Error("Sale confirmation missing.");
          const clearCart = !!quote && quote.snapshot === env.snapshot();
          active = null; quote = null; busy = false; operation = ""; reconciliation = false; operationError = ""; revision++; stop();
          visible = false; pendingCheckoutScope = null;
          env.forget?.();
          env.completed(data, clearCart); status("Sale confirmed. Payment completed.");
        } else {
          active = data.payment;
          verified = active?.status === "SUCCESS";
          operationError = "";
          display();
        }
      } catch (error) {
        if (valid(s, r)) {
          if (error.payment && error.payment.id === id) active = error.payment;
          operationError = error.message;
          if (name === "complete") reconciliation = true;
          if (active && ["FAILED", "EXPIRED", "CANCELLED", "CANCELED", "REFUNDED"].includes(active.status)) { display(); return; }
          status(name === "complete" ? "Payment received; sale needs reconciliation, do not pay again. " + error.message : error.message);
        }
      } finally { if (valid(s, r) || (s === scope() && !active)) { busy = false; operation = ""; env.busy(false); schedule(); } }
      if (verified && valid(s, r)) await completeIfVerified();
    }
    return {mode, preview, start, list, resume, action, loadConfig,
      close() { visible = false; pendingCheckoutScope = null; pollRevision++; stop(); },
      state: () => ({active, quote, busy})};
  }
  if (typeof module !== "undefined" && module.exports) module.exports = {createCheckout};
  if (!root.document) return;
  const doc = root.document, el = (id) => doc.getElementById(id);
  const modal = el("mmqrPaymentModal");
  const checkoutScope = () => typeof currentBranch === "undefined" || !currentBranch?.id ||
    (typeof dashboardSessionStale !== "undefined" && dashboardSessionStale) ||
    (typeof dashboardBranchStale !== "undefined" && dashboardBranchStale) ||
    (typeof branchSwitchInFlight !== "undefined" && branchSwitchInFlight) ? null :
    JSON.stringify([typeof CACHE_USER_ID === "undefined" ? null : CACHE_USER_ID, currentBranch.id]);

  // Reload support: the open order and its rendered QR are cached per
  // user+branch, so a refresh replays the same order instead of minting a new
  // one. Nothing secret is cached: the server re-validates everything.
  const storage = () => { try { return root.localStorage; } catch (error) { return null; } };
  const activeCacheKey = () => { const s = checkoutScope(); return s === null ? null : `mmpay_active:${s}`; };
  const qrCacheKey = () => { const s = checkoutScope(); return s === null ? null : `mmpay_qr:${s}`; };
  function readCachedQr(id) {
    const key = qrCacheKey(), store = storage();
    if (!key || !store) return null;
    try {
      const parsed = JSON.parse(store.getItem(key) || "null");
      return parsed && parsed.id === id && parsed.svg
        ? "data:image/svg+xml;charset=utf-8," + encodeURIComponent(parsed.svg) : null;
    } catch (error) { return null; }
  }
  async function cacheQrImage(url, id) {
    const key = qrCacheKey(), store = storage();
    if (!key || !store) return;
    try {
      const response = await root.fetch(url, {credentials: "same-origin", cache: "no-store"});
      if (!response.ok) return;
      const svg = await response.text();
      if (!svg.includes("<svg")) return;
      store.setItem(key, JSON.stringify({id, svg}));
    } catch (error) { /* offline: the visible QR is unaffected */ }
  }
  const snapshot = () => JSON.stringify({cart, fields: ["payment-method", "pos-customer-select", "split-method-one", "split-method-two", "split-amount-one", "split-amount-two", "pos-enable-delivery", "pos-delivery-recipient-name", "pos-delivery-recipient-phone", "pos-delivery-address", "pos-delivery-priority", "pos-delivery-fee"].map(id => [id, el(id)?.value, el(id)?.checked])});
  let lastActionState = null;
  function renderActions({mode, payment, busy, sandbox = false, recovery = false, view = "checkout"}) {
    lastActionState = {mode, payment, busy, sandbox, recovery, view};
    const recover = recovery || mode === "provider" || mode === "legacy";
    const existing = el("mmp-open-existing");
    existing.classList.toggle("d-none", !recover);
    existing.disabled = busy || !recover;
    existing.textContent = payment ? `Resume payment — ${formatMMK(payment.amount)}` : "Find unfinished payments";
    existing.setAttribute("aria-busy", String(busy));
    const paid = payment?.status === "SUCCESS";
    const pending = recover && !!payment && !paid;
    const confirm = el("confirm-mmqr-payment-btn");
    // Pending orders must be checked, never manually marked paid or recreated.
    confirm.classList.toggle("d-none", pending || (view === "discovery" && !payment));
    confirm.textContent = paid ? busy ? "Completing sale…" : sandbox ? "Complete sandbox sale" : "Complete verified sale"
      : mode === "legacy" && !payment ? "Record manually confirmed sale"
      : mode === "loading" ? "Checking payment setup…"
      : mode === "blocked" ? "Payment unavailable"
      : busy ? "Please wait…" : "Retry payment QR";
    confirm.disabled = busy || !recover || (mode === "blocked" && !payment) || (!!payment && !paid);
    confirm.setAttribute("aria-busy", String(busy && !pending));
    el("mmp-refresh").classList.toggle("d-none", !pending);
    el("mmp-refresh").textContent = busy ? "Please wait…" : "Check now";
    el("mmp-refresh").setAttribute("aria-busy", String(busy && pending));
    el("mmp-cancel").classList.toggle("d-none", !pending);
    el("mmp-resume").classList.toggle("d-none", !recover || !!payment);
    ["mmp-refresh", "mmp-cancel", "mmp-resume"].forEach(id => { el(id).disabled = busy || !recover; });
    el("mmqr-close-btn").textContent = payment ? "Close — resume later" : "Close";
    el("mmqr-close-note").classList.toggle("d-none", !payment);
    el("mmp-auto-status").classList.toggle("d-none", !pending);
    el("mmp-auto-status").textContent = "Checking payment status automatically. Once verified, we’ll open Done / Print receipt.";
  }
  function setBusy(value) {
    if (lastActionState) renderActions({...lastActionState,
      mode: checkout.mode(), payment: checkout.state().active, busy: value});
  }
  const checkout = createCheckout({
    fetch: (...args) => root.fetch(...args), online: () => navigator.onLine !== false,
    scope: checkoutScope,
    remember(payment) {
      const key = activeCacheKey(), store = storage();
      if (!key || !store) return;
      try { store.setItem(key, JSON.stringify(payment)); } catch (error) { /* quota */ }
    },
    restore() {
      const key = activeCacheKey(), store = storage();
      if (!key || !store) return null;
      try { return JSON.parse(store.getItem(key) || "null"); } catch (error) { return null; }
    },
    forget() {
      const key = activeCacheKey(), qrKey = qrCacheKey(), store = storage();
      if (!store) return;
      try {
        if (key) store.removeItem(key);
        if (qrKey) store.removeItem(qrKey);
      } catch (error) { /* ignore */ }
    },
    snapshot, uuid: () => root.crypto.randomUUID(),
    // Revalidate current fields rather than submitting a stale cart captured
    // while readiness or saved-order recovery was still in flight.
    checkoutReady() {
      const method = el("payment-method")?.value;
      if (method === "mobile_payment" || (method === "split_payment" && splitUsesMobilePayment())) completeSale(false);
    },
    ready(mode, recovery) {
      renderActions({mode, recovery, payment: checkout.state().active, busy: checkout.state().busy});
    },
    legacy() {
      el("mmp-status").textContent = "Manual payment — not verified by MyanMyanPay. No provider request will be sent.";
      renderActions({mode: "legacy", payment: null, busy: false});
      el("mmqr-payment-help").textContent = "Manual payment only. Independently check the merchant account before recording the sale.";
      showMMQRPaymentModal();
    },
    setTimeout: root.setTimeout.bind(root), clearTimeout: root.clearTimeout.bind(root),
    open() { (bootstrap.Modal.getInstance(modal) || new bootstrap.Modal(modal)).show(); },
    busy: setBusy,
    render({mode, sandbox, recovery, payment, busy, text, deadline, view, operation}) {
      el("mmp-status").textContent = text;
      const provider = mode === "provider";
      const recover = recovery || provider || mode === "legacy";
      renderActions({mode, payment, busy, sandbox, recovery, view});
      const discovery = view === "discovery" && !payment;
      el("mmqr-payment-title").textContent = discovery ? "Find unfinished payments"
        : payment?.status === "SUCCESS" ? "Payment verified" : "Scan MMQR to pay";
      el("mmqr-payment-help").textContent = sandbox
        ? "SANDBOX — no real money. Use the provider sandbox callback emulator. Verified sandbox payments automatically record a sale and update POS stock and reports."
        : discovery ? "Only unfinished payments for your account in this branch appear here. Searching never creates a payment or records a sale."
        : mode === "legacy" && !payment ? "Manual payment — not verified by MyanMyanPay. No provider request will be sent. Independently check the merchant account before recording the sale."
        : "Only server-verified payment can complete this sale. Closing does not cancel payment. Existing QR always belongs to its frozen order.";
      const image = el("pos-mmqr-image");
      // Never disturb the uploaded legacy QR while rendering an empty provider state.
      const showQr = Boolean(recover && payment?.qr_url && payment.status === "PENDING");
      if (!showQr && (mode !== "legacy" || payment || discovery)) {
        image.style.display = "none"; image.removeAttribute("src");
      }
      const missing = el("pos-mmqr-missing");
      missing.style.display = "none";
      // Compliance: only MMK may appear on the payment surface.
      el("pos-mmqr-total").textContent = payment ? formatMMK(payment.amount) : "—";
      const legacyQr = mode === "legacy" && !payment && !discovery;
      el("mmqr-amount-summary").classList.toggle("d-none", discovery);
      el("mmqr-timer-box").classList.toggle("d-none", !showQr && !legacyQr);
      el("mmqr-downloads").classList.toggle("d-none", !showQr && !legacyQr);
      el("mmqr-qr-surface").classList.toggle("d-none", !showQr && !legacyQr);
      el("mmqr-attribution").classList.toggle("d-none", discovery);
      if (!showQr && !legacyQr) {
        missing.textContent = discovery ? text : payment?.status === "CREATING"
          ? "No QR has been issued for this order. Check payment status to recover it, or explicitly cancel the order before trying again."
          : payment?.status === "SUCCESS" ? busy ? "Payment verified. Recording your sale — please wait." : "Payment received. If sale recording failed, retry below or ask a manager to reconcile. Do not pay again."
          : busy ? operation === "generate" ? "Generating your payment QR. Please wait…" : text
          : mode === "blocked" || mode === "loading" ? text
          : "No QR is active. Return to checkout to generate a payment, or find an unfinished payment.";
        missing.style.display = "block";
      }
      if (deadline && showQr) resetMMQRCountdown(deadline, true);
      else if (mode !== "legacy" || payment || discovery) stopMMQRCountdown();
      if (payment || mode !== "legacy" || discovery) setMMQRDownloadState(showQr);
      if (showQr) {
        // Prefer the cached copy so a reload (even offline) shows the same QR.
        const inline = readCachedQr(payment.id);
        if (inline) {
          if (image.getAttribute("src") !== inline) image.src = inline;
          image.style.display = "block";
        } else {
          const url = BASE + "/intents/" + encodeURIComponent(payment.id) + "/qr";
          const pinned = typeof dashboardIdentityUrl === "function" ? dashboardIdentityUrl(url) : url;
          if (pinned) {
            if (image.getAttribute("src") !== pinned) image.src = pinned;
            image.style.display = "block";
            cacheQrImage(pinned, payment.id);
          }
        }
      }
    },
    list(payments, resume) {
      el("mmp-existing").replaceChildren();
      payments.forEach(payment => {
        const button = doc.createElement("button");
        button.type = "button"; button.className = "btn btn-outline-primary mmqr-resume-payment";
        button.textContent = `Resume ${payment.order_id || payment.id}: ${formatMMK(payment.amount)} — ${payment.status}`;
        button.onclick = () => { el("mmp-existing").replaceChildren(); resume(payment); };
        el("mmp-existing").appendChild(button);
      });
    },
    completed(data, clearCart) {
      if (clearCart) {
        cart = []; updateCartDisplay(); resetSplitPaymentFields();
        el("cash-received").value = ""; el("pos-enable-delivery").checked = false;
        togglePosDeliveryFields(); updateCashRefundDisplay();
      } else showToast("Original payment sale completed. Current cart was retained; it was not part of this payment.", "info");
      showSaleSuccessModal(data.transaction_id, data.delivery_number, false,
        lastActionState?.sandbox === true
          ? "SANDBOX sale recorded — simulated payment, no real money received. POS stock and reports were updated."
          : "Sale recorded using the saved provider payment status. Reconcile the order with your merchant account.");
      ["sales-start-date-filter", "sales-end-date-filter", "sales-search"].forEach(id => { if (el(id)) el(id).value = ""; });
      loadSales(); loadDashboardStats(); invalidateProductsCache();
    }
  });
  root.MyanMyanPayCheckout = checkout;
  // The old manual handler is unreachable in enabled/loading/blocked modes.
  const manual = root.confirmMMQRPayment;
  root.confirmMMQRPayment = () => checkout.state().active
    ? checkout.state().active.status === "SUCCESS" ? checkout.action("complete") : checkout.preview()
    : checkout.mode() === "legacy" ? manual() : completeSale(false);
  el("mmp-resume").onclick = checkout.list;
  el("mmp-open-existing").onclick = () => {
    const current = checkout.state();
    if (current.busy) return;
    checkout.preview();
    if (!current.active) return checkout.list();
  };
  el("mmp-refresh").onclick = () => checkout.action("refresh");
  el("mmp-cancel").onclick = () => { if (root.confirm("Request cancellation from the provider? Closing the modal alone does not cancel.")) checkout.action("cancel"); };
  modal.addEventListener("hidden.bs.modal", checkout.close);
  // Seamless return from the banking app: re-read the saved order in place. The
  // page is never reloaded, and returning never requests a new transaction.
  doc.addEventListener("visibilitychange", () => {
    if (doc.hidden) return;
    const current = checkout.state();
    if (current.active) checkout.resume(current.active);
  });
  checkout.loadConfig();
})(typeof window === "undefined" ? globalThis : window);