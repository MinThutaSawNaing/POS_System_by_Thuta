/* Cashier-side publisher for the customer-facing display.
 *
 * The cashier tab is the only writer. It sends a small, already-formatted
 * snapshot; the display never recomputes money. Everything here is best-effort:
 * if the display is closed, nothing breaks.
 */
(function (root) {
  "use strict";
  const CHANNEL = "pos-customer-display";
  const STORAGE_KEY = "pos_customer_display_message";
  const WINDOW_NAME = "pos-customer-display";
  const WATCH_MS = 1000;          // how often we check the display window
  const DISPLAY_STALE_MS = 8000;
  const PAID_HOLD_MS = 10000;

  const doc = root.document;
  if (!doc) return;

  const publisherId = (() => {
    try { return root.crypto.randomUUID(); } catch (error) { return "pub-" + Math.random().toString(36).slice(2); }
  })();

  let transport = null;
  let revision = 0;
  let lastDisplaySeen = 0;
  let current = { kind: "idle" };
  let frozen = null;          // active payment: the sale on screen is frozen
  let lastTotalCents = 0;
  let lastTotalText = "";
  let paidTimer = null;
  let paidState = null;       // held thank-you screen
  let paidUntil = 0;
  let lastStatus = null;      // previous display connection state
  let displayWindow = null;   // handle from window.open, when we opened it

  // ---------------------------------------------------------------- formatting
  function money(value) {
    try {
      return typeof formatCurrency === "function" ? formatCurrency(value) : String(value);
    } catch (error) { return String(value); }
  }

  function text(nodeId) {
    const node = doc.getElementById(nodeId);
    return node ? node.textContent.trim() : "";
  }

  // --------------------------------------------------------------- cart reading
  function cartItems() {
    if (typeof cart === "undefined" || !Array.isArray(cart)) return [];
    return cart.map((item) => {
      const price = Number(item.price) || 0;
      const qty = Number(item.quantity) || 0;
      return {
        name: String(item.name || ""),
        qty,
        unit: item.unit ? String(item.unit) : "",
        unitPrice: money(price),
        lineTotal: money(Math.round(price * 100) * qty / 100),
      };
    });
  }

  function totals() {
    const deliveryRow = doc.getElementById("pos-delivery-fee-row");
    const deliveryShown = !!deliveryRow && deliveryRow.style.display !== "none";
    return {
      subtotal: text("subtotal"),
      tax: text("tax"),
      deliveryFee: deliveryShown ? text("pos-delivery-fee-total") : null,
      total: text("total"),
    };
  }

  function cartState() {
    const items = cartItems();
    if (!items.length) return { kind: "idle" };
    return Object.assign({ kind: "cart" }, totals(), {
      items,
      count: items.reduce((sum, item) => sum + item.qty, 0),
    });
  }

  function rememberTotal() {
    const shown = totals().total;
    if (shown) lastTotalText = shown;
    try {
      if (typeof getCurrentCartTotal === "function") {
        const cents = Math.round(Number(getCurrentCartTotal()) * 100);
        if (Number.isFinite(cents)) lastTotalCents = cents;
      }
    } catch (error) { /* leave the previous total */ }
  }

  // ----------------------------------------------------------------- publishing
  function post(type, extra) {
    if (!transport) return;
    transport.post(Object.assign({ type, publisherId, revision, at: Date.now() }, extra || {}));
  }

  function publish(next) {
    current = next;
    revision += 1;
    post("snapshot", { state: current });
  }

  function publishCart() {
    // The thank-you screen owns the display for a few seconds after a sale.
    if (paidState && Date.now() < paidUntil) { publish(paidState); return; }
    // While a payment QR is live the screen keeps the frozen sale, because that
    // is exactly what the QR pays for. Live cart edits wait for this order.
    if (frozen) { publish(frozen); return; }
    rememberTotal();
    const next = cartState();
    if (next.kind === "cart") lastTotalText = next.total;
    publish(next);
  }

  function publishPayment(info) {
    const payment = info && info.payment;
    const legacy = !!info && info.mode === "legacy" && !!info.qrSrc;
    // Reuse the amount already rendered on the payment surface so both screens
    // can never disagree about the currency or the formatting.
    const amountShown = text("pos-mmqr-total");
    const amount = payment
      ? (amountShown && amountShown !== "—" ? amountShown : money(payment.amount))
      : (lastTotalText || text("total"));

    // The customer has already paid: say so immediately, even before the sale
    // is recorded. Keeping the QR up here is what people read as "failed".
    if (payment && payment.status === "SUCCESS") {
      const reconciling = String(info.text || "").toLowerCase().includes("reconciliation");
      paidState = {
        kind: "paid", total: amount, change: paidState ? paidState.change : "",
        note: reconciling ? "Payment received — please speak to the cashier." : "",
      };
      paidUntil = Date.now() + PAID_HOLD_MS;
      publish(paidState);
      return;
    }
    // A new pending order means a new sale: release the previous thank-you.
    if (payment) { paidState = null; paidUntil = 0; }
    if (!payment && !legacy) {
      frozen = null;
      publishCart();
      return;
    }
    const base = cartState();
    if (base.kind === "cart" && base.total) lastTotalText = base.total;
    frozen = Object.assign({}, base, {
      kind: "qr",
      payment: {
        amount,
        orderId: payment ? String(payment.order_id || payment.id || "") : "",
        qrSrc: info.qrSrc || null,
        deadlineAt: info.deadline || null,
        note: "",
      },
    });
    publish(frozen);
  }

  function paid() {
    const input = doc.getElementById("cash-received");
    const raw = input ? String(input.value || "").replace(/[^0-9.]/g, "") : "";
    const cashCents = raw ? Math.round(Number(raw) * 100) : 0;
    const change = cashCents > 0 && lastTotalCents > 0 && cashCents > lastTotalCents
      ? money((cashCents - lastTotalCents) / 100) : "";
    const total = lastTotalText || text("total");
    frozen = null;
    paidState = { kind: "paid", total, change };
    paidUntil = Date.now() + PAID_HOLD_MS;
    publish(paidState);
    root.clearTimeout(paidTimer);
    paidTimer = root.setTimeout(() => {
      paidState = null;
      paidUntil = 0;
      publish(cartState());
    }, PAID_HOLD_MS);
  }

  function reset() {
    frozen = null;
    paidState = null;
    paidUntil = 0;
    root.clearTimeout(paidTimer);
    current = { kind: "idle" };
    revision += 1;
    post("reset", { state: current });
  }

  // ------------------------------------------------------------------ transport
  function createTransport(onMessage) {
    if (typeof root.BroadcastChannel === "function") {
      const channel = new root.BroadcastChannel(CHANNEL);
      channel.onmessage = (event) => onMessage(event.data);
      return {
        post: (message) => { try { channel.postMessage(message); } catch (error) { /* ignore */ } },
      };
    }
    const listener = (event) => {
      if (event.key !== STORAGE_KEY || !event.newValue) return;
      try { onMessage(JSON.parse(event.newValue)); } catch (error) { /* ignore */ }
    };
    root.addEventListener("storage", listener);
    return {
      post: (message) => {
        try { root.localStorage.setItem(STORAGE_KEY, JSON.stringify(message)); } catch (error) { /* ignore */ }
      },
    };
  }

  function isConnected() {
    return lastDisplaySeen > 0 && Date.now() - lastDisplaySeen < DISPLAY_STALE_MS;
  }

  // The window handle is the only *exact* signal: it flips the moment the popup
  // is closed, with no message and no timeout involved. If the cashier opened the
  // screen some other way (launcher, manual URL) there is no handle and the
  // heartbeat timeout below is the fallback.
  function displayGone() {
    if (!displayWindow) return false;
    let closed = false;
    try { closed = displayWindow.closed === true; } catch (error) { closed = false; }
    if (!closed) return false;
    displayWindow = null;
    return true;
  }

  function refreshStatus() {
    if (displayGone()) lastDisplaySeen = 0;
    paintStatus();
  }

  function paintStatus() {
    const dot = doc.getElementById("customer-screen-dot");
    const live = isConnected();
    // Tell the cashier loudly when the customer screen is gone: a silently
    // closed screen is the one thing cashiers never notice.
    if (lastStatus === true && !live) {
      try {
        if (typeof showToast === "function") {
          showToast("Customer screen closed. Click Customer Screen to reopen it.", "warning");
        }
      } catch (error) { /* toast is optional */ }
    }
    lastStatus = live;
    if (!dot) return;
    dot.classList.toggle("is-live", live);
    dot.classList.toggle("is-closed", !live);
    dot.setAttribute("title", live
      ? "Customer screen connected"
      : "Customer screen not connected — click to open");
  }

  function handle(message) {
    if (!message || message.from !== "display") return;
    if (message.type === "display-alive" || message.type === "hello") {
      lastDisplaySeen = Date.now();
      paintStatus();
      if (message.type === "hello") post("snapshot", { state: current });
      return;
    }
    // Closed on purpose (or navigated away): no need to wait for the timeout.
    if (message.type === "display-closing") {
      lastDisplaySeen = 0;
      displayWindow = null;
      paintStatus();
    }
  }

  // ----------------------------------------------------------------------- open
  function open(options) {
    const opts = options || {};
    const width = Number(opts.width) || 1024;
    const height = Number(opts.height) || 768;
    // Default: right-hand monitor, which is how most terminals are arranged.
    const left = Number.isFinite(opts.left) ? opts.left : (root.screen ? root.screen.availWidth : width);
    const top = Number.isFinite(opts.top) ? opts.top : 0;
    const win = root.open("/display", WINDOW_NAME,
      `popup=yes,width=${width},height=${height},left=${left},top=${top}`);
    if (win) {
      // Keep the handle: `win.closed` is the exact, instant close signal.
      displayWindow = win;
      try { win.focus(); } catch (error) { /* ignore */ }
    }
    return win;
  }

  function start() {
    if (transport) return;
    transport = createTransport(handle);
    paintStatus();
    let ticks = 0;
    root.setInterval(() => {
      ticks += 1;
      if (ticks % 3 === 0) post("heartbeat");
      refreshStatus();
    }, WATCH_MS);
    // Browsers throttle timers in an unfocused window, so re-check the moment
    // the cashier touches this window again. That is exactly when they look at
    // the indicator, so a closed screen turns red immediately.
    ["visibilitychange", "focus", "pointerdown", "keydown"].forEach((type) => {
      try { doc.addEventListener(type, refreshStatus, { passive: true }); } catch (error) { /* ignore */ }
    });
    try { root.addEventListener("focus", refreshStatus); } catch (error) { /* ignore */ }
  }

  root.CustomerDisplay = {
    open, publishCart, publishPayment, paid, reset, start,
    isConnected, status: isConnected, WINDOW_NAME,
  };
  start();
})(typeof window === "undefined" ? globalThis : window);
