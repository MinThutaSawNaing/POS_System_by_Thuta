/* Optional email two-step login. No credentials or codes enter browser storage. */
(function (root) {
  "use strict";
  const el = (id) => document.getElementById(id);
  let generation = 0;
  let state = null;
  let resendTimer = null;
  let settingsLoaded = false;
  let settingsBusy = false;
  let settingsDirty = false;
  let settingsToken = "";

  function message(id, text, error = false) {
    const box = el(id);
    box.textContent = text;
    box.className = `alert ${error ? "alert-danger" : "alert-light border"}`;
  }

  async function api(path, data, token) {
    const options = {cache: "no-store", credentials: "same-origin"};
    if (data !== undefined) {
      options.method = path === "/api/settings/email" ? "PUT" : "POST";
      options.headers = {"Content-Type": "application/json", "X-CSRF-Token": token};
      options.body = JSON.stringify(data);
    }
    const response = await root.fetch(path, options);
    let body;
    try { body = await response.json(); }
    catch (_) { throw new Error("Unable to reach the server. Check your connection and try again."); }
    if (!response.ok || body.success === false) {
      const error = new Error(body.message || body.error || "Unable to complete this request.");
      error.retryAfter = Number(body.resend_after || body.retry_after || response.headers?.get("Retry-After")) || 0;
      throw error;
    }
    return body;
  }

  function reset() {
    generation += 1;
    state = null;
    clearInterval(resendTimer);
    resendTimer = null;
    for (const id of ["email-security-address", "email-security-password", "email-security-code"]) el(id).value = "";
    el("email-security-start-form").hidden = true;
    el("email-security-verify-form").hidden = true;
    el("email-security-send").textContent = "Send verification code";
    el("email-security-resend").textContent = "Resend code";
    for (const id of ["email-security-send", "email-security-confirm", "email-security-resend"]) el(id).disabled = false;
    if (typeof markBranchFormSaved === "function") markBranchFormSaved("emailSecurityModal");
  }

  function cooldown(seconds) {
    clearInterval(resendTimer);
    const until = Date.now() + Math.max(0, Number(seconds) || 0) * 1000;
    const tick = () => {
      const left = Math.max(0, Math.ceil((until - Date.now()) / 1000));
      el("email-security-resend").disabled = left > 0 || Boolean(state?.busy);
      el("email-security-resend").textContent = left ? `Resend in ${left}s` : "Resend code";
      if (!left) { clearInterval(resendTimer); resendTimer = null; }
    };
    tick();
    if (until > Date.now()) resendTimer = setInterval(tick, 1000);
  }

  async function open(userId) {
    reset();
    const mine = el("emailSecurityModal").dataset.currentUser;
    const id = userId == null ? mine : String(userId);
    const stamp = generation;
    message("email-security-status", "Loading login security…");
    bootstrap.Modal.getOrCreateInstance(el("emailSecurityModal")).show();
    try {
      // This endpoint is also cashier-accessible and supplies a session CSRF token.
      const own = await api("/api/auth/email");
      const user = id === mine ? own : await api(`/api/users/${encodeURIComponent(id)}`);
      if (stamp !== generation) return;
      state = {id, action: user.email ? "unlink" : "link", csrf: own.csrf_token, busy: false};
      el("email-security-title").textContent = `${user.email ? "Unlink email" : "Add email"} — ${user.username}`;
      const unlink = state.action === "unlink";
      if (!own.configured) {
        message("email-security-status", "Email delivery is not configured. Ask a manager to configure Resend in Settings → Email before continuing.", true);
        return;
      }
      message("email-security-status", unlink
        ? `Two-step login is on for ${user.email}. Unlinking disables it. Confirm the account password, then enter the code sent to this inbox.`
        : "Add an email to turn on two-step login. The account stays password-only until you verify the email. Future sign-ins will require a code from this inbox.");
      el("email-security-address-group").hidden = unlink;
      el("email-security-address").required = !unlink;
      el("email-security-start-form").hidden = false;
      el(unlink ? "email-security-password" : "email-security-address").focus();
    } catch (error) {
      if (stamp === generation) message("email-security-status", error.message, true);
    }
  }

  async function start(event) {
    event.preventDefault();
    if (!state || state.busy || !el("email-security-start-form").reportValidity()) return;
    const active = state;
    active.busy = true;
    el("email-security-send").disabled = true;
    el("email-security-send").textContent = "Sending code…";
    try {
      const result = await api(`/api/users/${encodeURIComponent(active.id)}/email/start`, {
        email: el("email-security-address").value.trim(),
        password: el("email-security-password").value,
        action: active.action,
      }, active.csrf);
      if (state !== active) return;
      active.challenge = result.challenge_id;
      el("email-security-password").value = "";
      el("email-security-start-form").hidden = true;
      el("email-security-verify-form").hidden = false;
      el("email-security-confirm").textContent = active.action === "unlink" ? "Verify and unlink" : "Verify and enable";
      message("email-security-status", `Code sent to ${result.masked_email}. ${active.action === "unlink" ? "Two-step login stays on until you confirm." : "Enter the code to enable two-step login."}`);
      active.busy = false;
      cooldown(result.resend_after);
      el("email-security-code").focus();
    } catch (error) {
      if (state === active) message("email-security-status", error.message, true);
    } finally {
      if (state === active) {
        active.busy = false;
        el("email-security-send").disabled = false;
        el("email-security-send").textContent = "Send verification code";
      }
    }
  }

  async function verify(event) {
    event.preventDefault();
    if (!state?.challenge || state.busy || !el("email-security-verify-form").reportValidity()) return;
    const active = state;
    active.busy = true;
    el("email-security-confirm").disabled = true;
    el("email-security-resend").disabled = true;
    try {
      const result = await api(`/api/users/${encodeURIComponent(active.id)}/email/verify`, {
        challenge_id: active.challenge, code: el("email-security-code").value.trim(),
      }, active.csrf);
      if (state !== active) return;
      const unlink = active.action === "unlink";
      reset();
      message("email-security-status", result.message || (unlink ? "Email unlinked. Future sign-ins use your password only." : "Email verified. Two-step login is now on."));
      if (typeof invalidateCacheGroups === "function") invalidateCacheGroups(["users"], {resetRenderedSections: false});
      if (typeof loadUsers === "function" && !el("users-section").hidden && el("users-section").style.display !== "none") loadUsers();
    } catch (error) {
      if (state === active) {
        message("email-security-status", error.message, true);
        el("email-security-code").value = "";
        el("email-security-code").focus();
      }
    } finally {
      if (state === active) {
        active.busy = false;
        el("email-security-confirm").disabled = false;
        if (!resendTimer) cooldown(0);
      }
    }
  }

  async function resend() {
    if (!state?.challenge || state.busy || el("email-security-resend").disabled) return;
    const active = state;
    active.busy = true;
    el("email-security-resend").disabled = true;
    el("email-security-confirm").disabled = true;
    try {
      const result = await api(`/api/users/${encodeURIComponent(active.id)}/email/resend`, {challenge_id: active.challenge}, active.csrf);
      if (state !== active) return;
      active.challenge = result.challenge_id || active.challenge;
      el("email-security-code").value = "";
      message("email-security-status", "A new code was sent. Use the newest email; the previous code no longer works.");
      active.busy = false;
      cooldown(result.resend_after);
    } catch (error) {
      if (state === active) {
        message("email-security-status", error.message, true);
        active.busy = false;
        cooldown(error.retryAfter || 60);
      }
    } finally {
      if (state === active) { active.busy = false; el("email-security-confirm").disabled = false; }
    }
  }

  async function loadSettings(force = false) {
    if (settingsBusy || (!force && (settingsLoaded || settingsDirty))) return;
    if (force && settingsDirty && !root.confirm("Discard unsaved email settings and reload the saved values?")) return;
    settingsBusy = true;
    el("save-email-settings-btn").disabled = true;
    message("email-settings-status", "Checking email delivery…");
    try {
      const result = await api("/api/settings/email");
      settingsToken = result.csrf_token;
      el("email-from").value = result.from_email || "";
      el("email-api-key").value = "";
      el("email-settings-password").value = "";
      el("email-api-key").placeholder = result.api_key_configured ? "Key configured — leave blank to keep" : "re_…";
      message("email-settings-status", result.configured ? "Email settings are configured for every branch. Verify delivery by linking an email on a test account." : "Not configured. Add your Resend key and verified sender to allow users to opt in.");
      settingsLoaded = true;
      settingsDirty = false;
      el("email-from").disabled = false;
      el("email-api-key").disabled = false;
      el("email-settings-password").disabled = false;
    } catch (error) { message("email-settings-status", error.message, true); }
    finally { settingsBusy = false; el("save-email-settings-btn").disabled = !settingsLoaded; }
  }

  async function saveSettings(event) {
    event.preventDefault();
    if (settingsBusy || !settingsLoaded || !el("email-settings-form").reportValidity()) return;
    settingsBusy = true;
    el("save-email-settings-btn").disabled = true;
    el("save-email-settings-btn").textContent = "Saving…";
    const payload = {from_email: el("email-from").value.trim(), api_key: el("email-api-key").value.trim(), password: el("email-settings-password").value};
    // Prevent edits during a save from being silently discarded by its response.
    el("email-from").disabled = true;
    el("email-api-key").disabled = true;
    el("email-settings-password").disabled = true;
    try {
      const result = await api("/api/settings/email", payload, settingsToken);
      settingsToken = result.csrf_token || settingsToken;
      el("email-api-key").value = "";
      el("email-settings-password").value = "";
      settingsDirty = false;
      message("email-settings-status", result.message || "Email settings saved for every branch.");
    } catch (error) { message("email-settings-status", error.message, true); }
    finally {
      settingsBusy = false;
      el("save-email-settings-btn").disabled = false;
      el("save-email-settings-btn").textContent = "Save email settings";
      el("email-from").disabled = false;
      el("email-api-key").disabled = false;
      el("email-settings-password").disabled = false;
    }
  }

  el("email-security-start-form").addEventListener("submit", start);
  el("email-security-verify-form").addEventListener("submit", verify);
  el("email-security-resend").addEventListener("click", resend);
  el("emailSecurityModal").addEventListener("hidden.bs.modal", reset);
  el("email-settings-form").addEventListener("submit", saveSettings);
  el("email-settings-form").addEventListener("input", () => { settingsDirty = true; });
  root.addEventListener("pagehide", () => { reset(); el("email-api-key").value = ""; el("email-settings-password").value = ""; });
  root.EmailSecurity = {open, loadSettings, reset};
})(window);