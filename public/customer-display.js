/* Customer-facing display: read-only renderer.
 *
 * This page never calls the server. Every value it shows arrives from the
 * cashier tab in the same browser (BroadcastChannel, with a localStorage
 * fallback). The cashier tab stays the only writer, so this screen cannot
 * record a sale, create a payment or leak another branch's data.
 */
(function (root) {
  "use strict";
  const CHANNEL = "pos-customer-display";
  const STORAGE_KEY = "pos_customer_display_message";
  const STALE_MS = 10000;      // no cashier message for this long => disconnected
  const TICK_MS = 1000;
  const ALIVE_EVERY = 3;       // send display-alive every third tick
  const ADOPT_QUIET_MS = 3000;

  const doc = root.document;
  if (!doc) return;
  const el = (id) => doc.getElementById(id);

  let transport = null;
  let publisherId = null;
  let lastSeen = 0;
  let revision = -1;
  let state = null;

  const now = () => Date.now();
  const idle = () => ({ kind: "idle" });

  // ---------------------------------------------------------------- transport
  function createTransport(onMessage) {
    if (typeof root.BroadcastChannel === "function") {
      const channel = new root.BroadcastChannel(CHANNEL);
      channel.onmessage = (event) => onMessage(event.data);
      return {
        post: (message) => { try { channel.postMessage(message); } catch (error) { /* ignore */ } },
        close: () => { try { channel.close(); } catch (error) { /* ignore */ } },
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
      close: () => root.removeEventListener("storage", listener),
    };
  }

  function send(type, extra) {
    if (!transport) return;
    transport.post(Object.assign({ type, at: now(), revision, from: "display" }, extra || {}));
  }

  // ------------------------------------------------------------------ messages
  function handleMessage(message) {
    if (!message || typeof message !== "object" || message.from === "display") return;
    const type = message.type;
    if (type !== "snapshot" && type !== "heartbeat" && type !== "reset") return;
    if (!message.publisherId) return;

    // Two cashier tabs must not make the screen flicker. Adopt a new publisher
    // only when the previous one is silent, or when it answers with a snapshot.
    if (publisherId && message.publisherId !== publisherId) {
      const quiet = now() - lastSeen > ADOPT_QUIET_MS;
      if (!(quiet || type === "snapshot")) return;
    }

    publisherId = message.publisherId;
    lastSeen = now();

    if (type === "reset") {
      revision = typeof message.revision === "number" ? message.revision : -1;
      setState(idle());
    } else if (type === "snapshot" && message.state) {
      revision = typeof message.revision === "number" ? message.revision : revision;
      setState(message.state);
    } else if (type === "heartbeat"
      && typeof message.revision === "number" && message.revision !== revision) {
      askForState();
    }
    hideBanner();
    renderStatus();
  }

  function askForState() { send("hello", { want: revision }); }

  // ---------------------------------------------------------------------- view
  function setState(next) {
    state = next && typeof next === "object" ? next : idle();
    render();
  }

  const value = (input) => (typeof input === "string" ? input : "");

  function showBanner(show) {
    const banner = el("cd-banner");
    if (banner) banner.hidden = !show;
    const shell = el("cd-shell");
    if (shell) shell.classList.toggle("is-disconnected", !!show);
  }

  function hideBanner() { showBanner(false); }

  function renderStatus() {
    const dot = el("cd-connection");
    if (!dot) return;
    const live = publisherId !== null && now() - lastSeen < STALE_MS;
    dot.classList.toggle("is-live", live);
  }

  function renderItems(items) {
    const list = el("cd-items");
    if (!list) return;
    list.replaceChildren();
    (Array.isArray(items) ? items : []).forEach((item) => {
      const row = doc.createElement("li");
      row.className = "cd-item";

      const name = doc.createElement("span");
      name.className = "cd-item-name";
      const label = doc.createElement("strong");
      label.textContent = String(item.name || "");
      name.appendChild(label);

      const qty = doc.createElement("span");
      qty.className = "cd-item-qty";
      qty.textContent = `${item.qty || 0}${item.unit ? " " + item.unit : ""} × ${value(item.unitPrice)}`;
      name.appendChild(qty);

      const total = doc.createElement("span");
      total.className = "cd-item-total";
      total.textContent = value(item.lineTotal);

      row.append(name, total);
      list.appendChild(row);
    });
  }

  function renderTotals(next) {
    if (el("cd-subtotal")) el("cd-subtotal").textContent = value(next.subtotal);
    if (el("cd-tax")) el("cd-tax").textContent = value(next.tax);
    const deliveryRow = el("cd-delivery-row");
    if (deliveryRow) deliveryRow.hidden = !next.deliveryFee;
    if (next.deliveryFee && el("cd-delivery")) el("cd-delivery").textContent = value(next.deliveryFee);
    if (el("cd-total")) el("cd-total").textContent = value(next.total);
    if (el("cd-count")) {
      const count = Number(next.count) || 0;
      el("cd-count").textContent = count === 1 ? "1 item" : `${count} items`;
    }
  }

  const expiredNow = (payment) =>
    !!payment && payment.deadlineAt ? now() >= Number(payment.deadlineAt) : false;

  function renderCountdown(payment, expired) {
    const box = el("cd-timer");
    if (!box) return;
    if (!payment || !payment.deadlineAt) { box.hidden = true; box.classList.remove("is-expired"); return; }
    box.hidden = false;
    if (expired) { box.textContent = "QR expired"; box.classList.add("is-expired"); return; }
    const left = Math.max(0, Number(payment.deadlineAt) - now());
    const minutes = Math.floor(left / 60000);
    const seconds = Math.floor((left % 60000) / 1000);
    box.textContent = `${minutes}:${String(seconds).padStart(2, "0")}`;
    box.classList.remove("is-expired");
  }

  function renderPayment(next) {
    const payment = next.payment || null;
    const expired = expiredNow(payment);
    if (el("cd-qr-amount")) el("cd-qr-amount").textContent = value(payment && payment.amount);
    if (el("cd-qr-order")) {
      el("cd-qr-order").textContent = payment && payment.orderId ? `Order ${payment.orderId}` : "";
    }
    const image = el("cd-qr-image");
    if (image) {
      const source = payment && payment.qrSrc ? String(payment.qrSrc) : "";
      if (source && image.getAttribute("src") !== source) image.src = source;
      if (!source) image.removeAttribute("src");
      image.hidden = !source || expired;
    }
    if (el("cd-qr-expired")) el("cd-qr-expired").hidden = !expired;
    if (el("cd-qr-note")) el("cd-qr-note").textContent = payment && payment.note ? String(payment.note) : "";
    renderCountdown(payment, expired);
  }

  function render() {
    const next = state || idle();
    const showSale = next.kind === "cart" || next.kind === "qr";
    const showQr = next.kind === "qr" && !!next.payment;
    const showPaid = next.kind === "paid";

    if (el("cd-idle")) el("cd-idle").hidden = next.kind !== "idle";
    if (el("cd-sale")) el("cd-sale").hidden = !showSale;
    if (el("cd-qr-panel")) el("cd-qr-panel").hidden = !showQr;
    if (el("cd-paid")) el("cd-paid").hidden = !showPaid;

    if (showSale) {
      renderItems(next.items);
      renderTotals(next);
    }
    if (showQr) renderPayment(next);

    if (showPaid) {
      if (el("cd-paid-total")) el("cd-paid-total").textContent = value(next.total);
      const changeRow = el("cd-paid-change-row");
      if (changeRow) changeRow.hidden = !next.change;
      if (next.change && el("cd-paid-change")) el("cd-paid-change").textContent = value(next.change);
    }

    if (next.brand && el("cd-shop-name")) el("cd-shop-name").textContent = String(next.brand);
    renderStatus();
  }

  // ----------------------------------------------------------------- liveness
  function checkLiveness() {
    const connected = publisherId !== null && now() - lastSeen < STALE_MS;
    showBanner(!connected && state !== null);
    // Keep the QR timer honest without waiting for the next cashier message.
    if (state && state.kind === "qr") renderPayment(state);
  }

  function start() {
    transport = createTransport(handleMessage);
    askForState();
    let ticks = 0;
    root.setInterval(() => {
      checkLiveness();
      ticks += 1;
      if (ticks % ALIVE_EVERY === 0) send("display-alive");
    }, TICK_MS);
    root.addEventListener("beforeunload", () => { if (transport) transport.close(); }, { once: true });
    setState(idle());
    // A screen opened after the cashier already started must recover at once.
    root.setTimeout(askForState, 400);
  }

  root.posCustomerDisplay = { render, state: () => state };
  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", start, { once: true });
  else start();
})(typeof window === "undefined" ? globalThis : window);
