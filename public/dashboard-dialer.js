/* Owner-only callback calling. Audio stays on the two phones, including offline. */
(() => {
  "use strict";
  const PHONE = /^\+[1-9][0-9]{7,14}$/;
  const SESSION = /^[0-9a-f]{32}$/;
  const REQUEST_MS = 10000;
  let config = null, session = null, attempt = null;
  let mutation = false, generation = 0, refreshing = null, error = "", readError = false, initialized = false;
  let trigger, dialog, description, offline, loading, unlockForm, code, unlockButton;
  let callForm, number, numberHelp, goal, choices, callButton, setup, status, statusTitle, statusBody;
  let errorBox, endButton, anotherButton, dialogActions;

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text) element.textContent = text;
    return element;
  }
  function button(label, className, id) {
    const element = node("button", className, label);
    element.type = "button";
    if (id) element.id = id;
    return element;
  }
  const online = () => navigator.onLine !== false;
  const active = () => Boolean(session && session.phase !== "ended");
  const setText = (element, value) => {if (element.textContent !== value) element.textContent = value;};
  function adopt(value) {
    if (!value || !SESSION.test(value.id)) return;
    session = value;
    attempt = null;
  }

  async function request(url, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), REQUEST_MS);
    try {
      const response = await fetch(url, {...options, credentials: "same-origin", cache: "no-store",
        headers: {"X-Agent-Request": "1", ...(options.body ? {"Content-Type": "application/json"} : {}), ...options.headers},
        signal: controller.signal});
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        const failure = new Error(typeof body.detail === "string" ? body.detail : "The request could not be completed.");
        failure.status = response.status;
        throw failure;
      }
      return body;
    } finally { clearTimeout(timeout); }
  }

  function render() {
    const authenticated = Boolean(config?.authenticated);
    const hasCall = Boolean(session);
    trigger.textContent = active() ? "Call in progress" : "New call";
    trigger.setAttribute("data-active", String(active()));
    description.textContent = authenticated && config.owner_label
      ? `We'll ring your phone (${config.owner_label}) first. Answer and press 1 to connect the other person.`
      : "We'll ring your phone first. Answer and press 1 to connect the other person.";
    loading.hidden = config !== null;
    unlockForm.hidden = config === null || authenticated;
    code.disabled = mutation;
    unlockButton.disabled = mutation || !online();
    unlockButton.textContent = mutation ? "Unlocking…" : "Unlock calling";
    setup.hidden = !authenticated || config.enabled || hasCall;
    setup.textContent = "Calling is not set up yet. Ask the owner to enable outbound calling and approve a phone number.";
    callForm.hidden = !authenticated || !config.enabled || hasCall;
    numberHelp.textContent = config?.countries?.includes("US")
      ? "Enter a US phone number with +1 and the area code."
      : "Choose an approved number, including its country code.";
    number.readOnly = Boolean(attempt);
    goal.readOnly = Boolean(attempt);
    number.disabled = mutation;
    goal.disabled = mutation;
    callButton.disabled = mutation || !online() || Boolean(config?.busy && !active());
    callButton.textContent = mutation ? "Starting call…" : attempt ? "Retry same request" : "Call my phone";
    status.hidden = !authenticated || !hasCall;
    status.setAttribute("data-ended", String(session?.phase === "ended"));
    const phases = {
      reserved: ["Ringing your phone", "Answer your phone and press 1 to continue."],
      owner_ringing: ["Ringing your phone", "Answer your phone and press 1 to continue."],
      owner_prompt: ["Press 1 on your phone", "Press 1 to call the other person. They haven't been called yet."],
      remote_setup: ["Calling the other person", "Stay on the line while their phone rings."],
      connected: ["Connected", "Speak and listen on your phone. You can close this window; the call will continue."],
      ended: ["Call ended", endedMessage(session?.ended_reason)]
    };
    const phase = phases[session?.phase] || ["Checking call status", "Your call continues on your phone."];
    setText(statusTitle, phase[0]);
    setText(statusBody, phase[1]);
    endButton.hidden = !authenticated || !active();
    endButton.disabled = mutation || !online();
    endButton.textContent = mutation ? "Ending…" : "End call";
    anotherButton.hidden = !authenticated || !hasCall || active();
    anotherButton.disabled = mutation;
    dialogActions.hidden = endButton.hidden && anotherButton.hidden;
    offline.hidden = online();
    setText(offline, active()
      ? "You're offline. Your phone call can continue. Status will reconnect when you're online."
      : "You're offline. Reconnect to start a call or check its status.");
    setText(errorBox, error || (authenticated && config.busy && !hasCall ? "Another call is in progress. Wait for it to finish before starting a new call." : ""));
    errorBox.hidden = !errorBox.textContent;
  }

  function endedMessage(reason) {
    if (reason === "remote-busy") return "The other person's line was busy.";
    if (reason === "remote-no-answer") return "The other person didn't answer.";
    if (/owner-no-answer|owner-ring|owner-accept/.test(reason || "")) return "Your phone wasn't answered or the call wasn't accepted. You can try again.";
    if (/failed|timeout|error|setup/.test(reason || "")) return "The call could not connect. You can try again.";
    return "Both phone connections have been closed.";
  }

  function refresh() {
    if (refreshing || mutation || document.hidden || !online()) { render(); return refreshing || Promise.resolve(); }
    const revision = generation;
    refreshing = (async () => {
      try {
        const next = await request("/api/calls/config");
        if (revision !== generation) return;
        config = next;
        if (readError) {error = ""; readError = false;}
        choices.replaceChildren(...(config.destinations || []).map(value => {
          const option = node("option"); option.value = value; return option;
        }));
        if (!config.authenticated) {session = null; attempt = null;}
        else if (config.active_session) {if (attempt) error = ""; adopt(config.active_session);}
        else if (session && session.phase !== "ended") {
          try {
            const nextSession = await request(`/api/sessions/${session.id}`);
            if (revision === generation) {adopt(nextSession); if (session?.phase === "ended") error = "";}
          } catch (failure) {
            if (revision !== generation) return;
            if (failure.status === 404) {
              session = null;
              error = "This call is no longer available. Check your phone before starting another call.";
            } else throw failure;
          }
        }
      } catch (_) {
        if (revision === generation) {readError = true; error = active()
          ? "Call status is unavailable. Your phone call can continue; status will reconnect automatically."
          : "Calling is unavailable right now. Check your connection and try again.";}
      } finally {refreshing = null; render(); if (revision !== generation && !mutation) refresh();}
    })();
    return refreshing;
  }

  async function unlock(event) {
    event.preventDefault();
    if (mutation || !online()) return;
    if (!code.value.trim()) {error = "Enter your one-time owner access code."; render(); code.focus(); return;}
    mutation = true; generation++; error = ""; readError = false; render();
    try {
      await request("/api/agents/session", {method: "POST", body: JSON.stringify({code: code.value.trim()})});
      code.value = "";
    } catch (failure) {error = failure.status ? failure.message : "Could not unlock calling. Check your connection and try again.";}
    finally {mutation = false; generation++; render();}
    if (!error) {await refresh(); if (config?.authenticated) number.focus();}
  }

  async function start(event) {
    event.preventDefault();
    if (mutation || active() || !config?.authenticated || !config.enabled || config.busy || !online()) return;
    if (!attempt) {
      const to = number.value.trim().replace(/[\s().-]/g, "");
      if (!PHONE.test(to)) {error = "Enter a phone number with its country code, such as +14155550123."; render(); number.focus(); return;}
      if (!(config.destinations || []).includes(to) && !(config.countries?.includes("US") && /^\+1[0-9]{10}$/.test(to))) {
        error = config.countries?.includes("US") ? "Enter a US phone number with +1 and the area code." : "This number is not approved for calling. Choose an approved number.";
        render(); number.focus(); return;
      }
      if (!crypto.randomUUID) {error = "Calling needs a secure connection. Open this site with HTTPS."; render(); return;}
      attempt = {key: crypto.randomUUID(), payload: {to, goal: goal.value.trim().slice(0, 300)}};
      number.value = to;
    }
    mutation = true; generation++; error = ""; readError = false; render();
    try {
      const result = await request("/api/calls/outbound", {method: "POST", headers: {"Idempotency-Key": attempt.key}, body: JSON.stringify(attempt.payload)});
      if (!SESSION.test(result.session_id)) throw new Error("Invalid call response");
      adopt({id: result.session_id, phase: result.phase});
    } catch (failure) {
      if (failure.status && failure.status < 500) {
        attempt = null;
        error = failure.message.includes("destination-not-allowed") ? (config.countries?.includes("US")
          ? "Enter an allowed US phone number. Your own phone and the service number cannot be called."
          : "Choose an approved number. Your own phone and the service number cannot be called.")
          : failure.status === 403 ? "Unlock calling again, or choose an approved number."
          : failure.status === 429 || failure.status === 409 ? "Another call is already in progress. Its status will appear here."
          : failure.message;
      } else error = "We couldn't confirm whether the call started. Check your phone before retrying.";
    } finally {mutation = false; generation++; render();}
    // Reconcile reads only. Never automatically retry the call-creation request.
    await refresh();
  }

  async function end() {
    if (mutation || !active() || !online()) return;
    const id = session.id;
    mutation = true; generation++; error = ""; readError = false; render();
    try {adopt(await request(`/api/sessions/${id}/end`, {method: "POST"}));}
    catch (_) {error = "Could not confirm the call ended. Check your phone or try End call again.";}
    finally {mutation = false; generation++; render();}
    await refresh();
  }

  function init() {
    if (initialized) return;
    const mount = document.getElementById("header-actions");
    if (!mount) return;
    initialized = true;
    trigger = button("New call", "toolbar-primary-button dialer-trigger", "dialer-button");
    trigger.setAttribute("aria-haspopup", "dialog");
    trigger.setAttribute("aria-controls", "dialer-dialog");
    mount.prepend(trigger);
    dialog = node("dialog", "toolbar-dialog dialer-dialog"); dialog.id = "dialer-dialog";
    dialog.setAttribute("aria-labelledby", "dialer-title");
    dialog.setAttribute("aria-describedby", "dialer-description");
    const content = node("div", "dialer-content"), heading = node("div", "dialer-heading");
    const title = node("h2", "", "Make a call"); title.id = "dialer-title";
    const close = button("×", "toolbar-icon", "dialer-close"); close.setAttribute("aria-label", "Close calling window");
    close.addEventListener("click", () => dialog.close()); heading.append(title, close);
    description = node("p", "dialer-description"); description.id = "dialer-description";
    loading = node("p", "dialer-help", "Checking calling access…"); loading.setAttribute("role", "status");
    offline = node("p", "toolbar-error dialer-offline"); offline.setAttribute("role", "status");
    unlockForm = node("form"); unlockForm.id = "dialer-unlock-form";
    unlockForm.append(node("p", "dialer-help", "Calling is for the owner. Enter your one-time owner access code to unlock it."));
    const codeLabel = node("label", "toolbar-field", "Owner access code");
    code = node("input"); code.id = "dialer-code"; code.type = "password"; code.autocomplete = "one-time-code"; code.required = true;
    codeLabel.append(code); unlockButton = button("Unlock calling", "toolbar-primary-button", "dialer-unlock"); unlockButton.type = "submit";
    unlockForm.append(codeLabel, unlockButton); unlockForm.addEventListener("submit", unlock);
    setup = node("p", "dialer-setup"); setup.id = "dialer-setup";
    callForm = node("form"); callForm.id = "dialer-call-form";
    const numberLabel = node("label", "toolbar-field", "Phone number");
    number = node("input"); number.type = "tel"; number.id = "dialer-number"; number.autocomplete = "tel"; number.required = true;
    number.placeholder = "+1 415 555 0123"; number.setAttribute("list", "dialer-destinations"); number.setAttribute("aria-describedby", "dialer-number-help");
    choices = node("datalist"); choices.id = "dialer-destinations";
    numberHelp = node("span", "dialer-field-note", "Choose an approved number, including its country code."); numberHelp.id = "dialer-number-help";
    numberLabel.append(number);
    const goalLabel = node("label", "toolbar-field", "Call goal (optional)");
    goal = node("textarea"); goal.id = "dialer-goal"; goal.maxLength = 300; goal.rows = 2;
    goal.placeholder = "What would you like to discuss?"; goalLabel.append(goal);
    callButton = button("Call my phone", "toolbar-primary-button", "dialer-start"); callButton.type = "submit";
    const callActions = node("div", "dialer-actions"); callActions.append(callButton);
    callForm.append(numberLabel, numberHelp, choices, goalLabel, callActions); callForm.addEventListener("submit", start);
    status = node("section", "dialer-state"); status.id = "dialer-status"; status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite"); status.setAttribute("aria-atomic", "true");
    statusTitle = node("h3"); statusBody = node("p"); status.append(statusTitle, statusBody);
    errorBox = node("p", "toolbar-error dialer-error"); errorBox.id = "dialer-error"; errorBox.setAttribute("role", "alert");
    endButton = button("End call", "toolbar-secondary-button dialer-end", "dialer-end"); endButton.addEventListener("click", end);
    anotherButton = button("Start another call", "toolbar-primary-button", "dialer-another");
    anotherButton.addEventListener("click", () => {session = null; attempt = null; error = ""; render(); number.focus(); refresh();});
    dialogActions = node("div", "dialer-actions"); dialogActions.append(endButton, anotherButton);
    content.append(heading, description, offline, loading, unlockForm, setup, callForm, status, errorBox, dialogActions);
    dialog.append(content); document.body.append(dialog);
    const open = () => {if (!dialog.open) dialog.showModal(); render(); refresh();};
    trigger.addEventListener("click", open);
    dialog.addEventListener("close", () => trigger.focus());
    window.DashboardDialer = {open};
    const resume = () => {render(); refresh();};
    window.addEventListener("online", resume); window.addEventListener("offline", render);
    window.addEventListener("focus", resume); window.addEventListener("pageshow", resume);
    document.addEventListener("visibilitychange", resume);
    setInterval(() => {if (dialog.open || active() || attempt) refresh();}, 3000);
    render(); refresh();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, {once: true});
  else init();
})();
