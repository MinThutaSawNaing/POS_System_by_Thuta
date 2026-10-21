/* Server-authoritative checkout. No payment state or sale payload is trusted from storage. */
(function (root) {
  "use strict";
  const BASE = "/api/payments/myanmyanpay";
  function createCheckout(env) {
    let config = null, configScope = null, active = null, quote = null;
    let busy = false, visible = false, timer = null, revision = 0, reconciliation = false;
    let terminalNotice = "";
    const scope = () => env.scope();
    const valid = (s, r) => s !== null && s === scope() && (r === undefined || r === revision);
    const mode = () => configScope !== scope() || !config ? "loading" : config.enabled === false ? "legacy" : config.enabled === true && config.configured === true ? "provider" : "blocked";
    const canRecover = () => ["provider", "legacy"].includes(mode());
    const status = (text) => env.render({mode: mode(), payment: active, busy, text: terminalNotice + text});
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
        stop();
      }
      const changed = quote && quote.snapshot !== env.snapshot();
      status(active ? `${reconciliation ? "Payment received; sale needs reconciliation, do not pay again. " : ""}${active.status} · Order ${active.order_id || active.id}${changed ? " — Cart changed: this QR pays ONLY the original frozen order, not the current cart." : ""}` : "Ready. Validate checkout to create a payment QR, or find pending payments.");
      schedule();
    }
    async function loadConfig() {
      const s = scope();
      if (s === null) return;
      if (configScope !== s) { active = null; quote = null; busy = false; reconciliation = false; terminalNotice = ""; revision++; stop(); }
      config = null; configScope = s;
      try {
        const data = await request("/config");
        if (!valid(s)) return;
        config = data;
        env.ready?.(mode());
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
        display();
      } catch (error) {
        if (valid(s, r)) {
          if (error.payment) active = error.payment;
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
          env.completed(data, clearCart); status("Sale confirmed. Payment completed.");
        } else {
          active = data.payment;
          display();
        }
      } catch (error) {
        if (valid(s, r)) {
          if (error.payment && error.payment.id === id) active = error.payment;
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
  const snapshot = () => JSON.stringify({cart, fields: ["payment-method", "pos-customer-select", "split-method-one", "split-method-two", "split-amount-one", "split-amount-two", "pos-enable-delivery", "pos-delivery-recipient-name", "pos-delivery-recipient-phone", "pos-delivery-address", "pos-delivery-priority", "pos-delivery-fee"].map(id => [id, el(id)?.value, el(id)?.checked])});
  function setBusy(value) {
    ["confirm-mmqr-payment-btn", "mmp-refresh", "mmp-cancel", "mmp-resume"].forEach(id => { el(id).disabled = value; });
    if (!value && checkout.state().active && checkout.state().active.status !== "SUCCESS") el("confirm-mmqr-payment-btn").disabled = true;
  }
  const checkout = createCheckout({
    fetch: (...args) => root.fetch(...args), online: () => navigator.onLine !== false,
    scope: () => typeof currentBranch === "undefined" || !currentBranch?.id ||
      (typeof dashboardSessionStale !== "undefined" && dashboardSessionStale) ||
      (typeof dashboardBranchStale !== "undefined" && dashboardBranchStale) ||
      (typeof branchSwitchInFlight !== "undefined" && branchSwitchInFlight) ? null :
      JSON.stringify([typeof CACHE_USER_ID === "undefined" ? null : CACHE_USER_ID, currentBranch.id]),
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
    render({mode, payment, busy, text}) {
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
      image.style.display = "none"; image.removeAttribute("src");
      el("pos-mmqr-missing").style.display = "none";
      el("pos-mmqr-total").textContent = payment ? `${payment.amount} ${payment.currency || "MMK"}` : "—";
      if (recover && payment?.qr_url && payment.status === "PENDING") {
        const url = BASE + "/intents/" + encodeURIComponent(payment.id) + "/qr";
        const pinned = typeof dashboardIdentityUrl === "function" ? dashboardIdentityUrl(url) : url;
        if (pinned) { image.src = pinned; image.style.display = "inline-block"; }
      }
    },
    list(payments, resume) {
      el("mmp-existing").replaceChildren();
      payments.forEach(payment => {
        const button = doc.createElement("button");
        button.type = "button"; button.className = "btn btn-outline-primary m-1";
        button.textContent = `Resume ${payment.order_id || payment.id}: ${payment.amount} ${payment.currency || "MMK"} — ${payment.status}`;
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
  checkout.loadConfig();
})(typeof window === "undefined" ? globalThis : window);