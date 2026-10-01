/* Callback calling, with browser audio when the owner calls their own phone. */
(() => {
  "use strict";
  const PHONE = /^\+[1-9][0-9]{7,14}$/;
  const SESSION = /^[0-9a-f]{32}$/;
  const REQUEST_MS = 10000;
  let config = null, session = null, attempt = null;
  let dialed = "", countryNormalized = false;
  let mutation = false, generation = 0, refreshing = null, error = "", readError = false, initialized = false;
  let browserAudio = null, audioEpoch = 0, preparationController = null;
  let trigger, dialog, offline, loading, unlockForm, code, unlockButton;
  let callForm, number, callButton, setup, status, statusTitle, statusBody;
  let keypad, backspace, clearNumber, content, minimizeButton, restoreButton;
  const keys = [];
  let errorBox, endButton, anotherButton, resumeAudioButton, selfCallHelp, dialogActions;
  let liveKeypad, agentControls, agentButtons, humanButton, agentSignature = "";
  const liveKeys = [], agentKeys = [];

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
  function icon(pathData) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    for (const [name, value] of Object.entries({viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
      "stroke-width": "1.8", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true"})) svg.setAttribute(name, value);
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", pathData); svg.append(path); return svg;
  }
  const handset = "M7 3H4a1 1 0 0 0-1 1c0 9.4 7.6 17 17 17a1 1 0 0 0 1-1v-3l-5-2-2 2a13 13 0 0 1-7-7l2-2-2-5Z";
  function minimize() {
    content.hidden = true; restoreButton.hidden = false;
    dialog.setAttribute("data-minimized", "true");
    trigger.setAttribute("aria-expanded", "false"); restoreButton.focus();
  }
  const online = () => navigator.onLine !== false;
  const active = () => Boolean(session && session.phase !== "ended");
  const canCall = () => Boolean(config?.authenticated || config?.public_calling);
  const visitor = () => Boolean(config?.public_calling && !config?.authenticated);
  const selfCall = to => Boolean(config?.authenticated && PHONE.test(config?.self_call_number || "") && to === config.self_call_number);
  const sdkCallingEnabled = () => Boolean(config?.authenticated && (config.browser_voice_enabled || config.browser_transport === "twilio-voice-sdk"));
  const browserDestination = to => sdkCallingEnabled() || selfCall(to);
  const destination = value => !value.startsWith("+") && /^[0-9]{10}$/.test(value) && selfCall("+1" + value)
    ? "+1" + value : value.startsWith("+") ? value : "+" + value;
  const browserCall = () => Boolean(session?.browser_audio);
  const audioConnected = () => Boolean(browserAudio?.connected && browserAudio.sessionId === session?.id);
  const usNumber = () => Boolean(config?.countries?.includes("US") ||
    (config?.destinations?.length && config.destinations.every(value => /^\+1[0-9]{10}$/.test(value))));
  const setText = (element, value) => {if (element.textContent !== value) element.textContent = value;};
  function normalizeCountryCode() {
    if (!countryNormalized && (usNumber() || selfCall(destination(dialed))) && /^\+?1[0-9]{10}$/.test(dialed)) {
      dialed = dialed.replace(/^\+?1/, "");
      countryNormalized = true;
    }
  }
  function editNumber(action) {
    if (mutation || attempt || content.hidden || callForm.hidden) return;
    if (action === "backspace") dialed = dialed.slice(0, -1);
    else if (action === "clear") dialed = "";
    else if (/^[0-9*#+]$/.test(action) && dialed.length < 32) dialed += action;
    if (!dialed) countryNormalized = false;
    normalizeCountryCode();
    error = ""; readError = false; render();
  }
  function numberKey(event) {
    if (event.key === "Escape") {event.preventDefault(); minimize(); return;}
    if (event.ctrlKey || event.metaKey || event.altKey || content.hidden || callForm.hidden ||
        ["INPUT", "TEXTAREA", "SELECT"].includes(event.target?.tagName) || event.target?.isContentEditable) return;
    if (/^[0-9*#+]$/.test(event.key)) {event.preventDefault(); editNumber(event.key);}
    else if (event.key === "Backspace") {event.preventDefault(); editNumber("backspace");}
    else if (event.key === "Delete") {event.preventDefault(); editNumber("clear");}
    else if (event.key === "Enter" && event.target === number) {event.preventDefault(); callForm.requestSubmit();}
  }
  function pasteNumber(event) {
    if (event.target !== number || mutation || attempt || content.hidden || callForm.hidden) return;
    event.preventDefault();
    const pasted = event.clipboardData?.getData("text") || "";
    const cleaned = pasted.replace(/[\s().-]/g, "");
    countryNormalized = false;
    if (cleaned.length > 32) {
      dialed = "";
      error = "That number is too long. Paste only the phone number, without extra text.";
    } else {
      dialed = cleaned;
      normalizeCountryCode();
      error = /[^0-9+*#]/.test(dialed) ? "Paste only the phone number, without extension text or letters." : "";
    }
    readError = false; render();
  }
  function adopt(value) {
    if (!value || !SESSION.test(value.id)) return;
    if (value.phase === "ended" || (browserAudio?.sessionId && browserAudio.sessionId !== value.id)) stopBrowserAudio();
    session = value;
    attempt = null;
  }

  function stopBrowserAudio() {
    audioEpoch++;
    preparationController?.abort(); preparationController = null;
    if (browserAudio) {browserAudio.stop(); browserAudio = null;}
  }

  async function prepareBrowserAudio() {
    const epoch = audioEpoch;
    if (!window.DashboardBrowserAudio) throw new Error("Browser audio is unavailable. Refresh this page and try again.");
    const controller = preparationController = new AbortController();
    let prepared;
    try {prepared = await window.DashboardBrowserAudio.prepare({
      transport: sdkCallingEnabled() ? "twilio-voice-sdk" : "legacy", signal: controller.signal
    });} finally {if (preparationController === controller) preparationController = null;}
    if (epoch !== audioEpoch) {prepared.stop(); throw new Error("Browser calling was cancelled. Try again.");}
    if (prepared.stopped) throw new Error("Browser audio was interrupted before the call started. Try again.");
    browserAudio = prepared;
    prepared.onDisconnect = message => {
      if (browserAudio !== prepared) return;
      browserAudio = null; audioEpoch++;
      if (active()) error = message;
      render();
      // An ended SDK leg also closes the remote phone leg. Reconcile explicitly
      // once; disconnect handlers never start or retry a call.
      if (prepared.transport === "twilio-voice-sdk" && active()) end(); else refresh();
    };
  }

  function sendPhoneDigit(digit) {
    if (mutation || !online() || !config?.authenticated || !active() || !audioConnected() || typeof browserAudio?.sendDigits !== "function") return;
    try {browserAudio.sendDigits(digit);} catch (_) {error = "That keypad digit could not be sent. Try again."; render();}
  }

  async function controlAgent(slot) {
    if (mutation || !online() || !config?.authenticated || !config?.manual_takeover_enabled ||
        !active() || session.phase !== "connected" || !audioConnected() || browserAudio?.transport !== "twilio-voice-sdk") return;
    if (slot !== "0" && !(config.agents || []).some(agent => String(agent.slot) === slot)) return;
    const id = session.id;
    mutation = true; generation++; error = ""; readError = false; render();
    try {
      const result = await request(`/api/sessions/${id}/${slot === "0" ? "mode" : "takeover"}`, {
        method: "POST", body: JSON.stringify(slot === "0" ? {mode: "human"} : {slot})
      });
      if (session?.id === id && active()) session = {...session, mode: result.mode};
    } catch (failure) {error = failure.status ? failure.message : "Could not confirm the agent change. Check call status before trying again.";}
    finally {mutation = false; generation++; render();}
    await refresh();
  }

  async function attachBrowserAudio() {
    const id = session.id, prepared = browserAudio;
    const credentials = await request(`/api/sessions/${id}/browser-token`, {method: "POST"});
    if (prepared !== browserAudio || !active() || session.id !== id) throw new Error("Browser calling was cancelled. Try again.");
    await prepared.attach(credentials, id);
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
    const allowed = canCall(), publicVisitor = visitor();
    const hasCall = Boolean(session);
    trigger.textContent = active() ? "Call in progress" : "New call";
    trigger.setAttribute("data-active", String(active()));
    restoreButton.setAttribute("aria-label", active() ? "Restore active call" : "Restore dialer");
    restoreButton.setAttribute("data-active", String(active()));
    loading.hidden = config !== null;
    unlockForm.hidden = config === null || allowed;
    code.disabled = mutation;
    unlockButton.disabled = mutation || !online();
    unlockButton.textContent = mutation ? "Unlocking…" : "Unlock calling";
    setup.hidden = !allowed || config.enabled || hasCall;
    setup.textContent = "Calling is not set up yet. Ask the owner to enable outbound calling and approve a phone number.";
    callForm.hidden = !allowed || !config.enabled || hasCall;
    let displayed = dialed;
    if (usNumber() && /^[0-9]{1,10}$/.test(displayed)) displayed = [displayed.slice(0, 3), displayed.slice(3, 6), displayed.slice(6)].filter(Boolean).join(" ");
    setText(number, displayed);
    for (const key of keys) key.disabled = mutation || Boolean(attempt);
    backspace.disabled = clearNumber.disabled = mutation || Boolean(attempt) || !dialed;
    callButton.disabled = mutation || !online() || Boolean(config?.busy && !active()) || (!dialed && !attempt);
    const displayedDestination = /^[0-9]{10}$/.test(dialed) && usNumber() ? "+1" + dialed : destination(dialed);
    const browserDestinationSelected = browserDestination(displayedDestination);
    selfCallHelp.hidden = !browserDestinationSelected || callForm.hidden;
    setText(selfCallHelp, sdkCallingEnabled() ? "Speak and listen through this browser's microphone and speaker. Keep this tab open during the call."
      : "Calling your own number uses this browser's microphone and speaker. Your phone rings once; no need to press 1.");
    const callLabel = mutation ? "Starting call…" : attempt ? "Retry same request" : browserDestinationSelected ? "Call from this browser" : publicVisitor ? "Call owner's phone" : "Call my phone";
    callButton.setAttribute("aria-label", callLabel);
    status.hidden = !allowed || !hasCall;
    status.setAttribute("data-ended", String(session?.phase === "ended"));
    const phases = {
      reserved: publicVisitor ? ["Ringing the owner's phone", "The owner needs to answer and press 1 to continue."] : ["Ringing your phone", "Answer your phone and press 1 to continue."],
      owner_ringing: publicVisitor ? ["Ringing the owner's phone", "The owner needs to answer and press 1 to continue."] : ["Ringing your phone", "Answer your phone and press 1 to continue."],
      owner_prompt: publicVisitor ? ["Waiting for the owner", "The owner must press 1 before the other person is called."] : ["Press 1 on your phone", "Press 1 to call the other person. They haven't been called yet."],
      remote_setup: ["Calling the other person", publicVisitor ? "The owner is on the line while the other person's phone rings." : "Stay on the line while their phone rings."],
      connected: ["Connected", publicVisitor ? "The owner and the other person are connected. You can close this window; the call will continue." : "Speak and listen on your phone. You can close this window; the call will continue."],
      ended: ["Call ended", endedMessage(session?.ended_reason)]
    };
    const phase = phases[session?.phase] || ["Checking call status", "The call continues on the phones."];
    if (session?.audio_path === "native-conference" && session.phase === "connected" && !publicVisitor) {
      phase[0] = session.mode === "agent" || session.mode === "announcing" ? "AI agent speaking" : "Connected";
      phase[1] = "For phone controls, press *, wait for the prompt, then press your agent's number. Use 0 to return to speaking. You briefly stop hearing the call during the menu.";
    }
    if (browserCall() && active()) {
      if (!audioConnected()) {
        phase[0] = mutation ? "Connecting browser audio" : "Use browser audio";
        phase[1] = "Use the microphone in this tab to speak and listen. If this call is open in another tab, continue there.";
      } else {
        const sdk = browserAudio.transport === "twilio-voice-sdk";
        const codec = browserAudio.codec === "opus" ? "Opus" : browserAudio.codec === "pcmu" ? "PCMU" : "";
        const browserStatus = `Browser audio connected${sdk && codec ? ` (${codec})` : ""}`;
        const agentSpeaking = session.mode === "agent" || session.mode === "announcing";
        phase[0] = session.phase === "connected" ? (agentSpeaking ? "AI agent speaking" : sdk ? browserStatus : "Connected")
          : session.phase === "reserved" ? "Starting your call" : sdk ? "Calling the other person" : "Ringing your phone";
        phase[1] = session.phase === "connected" ? (sdk && agentSpeaking ? `${browserStatus}. Listen while the agent speaks. Use #0 Speak again to return to speaking. Keep this tab open.`
          : "Speak and listen here. Keep this tab open; minimizing the dialer keeps the call connected.")
          : sdk ? `${browserStatus}. Waiting for the other person to answer; no need to press 1. Keep this tab open.`
          : "Your phone rings once. Answer it to connect; no need to press 1. Speak and listen here, and keep this tab open.";
      }
    }
    setText(statusTitle, phase[0]);
    setText(statusBody, phase[1]);
    endButton.hidden = !allowed || !active();
    endButton.disabled = mutation || !online();
    endButton.textContent = mutation ? "Ending…" : "End call";
    anotherButton.hidden = !allowed || !hasCall || active();
    anotherButton.disabled = mutation;
    resumeAudioButton.hidden = !allowed || !config?.authenticated || !active() || !browserCall() || audioConnected();
    resumeAudioButton.disabled = mutation || !online();
    const liveBrowser = Boolean(active() && config?.authenticated && audioConnected() && browserAudio.transport === "twilio-voice-sdk");
    liveKeypad.hidden = !liveBrowser || session.phase !== "connected";
    for (const key of liveKeys) key.disabled = mutation || !online();
    const agents = (config?.agents || []).filter(agent => /^[1-9]$/.test(String(agent.slot)));
    const signature = JSON.stringify(agents.map(agent => [String(agent.slot), String(agent.name || "Agent")]));
    if (signature !== agentSignature) {
      agentSignature = signature; agentKeys.length = 0; agentButtons.replaceChildren();
      for (const agent of agents) {
        const slot = String(agent.slot), key = button(`#${slot} ${agent.name || "Agent"}`, "toolbar-secondary-button", `dialer-agent-${slot}`);
        key.addEventListener("click", () => controlAgent(slot)); agentKeys.push(key); agentButtons.append(key);
      }
    }
    agentControls.hidden = !liveBrowser || session.phase !== "connected" || !config?.manual_takeover_enabled ||
      (!agents.length && session?.mode === "human");
    for (const key of agentKeys) key.disabled = mutation || !online() || !config?.voice_ready;
    humanButton.disabled = mutation || !online() || session?.mode === "human";
    dialogActions.hidden = endButton.hidden && anotherButton.hidden && resumeAudioButton.hidden;
    offline.hidden = online();
    setText(offline, browserCall() && active() ? "You're offline. Browser audio will disconnect and the call will end. Reconnect before starting another call." : active()
      ? "You're offline. The phone call can continue. Status will reconnect when you're online."
      : "You're offline. Reconnect to start a call or check its status.");
    setText(errorBox, error || (allowed && config.busy && !hasCall ? "Another call is in progress. Wait for it to finish before starting a new call." : ""));
    errorBox.hidden = !errorBox.textContent;
  }

  function endedMessage(reason) {
    if (reason === "remote-busy") return "The other person's line was busy.";
    if (reason === "remote-no-answer") return "The other person didn't answer.";
    if (/owner-no-answer|owner-ring|owner-accept/.test(reason || "")) return visitor()
      ? "The owner's phone wasn't answered or the call wasn't accepted. You can try again."
      : "Your phone wasn't answered or the call wasn't accepted. You can try again.";
    if (/failed|timeout|error|setup/.test(reason || "")) return "The call could not connect. You can try again.";
    return browserCall() ? "The browser and phone connections have been closed." : "Both phone connections have been closed.";
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
        if (!canCall()) {stopBrowserAudio(); session = null; attempt = null;}
        else if (!config.authenticated && browserAudio) {stopBrowserAudio(); session = null; attempt = null;}
        else if (config.active_session) {if (attempt) error = ""; adopt(config.active_session);}
        else if (session && session.phase !== "ended") {
          try {
            const nextSession = await request(`/api/sessions/${session.id}`);
            if (revision === generation) {adopt(nextSession); if (session?.phase === "ended") error = "";}
          } catch (failure) {
            if (revision !== generation) return;
            if (failure.status === 404) {
              stopBrowserAudio();
              session = null;
              error = visitor() ? "This call is no longer available. Check with the owner before starting another call."
                : "This call is no longer available. Check your phone before starting another call.";
            } else throw failure;
          }
        }
      } catch (_) {
        if (revision === generation) {readError = true; error = browserCall() && active() ? "Call status is unavailable. Keep this tab open while browser audio is connected."
          : active()
          ? "Call status is unavailable. The phone call can continue; status will reconnect automatically."
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
    if (mutation || active() || !canCall() || !config.enabled || config.busy || !online()) return;
    if (!attempt) {
      if (/[^0-9+*#]/.test(dialed)) {error = "Use only the phone number, without extension text or letters."; render(); number.focus(); return;}
      if (/[*#]/.test(dialed)) {error = "Use digits only for phone numbers. Remove * and # before calling."; render(); number.focus(); return;}
      let to = destination(dialed);
      if (usNumber()) {
        if (dialed.startsWith("+") && !/^\+1[0-9]{10}$/.test(dialed)) {
          error = "Use a US phone number: 10 digits, or +1 followed by 10 digits. Other country codes are not supported.";
          render(); number.focus(); return;
        }
        if (/^[0-9]{10}$/.test(dialed)) to = "+1" + dialed;
        else if (countryNormalized || !/^\+?1[0-9]{10}$/.test(dialed)) {error = "Enter a 10-digit US phone number, including the area code."; render(); number.focus(); return;}
      }
      if (!PHONE.test(to)) {error = "Enter a phone number with its country code, such as +14155550123."; render(); number.focus(); return;}
      if (!selfCall(to) && !(config.destinations || []).includes(to) && !(config.countries?.includes("US") && /^\+1[0-9]{10}$/.test(to))) {
        error = config.countries?.includes("US") ? "Enter a US phone number with +1 and the area code." : "This number is not approved for calling. Choose an approved number.";
        render(); number.focus(); return;
      }
      if (!crypto.randomUUID) {error = "Calling needs a secure connection. Open this site with HTTPS."; render(); return;}
      attempt = {key: crypto.randomUUID(), payload: {to, goal: "", ...(sdkCallingEnabled() ? {browser_audio: true} : {})}, browser: browserDestination(to)};
    }
    mutation = true; generation++; error = ""; readError = false; render();
    let posted = false;
    try {
      if (attempt.browser) await prepareBrowserAudio();
      posted = true;
      const result = await request("/api/calls/outbound", {method: "POST", headers: {"Idempotency-Key": attempt.key}, body: JSON.stringify(attempt.payload)});
      if (!SESSION.test(result.session_id)) throw new Error("Invalid call response");
      adopt({id: result.session_id, phase: result.phase, browser_audio: result.browser_audio === true,
        audio_path: result.audio_path, mode: result.mode});
      if (browserCall() && active()) {
        if (!browserAudio) throw new Error("Browser audio is unavailable. Use the microphone button to connect.");
        await attachBrowserAudio();
      } else stopBrowserAudio();
    } catch (failure) {
      stopBrowserAudio();
      if (!posted) {attempt = null; error = failure.message;}
      else if (session?.browser_audio) {
        error = failure.status === 409 ? "This call is already open in another tab. Continue the call there." : failure.message;
      } else if (failure.status && failure.status < 500) {
        attempt = null;
        error = failure.message.includes("destination-not-allowed") ? (config.countries?.includes("US")
          ? "Enter an allowed US phone number. " + (visitor() ? "The owner's phone and the service number cannot be called." : "The service number cannot be called.")
          : "Choose an approved number. " + (visitor() ? "The owner's phone and the service number cannot be called." : "The service number cannot be called."))
          : failure.status === 403 ? (visitor() ? "Calling is unavailable for this request. Refresh the page and try again." : "Unlock calling again, or choose an approved number.")
          : failure.status === 429 || failure.status === 409 ? "Another call is already in progress. Its status will appear here."
          : failure.message;
      } else error = attempt?.browser ? "We couldn't confirm whether the call started. Your microphone is off. Check call status before retrying the same request."
        : visitor() ? "We couldn't confirm whether the call started. Check with the owner before retrying."
        : "We couldn't confirm whether the call started. Check your phone before retrying.";
    } finally {mutation = false; generation++; render();}
    // Reconcile reads only. Never automatically retry the call-creation request.
    await refresh();
  }

  async function resumeBrowserAudio() {
    if (mutation || !active() || !browserCall() || audioConnected() || !config?.authenticated || !online()) return;
    mutation = true; generation++; error = ""; readError = false; render();
    try {await prepareBrowserAudio(); await attachBrowserAudio();}
    catch (failure) {stopBrowserAudio(); error = failure.status === 409 ? "This call is already open in another tab. Continue the call there." : failure.message;}
    finally {mutation = false; generation++; render();}
    await refresh();
  }

  async function end() {
    if (mutation || !active() || !canCall() || !online()) return;
    const id = session.id;
    if (browserCall()) stopBrowserAudio();
    mutation = true; generation++; error = ""; readError = false; render();
    try {adopt(await request(`/api/sessions/${id}/end`, {method: "POST"}));}
    catch (_) {error = visitor() ? "Could not confirm the call ended. Check with the owner or try End call again."
      : "Could not confirm the call ended. Check your phone or try End call again.";}
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
    trigger.setAttribute("aria-expanded", "false");
    mount.prepend(trigger);
    dialog = node("dialog", "toolbar-dialog dialer-dialog"); dialog.id = "dialer-dialog";
    dialog.setAttribute("aria-label", "Phone keypad");
    dialog.setAttribute("aria-modal", "false");
    dialog.setAttribute("data-minimized", "false");
    dialog.addEventListener("cancel", event => {event.preventDefault(); minimize();});
    content = node("div", "dialer-content");
    const controls = node("div", "dialer-panel-controls");
    minimizeButton = button("", "toolbar-icon dialer-minimize", "dialer-minimize");
    minimizeButton.setAttribute("aria-label", "Minimize dialer");
    minimizeButton.append(icon("M5 12h14")); minimizeButton.addEventListener("click", minimize);
    controls.append(minimizeButton);
    restoreButton = button("", "dialer-call-button dialer-restore", "dialer-restore");
    restoreButton.setAttribute("aria-label", "Restore dialer");
    restoreButton.append(icon(handset)); restoreButton.hidden = true;
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
    const display = node("div", "dialer-number-display");
    number = node("output", "dialer-number"); number.id = "dialer-number"; number.tabIndex = 0;
    number.setAttribute("aria-label", "Phone number"); number.setAttribute("aria-live", "polite");
    display.append(number);
    const editing = node("div", "dialer-number-actions");
    clearNumber = button("Clear", "dialer-edit", "dialer-clear"); clearNumber.setAttribute("aria-label", "Clear phone number");
    clearNumber.addEventListener("click", () => editNumber("clear"));
    backspace = button("⌫", "dialer-edit dialer-backspace", "dialer-backspace"); backspace.setAttribute("aria-label", "Delete last digit");
    backspace.addEventListener("click", () => editNumber("backspace")); editing.append(clearNumber, backspace);
    keypad = node("div", "dialer-keypad"); keypad.id = "dialer-keypad"; keypad.setAttribute("role", "group"); keypad.setAttribute("aria-label", "Phone keypad");
    const letters = {2: "ABC", 3: "DEF", 4: "GHI", 5: "JKL", 6: "MNO", 7: "PQRS", 8: "TUV", 9: "WXYZ"};
    for (const digit of ["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#"]) {
      const name = digit === "*" ? "star" : digit === "#" ? "hash" : digit;
      const key = button("", "dialer-key", `dialer-key-${name}`);
      key.setAttribute("aria-label", digit === "*" ? "Star (*)" : digit === "#" ? "Hash (#)" : digit);
      const value = node("span", "dialer-key-digit", digit); value.setAttribute("aria-hidden", "true");
      const abc = node("span", "dialer-key-letters", letters[digit] || "\u00a0"); abc.setAttribute("aria-hidden", "true");
      key.append(value, abc); key.addEventListener("click", () => editNumber(digit)); keys.push(key); keypad.append(key);
    }
    callButton = button("", "dialer-call-button", "dialer-start"); callButton.type = "submit";
    callButton.append(icon(handset));
    const callActions = node("div", "dialer-keypad-call"); callActions.append(callButton);
    selfCallHelp = node("p", "dialer-help", "Calling your own number uses this browser's microphone and speaker. Your phone rings once; no need to press 1.");
    selfCallHelp.id = "dialer-self-call-help";
    callForm.append(display, editing, keypad, callActions, selfCallHelp); callForm.addEventListener("submit", start);
    dialog.addEventListener("keydown", numberKey);
    dialog.addEventListener("paste", pasteNumber);
    status = node("section", "dialer-state"); status.id = "dialer-status"; status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite"); status.setAttribute("aria-atomic", "true");
    statusTitle = node("h3"); statusBody = node("p"); status.append(statusTitle, statusBody);
    liveKeypad = node("div", "dialer-keypad"); liveKeypad.id = "dialer-live-keypad";
    liveKeypad.setAttribute("role", "group"); liveKeypad.setAttribute("aria-label", "Send phone keypad tones");
    for (const digit of ["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#"]) {
      const name = digit === "*" ? "star" : digit === "#" ? "hash" : digit;
      const key = button(digit, "dialer-key", `dialer-live-key-${name}`);
      key.setAttribute("aria-label", `Send ${digit === "*" ? "star" : digit === "#" ? "hash" : digit} to the phone call`);
      key.addEventListener("click", () => sendPhoneDigit(digit)); liveKeys.push(key); liveKeypad.append(key);
    }
    agentControls = node("section"); agentControls.id = "dialer-agent-controls";
    agentControls.setAttribute("aria-label", "AI agent controls");
    agentControls.append(node("p", "dialer-help", "Choose an agent to speak while you listen. Use #0 to speak again."));
    agentButtons = node("div", "dialer-actions");
    humanButton = button("#0 Speak again", "toolbar-primary-button", "dialer-agent-human");
    humanButton.addEventListener("click", () => controlAgent("0"));
    agentControls.append(agentButtons, humanButton);
    errorBox = node("p", "toolbar-error dialer-error"); errorBox.id = "dialer-error"; errorBox.setAttribute("role", "alert");
    endButton = button("End call", "toolbar-secondary-button dialer-end", "dialer-end"); endButton.addEventListener("click", end);
    anotherButton = button("Start another call", "toolbar-primary-button", "dialer-another");
    anotherButton.addEventListener("click", () => {stopBrowserAudio(); session = null; attempt = null; error = ""; render(); number.focus(); refresh();});
    resumeAudioButton = button("Use microphone in this tab", "toolbar-primary-button", "dialer-resume-audio");
    resumeAudioButton.addEventListener("click", resumeBrowserAudio);
    dialogActions = node("div", "dialer-actions"); dialogActions.append(resumeAudioButton, endButton, anotherButton);
    content.append(controls, offline, loading, unlockForm, setup, callForm, status, liveKeypad, agentControls, errorBox, dialogActions);
    dialog.append(content, restoreButton); document.body.append(dialog);
    const open = () => {
      content.hidden = false; restoreButton.hidden = true;
      dialog.setAttribute("data-minimized", "false"); trigger.setAttribute("aria-expanded", "true");
      if (!dialog.open) dialog.show();
      render(); (callForm.hidden ? minimizeButton : number).focus(); refresh();
    };
    trigger.addEventListener("click", open);
    restoreButton.addEventListener("click", open);
    dialog.addEventListener("close", () => trigger.focus());
    window.DashboardDialer = {open};
    window.addEventListener("pagehide", stopBrowserAudio);
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
