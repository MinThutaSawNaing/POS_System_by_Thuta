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
    const scope = () => env.scope();
    const valid = (s, r) => s !== null && s === scope() && (r === undefined || r === revision);
    const mode = () => configScope !== scope() || !config ? "loading" : config.enabled === false ? "legacy" : config.enabled === true && config.configured === true ? "provider" : "blocked";
    const canRecover = () => ["provider", "legacy"].includes(mode());
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
    const status = (text) => env.render({mode: mode(), payment: active, busy, text: terminalNotice + (operationError ? operationError + " " : "") + text, deadline: deadline()});
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
      if (configScope !== s) { active = null; quote = null; busy = false; reconciliation = false; terminalNotice = ""; operationError = ""; revision++; stop(); }
      config = null; configScope = s;
      try {
        const data = await request("/config");
        if (!valid(s)) return;
        config = data;
        env.ready?.(mode());
        // A reload must restore the open order and its QR instead of minting a
        // new one. restore() only re-reads an order the server already owns.
        if (!active && canRecover()) restore();
        if (visible && mode() === "legacy" && !active) env.legacy?.();
        else if (visible) preview();
      } catch (error) { if (valid(s)) status("Cannot verify payment configuration. Mobile checkout is blocked. " + error.message); }
    }
    function preview() {
      visible = true;
      env.open();
      if (mode() === "loading") { status("Checking mobile payment readiness. No payment can be accepted yet."); if (configScope !== scope()) loadConfig(); }
      else if (mode() === "blocked") status(config.message || "MyanMyanPay is enabled but not configured. Mobile checkout is blocked; contact a manager.");
      else display();
      return false;
    }
    async function list() {
      if (busy || !canRecover()) return;
      const s = scope(), r = revision;
      busy = true; status("Finding pending and paid payments…");
      try {
        const data = await request("/intents");
        if (!valid(s, r)) return;
        env.list(data.payments || [], payment => { if (valid(s, r)) resume(payment); });
        status("Select an existing payment to resume. A paid payment must be completed, not paid again.");
      } catch (error) { if (valid(s, r)) status(error.message); }
      finally { if (valid(s, r)) { busy = false; env.busy(false); } }
    }
    function resume(payment) {
      if (busy || scope() === null || !canRecover()) return;
      revision++; active = payment; quote = null; reconciliation = false;
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
      busy = true; status("Checking for existing payments…");
      try {
        const pending = await request("/intents");
        if (!valid(s, r)) return;
        if ((pending.payments || []).length) {
          env.list(pending.payments, payment => { if (valid(s, r)) resume(payment); });
          status("Resume or cancel an existing payment before creating a new one. Do not pay again.");
          return;
        }
        if (snapshot !== env.snapshot()) { status("Cart changed. Checkout again after resolving this order."); return; }
        const data = await request("/intents", "POST", quote.payload);
        if (!valid(s, r)) return;
        active = data.payment;
        operationError = "";
        display();
      } catch (error) {
        if (valid(s, r)) {
          if (error.payment) active = error.payment;
          operationError = error.message;
          status(error.message + " Find pending payments or refresh this same order; do not pay again.");
        }
      }
      finally { if (valid(s, r)) { busy = false; env.busy(false); if (active) display(); } }
    }
    async function poll() {
      stop();
      if (mode() === "loading" && visible) { status("Payment context changed. Reload readiness before resuming this order."); return; }
      if (!visible || !active || busy || !canRecover()) { schedule(); return; }
      const s = scope(), r = revision, id = active.id;
      try {
        const data = await request("/intents/" + encodeURIComponent(id));
        if (!valid(s, r) || active?.id !== id) return;
        active = data.payment; display();
      } catch (error) { if (valid(s, r)) status(error.message); }
      finally { if (valid(s, r)) schedule(); }
    }
    async function action(name) {
      if (busy || !active || !canRecover()) return;
      if (name === "complete" && active.status !== "SUCCESS") return;
      if (!["complete", "refresh", "cancel"].includes(name)) return;
      const s = scope(), r = revision, id = active.id;
      busy = true; status(name === "complete" ? "Completing verified payment…" : "Contacting payment server…");
      try {
        const data = await request("/intents/" + encodeURIComponent(id) + "/" + name, "POST");
        if (!valid(s, r) || active?.id !== id) return;
        if (name === "complete") {
          if (!data.success || !data.transaction_id) throw new Error("Sale confirmation missing.");
          const clearCart = !!quote && quote.snapshot === env.snapshot();
          active = null; quote = null; revision++; stop();
          env.forget?.();
          env.completed(data, clearCart); status("Sale confirmed. Payment completed.");
        } else {
          active = data.payment;
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
      } finally { if (s === scope()) { busy = false; env.busy(false); schedule(); } }
    }
    return {mode, preview, start, list, resume, action, loadConfig,
      close() { visible = false; stop(); },
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
  function setBusy(value) {
    ["confirm-mmqr-payment-btn", "mmp-refresh", "mmp-cancel", "mmp-resume"].forEach(id => { el(id).disabled = value; });
    if (!value && checkout.state().active && checkout.state().active.status !== "SUCCESS") el("confirm-mmqr-payment-btn").disabled = true;
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
    ready(mode) { el("mmp-open-existing").classList.toggle("d-none", !["provider", "legacy"].includes(mode)); },
    legacy() {
      el("mmp-status").textContent = "";
      ["mmp-refresh", "mmp-cancel"].forEach(id => el(id).classList.add("d-none"));
      el("mmp-resume").classList.remove("d-none");
      el("confirm-mmqr-payment-btn").textContent = "Payment Received — Complete Sale";
      el("confirm-mmqr-payment-btn").disabled = false;
      el("mmqr-payment-help").textContent = "Ask the customer to scan the QR and confirm that payment was received.";
      showMMQRPaymentModal();
    },
    setTimeout: root.setTimeout.bind(root), clearTimeout: root.clearTimeout.bind(root),
    open() { (bootstrap.Modal.getInstance(modal) || new bootstrap.Modal(modal)).show(); },
    busy: setBusy,
    render({mode, payment, busy, text, deadline}) {
      el("mmp-status").textContent = text;
      const provider = mode === "provider";
      const recover = provider || mode === "legacy";
      ["mmp-resume", "mmp-refresh", "mmp-cancel"].forEach(id => el(id).classList.toggle("d-none", !recover || (id !== "mmp-resume" && !payment)));
      const confirm = el("confirm-mmqr-payment-btn");
      confirm.textContent = payment?.status === "SUCCESS" ? "Complete verified sale" : !payment && mode === "legacy" ? "Payment Received — Complete Sale" : "Create payment QR";
      confirm.disabled = busy || !recover || (!!payment && payment.status !== "SUCCESS");
      el("mmp-refresh").disabled = el("mmp-cancel").disabled = el("mmp-resume").disabled = busy;
      el("mmqr-payment-help").textContent = "Only server-verified payment can complete this sale. Closing does not cancel payment. Existing QR always belongs to its frozen order.";
      const image = el("pos-mmqr-image");
      // Never disturb the uploaded legacy QR while rendering an empty provider state.
      if (mode !== "legacy" || payment) {
        image.style.display = "none"; image.removeAttribute("src");
      }
      const missing = el("pos-mmqr-missing");
      missing.style.display = "none";
      // Compliance: only MMK may appear on the payment surface.
      el("pos-mmqr-total").textContent = payment ? formatMMK(payment.amount) : "—";
      const showQr = Boolean(recover && payment?.qr_url && payment.status === "PENDING");
      if (!showQr && (mode !== "legacy" || payment)) {
        missing.textContent = payment?.status === "CREATING"
          ? "No QR has been issued for this order. Refresh provider to recover it, or explicitly cancel the order before trying again."
          : "Choose Create payment QR to request a QR for this checkout.";
        missing.style.display = "block";
      }
      if (deadline) resetMMQRCountdown(deadline, showQr);
      else if (mode !== "legacy" || payment) stopMMQRCountdown();
      if (payment || mode !== "legacy") setMMQRDownloadState(showQr);
      if (showQr) {
        // Prefer the cached copy so a reload (even offline) shows the same QR.
        const inline = readCachedQr(payment.id);
        if (inline) {
          image.src = inline; image.style.display = "block";
        } else {
          const url = BASE + "/intents/" + encodeURIComponent(payment.id) + "/qr";
          const pinned = typeof dashboardIdentityUrl === "function" ? dashboardIdentityUrl(url) : url;
          if (pinned) {
            image.src = pinned; image.style.display = "block";
            cacheQrImage(pinned, payment.id);
          }
        }
      }
    },
    list(payments, resume) {
      el("mmp-existing").replaceChildren();
      payments.forEach(payment => {
        const button = doc.createElement("button");
        button.type = "button"; button.className = "btn btn-outline-primary m-1";
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
      bootstrap.Modal.getInstance(modal)?.hide();
      showSaleSuccessModal(data.transaction_id, data.delivery_number, false);
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