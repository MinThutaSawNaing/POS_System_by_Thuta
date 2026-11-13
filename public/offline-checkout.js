/* Durable checkout journal. No queue rewrites or destructive reconciliation.
 * Each immutable intent/attempt/result has its own key. Even without Web Locks,
 * concurrent tabs cannot overwrite another intent or lose an attempt. Duplicate
 * transport of the SAME immutable intent is resolved by server idempotency.
 */
(function (root) {
  "use strict";
  function create(options) {
    const store = options.storage;
    const prefix = "pos_checkout_v3:" + encodeURIComponent(options.userId) + ":" + encodeURIComponent(options.branchId) + ":";
    const copy = value => JSON.parse(JSON.stringify(value));
    const keys = () => Array.from({length: store.length}, (_, i) => store.key(i)).filter(k => k && k.startsWith(prefix));
    function put(key, value) {
      const raw = JSON.stringify(value);
      if (store.getItem(key) !== null) throw Error("Journal key already exists; nothing overwritten");
      store.setItem(key, raw);
      if (store.getItem(key) !== raw) throw Error("Device did not retain checkout data");
    }
    function list() {
      const all = keys(), entries = [];
      for (const key of all.filter(k => k.includes(":intent:"))) {
        const raw = store.getItem(key);
        try {
          const intent = JSON.parse(raw);
          if (!intent || intent.version !== 3 || !intent.saleData || intent.transaction_id !== intent.saleData.transaction_id ||
              intent.saleData.checkout_context.user_id !== options.userId || intent.saleData.checkout_context.branch_id !== options.branchId ||
              key !== prefix + "intent:" + intent.transaction_id) throw Error("Invalid journal intent");
          const events = [];
          let corrupt = false;
          for (const eventKey of all.filter(k => k.startsWith(prefix + "event:" + intent.transaction_id + ":"))) {
            try {
              const event = JSON.parse(store.getItem(eventKey));
              if (!event || !["attempt", "result"].includes(event.type) || !event.attempt_id ||
                  typeof event.at !== "string" || !Number.isFinite(Date.parse(event.at)) ||
                  eventKey !== prefix + "event:" + intent.transaction_id + ":" + event.attempt_id +
                    (event.type === "attempt" ? ":start" : ":result") ||
                  (event.type === "result" && (!["synced", "conflict", "auth", "rejected", "unknown"].includes(event.status) ||
                    (event.status === "synced" && event.transaction_id !== intent.transaction_id)))) throw Error("Invalid attempt");
              events.push(event);
            } catch (_) { corrupt = true; }
          }
          events.sort((a,b) => a.at.localeCompare(b.at));
          const results = events.filter(e => e.type === "result");
          const attempts = events.filter(e => e.type === "attempt");
          if (results.some(result => !attempts.some(start => start.attempt_id === result.attempt_id))) corrupt = true;
          const unfinished = attempts.some(start => !results.some(result => result.attempt_id === start.attempt_id));
          const success = results.find(e => e.status === "synced");
          const last = results[results.length - 1];
          entries.push({...intent, events, attempts: events.filter(e => e.type === "attempt").length,
            status: corrupt ? "corrupt" : success ? "synced" : unfinished ? "unknown" : last ? last.status : "pending",
            error: corrupt ? "Corrupt attempt data retained; export for reconciliation" : last?.error || (events.length && !success ? "Response not recorded; outcome unknown" : ""),
            server_transaction_id: success?.transaction_id});
        } catch (_) { entries.push({transaction_id: key.slice((prefix + "intent:").length), status: "corrupt", error: "Corrupt checkout retained; export for reconciliation", raw, events: [], attempts: 0}); }
      }
      // Losing an intent is not the same as an empty queue. Preserve and expose
      // orphaned attempt keys for export instead of silently hiding the evidence.
      for (const key of all.filter(k => k.startsWith(prefix + "event:"))) {
        if (!entries.some(entry => key.startsWith(prefix + "event:" + entry.transaction_id + ":"))) {
          entries.push({transaction_id: key.slice((prefix + "event:").length), status: "corrupt",
            error: "Orphaned attempt retained; export for reconciliation", events: [], attempts: 0});
        }
      }
      return entries;
    }
    function prepare(payload, receipt, offline) {
      if (!options.current()) throw Error("Session or branch changed; reload before checkout");
      if (offline && (payload.payment_method !== "cash" || payload.payment_breakdown || payload.coupon_code || payload.delivery?.enabled)) {
        throw Error("New offline sales are cash only, without coupons or delivery");
      }
      const sale = copy(payload);
      sale.transaction_id = sale.transaction_id || options.id();
      const deviceKey = "pos_checkout_device_v3";
      let device = store.getItem(deviceKey);
      if (!device) { device = options.id(); store.setItem(deviceKey, device); }
      if (store.getItem(deviceKey) !== device) throw Error("Cannot persist device identity");
      sale.checkout_context = {branch_id: options.branchId, user_id: options.userId, device_id: device,
        occurred_at: new Date().toISOString(), offline: Boolean(offline), expected_total: Number(receipt.total)};
      if (!Number.isFinite(sale.checkout_context.expected_total)) throw Error("Invalid checkout total");
      const snapshot = copy(receipt);
      snapshot.transactionId = sale.transaction_id;
      snapshot.checkoutOffline = Boolean(offline);
      const intent = {version: 3, transaction_id: sale.transaction_id, saleData: sale, receiptSnapshot: snapshot, created_at: sale.checkout_context.occurred_at};
      put(prefix + "intent:" + sale.transaction_id, intent);
      return copy(intent);
    }
    async function send(id, manual, initialOnline, reviewer, automatic) {
      const run = async () => {
        if (!options.current()) throw Error("Session or branch changed; reload before retry");
        if (manual && (typeof reviewer !== "string" || !reviewer.trim())) throw Error("Record the reviewing manager before retry");
        const entry = list().find(e => e.transaction_id === id);
        if (!entry || entry.status === "corrupt") throw Error("Checkout unavailable or corrupt; export for reconciliation");
        if (entry.status === "synced") return {success: true, transaction_id: entry.server_transaction_id, replay: true};
        if (automatic) {
          // Never change the original intent. The server's atomic acceptance
          // ledger makes an exact replay safe even after a lost response/crash.
          // Definite business/permission failures always require review;
          // expired sessions may recover in the same authenticated scope.
          if (entry.events.some(e => e.type === "result" && (["conflict", "rejected"].includes(e.status) || (e.status === "auth" && e.http_status !== 401)))) return null;
          const lastAt = Math.max(0, ...entry.events.map(e => Date.parse(e.at)));
          const delay = Math.min(300000, 60000 * 2 ** Math.min(3, Math.max(0, entry.attempts - 1)));
          if (entry.attempts && Date.now() - lastAt < delay) return null;
          if (!["pending", "unknown", "auth"].includes(entry.status)) return null;
        } else if (!manual && (entry.status !== "pending" || (!initialOnline && !entry.saleData.checkout_context.offline))) return null;
        const attempt = options.id(), base = prefix + "event:" + id + ":" + attempt;
        put(base + ":start", {type: "attempt", attempt_id: attempt, at: new Date().toISOString(), manual: Boolean(manual),
          manager_review: manual ? reviewer.trim() : null, review_attested_by: manual ? options.userId : null});
        let result, data;
        const controller = new AbortController();
        const timeoutMs = options.timeoutMs ?? 30000;
        let timer;
        try {
          const transport = async () => {
            const response = await options.fetch("/api/sales", {method: "POST", headers: {"Content-Type": "application/json"},
              signal: controller.signal, body: JSON.stringify(entry.saleData)});
            if (!(response.headers.get("content-type") || "").toLowerCase().includes("application/json")) throw Error("Non-JSON/login response; outcome unknown");
            data = await response.json();
            if (!data || typeof data !== "object" || Array.isArray(data)) throw Error("Invalid response; outcome unknown");
            if (response.ok && (data.success !== true || data.transaction_id !== entry.transaction_id)) throw Error("Unconfirmed transaction response; outcome unknown");
            return response;
          };
          const response = await Promise.race([transport(), new Promise((_, reject) => {
            timer = setTimeout(() => { controller.abort(); reject(Error("Checkout timed out; saved for automatic recovery with the original ID. Do not collect payment again.")); }, timeoutMs);
          })]);
          const success = response.ok && data.success === true && data.transaction_id === entry.transaction_id;
          result = {status: success ? "synced" : response.status >= 500 || [408, 429].includes(response.status) ? "unknown" : response.status === 409 ? "conflict" : response.status === 401 || response.status === 403 ? "auth" : "rejected",
            http_status: response.status, code: data.code || null, error: success ? "" : String(data.message || "Checkout rejected; manager review required"), transaction_id: success ? data.transaction_id : null};
        } catch (error) { result = {status: "unknown", error: String(error.message || error)}; }
        finally { clearTimeout(timer); }
        // Always journal the response under the ORIGINAL scope, never the newly
        // selected branch. A stale screen must not render success into its cart.
        put(base + ":result", {type: "result", attempt_id: attempt, at: new Date().toISOString(), ...result});
        return result.status === "synced" ? data : {success: false, ...result};
      };
      if (options.locks?.request) return options.locks.request(prefix + id, run);
      return run();
    }
    return {prepare, list, send, recover: id => send(id, false, false, null, true), exportData: () => ({version: 3, user_id: options.userId, branch_id: options.branchId,
      records: keys().map(key => ({key, raw: store.getItem(key)}))})};
  }
  root.OfflineCheckout = {create};
})(typeof window === "undefined" ? globalThis : window);