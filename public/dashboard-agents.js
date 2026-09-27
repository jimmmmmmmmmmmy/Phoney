(() => {
  "use strict";
  const MAX_UPLOAD_BYTES = 16 * 1024 * 1024;
  const state = {drafts: [], loaded: false, workspaceError: "", config: null, selected: null,
    values: null, dirty: false, pending: false, notice: "", error: "", sessions: [], sessionId: "", slot: ""};
  let mount, initialized = false, configRequest = 0;
  const $ = id => document.getElementById(id);
  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }
  function button(text, action, primary = false) {
    const control = node("button", primary ? "toolbar-primary-button" : "toolbar-secondary-button", text);
    control.type = "button";
    control.disabled = state.pending;
    control.addEventListener("click", action);
    return control;
  }
  function option(value, text) {
    const element = node("option", "", text); element.value = String(value); return element;
  }
  function field(label, control) {
    const wrapper = node("label", "agent-field", label);
    wrapper.append(control); return wrapper;
  }
  function input(id, value = "", type = "text") {
    const control = node("input"); control.id = id; control.type = type; control.value = value;
    control.disabled = state.pending; return control;
  }
  function voiceState(voice) { return voice.ready ? "Ready" : voice.requiresVerification ? "Verification needed" : "Unavailable"; }
  function published(id) { return (state.config?.agents || []).find(agent => agent.id === id); }
  function allAgents() {
    const records = new Map(state.drafts.map(agent => [agent.id, agent]));
    for (const agent of state.config?.agents || []) if (!records.has(agent.id)) records.set(agent.id, agent);
    return [...records.values()];
  }
  function choose(id) {
    const draft = allAgents().find(agent => agent.id === id);
    if (!draft) return;
    const saved = published(id);
    state.selected = id;
    state.values = {name: draft.name ?? saved?.name, prompt: draft.prompt ?? saved?.prompt ?? "",
      voiceProfileId: saved?.voiceProfileId || "", slot: saved?.slot == null ? "" : String(saved.slot)};
    state.dirty = false; state.error = ""; state.notice = "";
  }
  function remember(control, key) {
    control.addEventListener("input", () => {state.values[key] = control.value; state.dirty = true;});
    control.addEventListener("change", () => {state.values[key] = control.value; state.dirty = true;});
    return control;
  }
  async function request(path, {method = "GET", body, multipart = false, timeout = 15000} = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(path, {method, credentials: "same-origin", cache: "no-store", signal: controller.signal,
        headers: {Accept: "application/json", ...(method === "GET" ? {} : {"X-Agent-Request": "1"}),
          ...(body === undefined || multipart ? {} : {"Content-Type": "application/json"})},
        ...(body === undefined ? {} : {body: multipart ? body : JSON.stringify(body)})});
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        if (response.status === 401 || response.status === 403) {
          if (state.config) state.config = {...state.config, authenticated: false, agents: [], voices: []};
          state.sessions = [];
          throw new Error("Owner access is locked or expired. Enter a new one-time code.");
        }
        if (response.status === 409) throw new Error(typeof data.detail === "string" ? data.detail : "This slot or call is unavailable. Refresh and try again.");
        if (response.status === 413) throw new Error("The audio upload is too large. Use smaller recordings.");
        if (response.status === 400 || response.status === 422) throw new Error(typeof data.detail === "string" ? data.detail : "Review the entered details and try again.");
        throw new Error(response.status === 503 ? (typeof data.detail === "string" ? data.detail : "Agent controls are not enabled on this server.") : "The request could not be completed. Try again.");
      }
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("The request timed out. Refresh before retrying to check whether it completed.");
      throw error;
    } finally {clearTimeout(timer);}
  }
  async function perform(operation) {
    if (state.pending) return;
    state.pending = true; state.error = ""; state.notice = ""; render();
    try {await operation();}
    catch (error) {state.error = error instanceof Error ? error.message : "The request failed. Try again.";}
    finally {state.pending = false; render();}
  }
  async function loadConfig() {
    const generation = ++configRequest;
    const config = await request("/api/agents/config");
    if (generation !== configRequest) return;
    state.config = config;
    if (!state.selected && allAgents().length) choose(allAgents()[0].id);
    if (state.selected && !state.dirty) choose(state.selected);
  }
  function validDraft() {
    const values = state.values;
    if (!values?.name.trim()) throw new Error("Enter an agent name.");
    if (values.name.trim().length > 80 || values.prompt.length > 8000) throw new Error("Use at most 80 characters for the name and 8,000 for the prompt.");
    return {name: values.name.trim(), prompt: values.prompt.trim()};
  }
  function saveDraft() {
    perform(async () => {
      const values = validDraft();
      const original = state.drafts.find(agent => agent.id === state.selected);
      if (!window.DashboardWorkspace?.saveAgent) throw new Error("Workspace storage is unavailable.");
      await window.DashboardWorkspace.saveAgent({id: state.selected, createdAt: original?.createdAt || new Date().toISOString(), ...values});
      state.dirty = false; state.notice = "Draft saved. Published call settings are unchanged.";
    });
  }
  function publishAgent() {
    perform(async () => {
      if (!state.config?.authenticated || !state.config?.enabled) throw new Error("Unlock owner controls before publishing call settings.");
      const values = validDraft();
      if (!values.prompt) throw new Error("Add a prompt before publishing call settings.");
      const slot = state.values.slot ? Number(state.values.slot) : null;
      const voice = (state.config?.voices || []).find(item => item.id === state.values.voiceProfileId);
      if (slot !== null && (!voice || !voice.ready)) throw new Error("Choose a ready voice before assigning a call shortcut.");
      const original = state.drafts.find(agent => agent.id === state.selected);
      if (window.DashboardWorkspace?.saveAgent) await window.DashboardWorkspace.saveAgent({id: state.selected,
        createdAt: original?.createdAt || new Date().toISOString(), ...values});
      const saved = await request(`/api/agents/${encodeURIComponent(state.selected)}`, {method: "PUT",
        body: {...values, voiceProfileId: state.values.voiceProfileId || null, slot}});
      state.config.agents = [...(state.config.agents || []).filter(agent => agent.id !== saved.id), saved];
      state.dirty = false; state.notice = slot ? `Published for #${slot}. Takeover remains manual.` : "Configuration saved without a call shortcut.";
    });
  }
  function renderList() {
    const list = node("div", "agents-list");
    list.setAttribute("aria-label", "Saved agents");
    const records = allAgents();
    if (!state.loaded && !records.length) list.append(node("p", "agent-empty", state.workspaceError || "Loading workspace agents…"));
    else if (!records.length) list.append(node("p", "agent-empty", "No agents yet. Create an agent to save a prompt and choose its voice."));
    for (const record of records) {
      const saved = published(record.id);
      const row = button("", () => {
        if (state.dirty && !window.confirm("Discard unsaved agent changes?")) return;
        choose(record.id); render();
      });
      row.className = "agent-select"; row.setAttribute("aria-pressed", String(record.id === state.selected));
      row.append(node("span", "agent-select-name", record.name || saved?.name),
        node("span", "agent-state", saved ? (saved.slot ? `#${saved.slot} · Published` : "Unassigned") : "Draft"));
      list.append(row);
    }
    return list;
  }
  function renderEditor() {
    const panel = node("section", "agent-editor");
    panel.setAttribute("aria-label", "Agent configuration");
    if (!state.values) {panel.append(node("p", "agent-empty", "Select an agent to edit its prompt, voice, and call shortcut.")); return panel;}
    const heading = node("div", "agent-section-heading");
    const revision = published(state.selected)?.revision;
    heading.append(node("h3", "", "Agent details"), node("span", "agent-state", revision ? `Revision ${revision} published` : "Draft"));
    const name = remember(input("agent-edit-name", state.values.name), "name"); name.maxLength = 80; name.required = true;
    const prompt = node("textarea"); prompt.id = "agent-edit-prompt"; prompt.value = state.values.prompt; prompt.maxLength = 8000; prompt.rows = 7; prompt.disabled = state.pending;
    remember(prompt, "prompt");
    const mappings = node("div", "agent-field-row");
    const voice = node("select"); voice.id = "agent-edit-voice"; voice.disabled = state.pending || !state.config?.authenticated;
    voice.append(option("", "Select a voice"));
    for (const item of state.config?.voices || []) {
      const choice = option(item.id, item.name + (item.ready ? "" : ` · ${voiceState(item)}`));
      choice.disabled = !item.ready; voice.append(choice);
    }
    voice.value = state.values.voiceProfileId; remember(voice, "voiceProfileId");
    const slot = node("select"); slot.id = "agent-edit-slot"; slot.disabled = state.pending || !state.config?.authenticated;
    slot.append(option("", "No shortcut"));
    for (let number = 1; number <= 9; number++) {
      const used = (state.config?.agents || []).find(agent => agent.slot === number && agent.id !== state.selected);
      const choice = option(number, `#${number}` + (used ? ` · ${used.name}` : "")); choice.disabled = Boolean(used); slot.append(choice);
    }
    slot.value = state.values.slot; remember(slot, "slot");
    mappings.append(field("Voice", voice), field("Call shortcut", slot));
    const actions = node("div", "agent-editor-actions");
    actions.append(button("Save draft", saveDraft));
    const publish = button("Publish call settings", publishAgent, true); publish.id = "agent-publish";
    publish.disabled = state.pending || !state.config?.authenticated || !state.config?.enabled;
    actions.append(publish);
    panel.append(heading, field("Name", name), field("Prompt", prompt), mappings, actions);
    if (!state.config?.authenticated) panel.append(node("p", "agent-help", "Unlock owner controls to choose a voice and publish call shortcuts."));
    return panel;
  }
  function renderAccess() {
    const panel = node("section", "agent-access");
    const heading = node("div", "agent-section-heading");
    heading.append(node("h3", "", "Owner controls"), node("span", "agent-state", state.config?.authenticated ? "Unlocked" : "Locked"));
    panel.append(heading);
    if (state.config?.authenticated) {
      panel.append(node("p", "agent-help", "Only you can publish agents, add voices, or take over a call."));
      const lock = button("Lock controls", () => perform(async () => {
        await request("/api/agents/session", {method: "DELETE"});
        state.config = {...state.config, authenticated: false, agents: [], voices: []};
        state.sessions = []; state.notice = "Owner controls locked.";
      }));
      panel.append(lock);
      return panel;
    }
    const form = node("form", "agent-unlock-form");
    const code = input("agent-owner-code", "", "password"); code.autocomplete = "one-time-code"; code.required = true;
    code.placeholder = "One-time owner code"; code.setAttribute("aria-label", "One-time owner code");
    const unlock = button("Unlock", () => {}, true); unlock.type = "submit";
    form.append(code, unlock);
    form.addEventListener("submit", event => {
      event.preventDefault(); const value = code.value.trim(); code.value = "";
      if (!value) return;
      perform(async () => {
        state.config = await request("/api/agents/session", {method: "POST", body: {code: value}});
        if (!state.selected && allAgents().length) choose(allAgents()[0].id);
        if (state.selected && !state.dirty) choose(state.selected);
        state.notice = "Owner controls unlocked.";
      });
    });
    panel.append(node("p", "agent-help", "Use the one-time code generated on your server. No API keys are needed here."), form);
    return panel;
  }
  function renderVoices() {
    const panel = node("section", "agent-voices");
    const heading = node("div", "agent-section-heading");
    heading.append(node("h3", "", "Voices"));
    const refresh = button("Refresh voices", () => perform(async () => {
      const response = await request("/api/agents/voices/refresh", {method: "POST", body: {}});
      state.config.voices = response.voices || []; state.notice = "Voice library refreshed.";
    }));
    refresh.id = "agent-refresh-voices";
    refresh.disabled = state.pending || !state.config?.enabled || !state.config?.capabilities?.voiceCatalog;
    heading.append(refresh); panel.append(heading);
    const voices = state.config?.voices || [];
    if (!voices.length) panel.append(node("p", "agent-help", "Refresh to load voices from your ElevenLabs account."));
    for (const voice of voices) {
      const row = node("div", "agent-voice-row");
      row.append(node("span", "", voice.name), node("span", "agent-state", voiceState(voice))); panel.append(row);
    }
    if (!state.config?.capabilities?.voiceCloning || !state.config?.enabled) return panel;
    const details = node("details", "agent-clone"); details.append(node("summary", "", "Add a voice clone"));
    const form = node("form");
    const name = input("agent-clone-name"); name.required = true; name.maxLength = 80;
    const files = input("agent-clone-files", "", "file"); files.accept = ".wav,.mp3,.m4a,.flac,.ogg,audio/wav,audio/mpeg,audio/mp4,audio/flac,audio/ogg"; files.multiple = true; files.required = true;
    const consent = input("agent-clone-consent", "", "checkbox"); consent.required = true;
    const agreement = node("label", "agent-consent");
    agreement.append(consent, node("span", "", "I own this voice or have the speaker’s permission to create and use this clone."));
    const submit = button("Create voice clone", () => {}, true); submit.type = "submit";
    form.append(field("Voice name", name), field("Audio recordings", files), node("p", "agent-help", "Up to 3 recordings, 8 MB each and 16 MB combined. Recordings are sent to ElevenLabs."), agreement, submit);
    form.addEventListener("submit", event => {
      event.preventDefault();
      const samples = Array.from(files.files || []);
      if (!name.value.trim() || !consent.checked || !samples.length) return;
      if (samples.length > 3 || samples.some(file => !file.size || file.size > 8 * 1024 * 1024) || samples.reduce((total, file) => total + file.size, 0) > MAX_UPLOAD_BYTES) {
        state.error = "Choose 1–3 nonempty audio recordings, up to 8 MB each and 16 MB combined."; render(); return;
      }
      const body = new FormData(); body.append("name", name.value.trim()); body.append("consent", "true");
      for (const file of samples) body.append("files", file);
      perform(async () => {
        const voice = await request("/api/agents/voices/clone", {method: "POST", body, multipart: true, timeout: 130000});
        state.config.voices = [...(state.config.voices || []).filter(item => item.id !== voice.id), voice];
        state.notice = voice.ready ? "Voice clone created. Select it in an agent’s settings." : "Voice clone created. Complete verification in ElevenLabs before using it.";
      });
    });
    details.append(form); panel.append(details); return panel;
  }
  async function refreshSessions() {
    const response = await request("/api/operator/sessions");
    state.sessions = response.sessions || [];
    if (!state.sessions.some(session => session.id === state.sessionId)) state.sessionId = state.sessions[0]?.id || "";
    state.callVoiceReady = response.voice_ready === true;
  }
  function renderCalls() {
    const panel = node("section", "agent-call-controls");
    const heading = node("div", "agent-section-heading");
    heading.append(node("h3", "", "Manual call controls"), button("Refresh calls", () => perform(async () => {await refreshSessions();})));
    panel.append(heading, node("p", "agent-help", "Choose a connected operator call. #1–#9 select a published agent; #0 returns control to you."));
    const row = node("div", "agent-field-row");
    const sessions = node("select"); sessions.id = "agent-live-call"; sessions.disabled = state.pending;
    sessions.append(option("", state.sessions.length ? "Select a call" : "No connected operator calls"));
    for (const session of state.sessions) sessions.append(option(session.id, `${session.to || session.canonical_call_sid || session.id} · ${session.mode}`));
    sessions.value = state.sessionId; sessions.addEventListener("change", () => {state.sessionId = sessions.value;});
    const slots = node("select"); slots.id = "agent-live-slot"; slots.disabled = state.pending;
    slots.append(option("", "Select an agent"));
    for (const agent of (state.config?.agents || []).filter(agent => agent.slot).sort((a, b) => a.slot - b.slot)) slots.append(option(agent.slot, `#${agent.slot} · ${agent.name}`));
    slots.value = state.slot; slots.addEventListener("change", () => {state.slot = slots.value;});
    row.append(field("Connected call", sessions), field("Agent", slots));
    const actions = node("div", "agent-editor-actions");
    const takeover = button("Take over call", () => perform(async () => {
      if (state.config?.manualEnabled !== true || !state.callVoiceReady) throw new Error("Manual takeover is not enabled on this server.");
      if (!state.sessionId || !state.slot) throw new Error("Choose a connected call and an agent.");
      await request(`/api/sessions/${encodeURIComponent(state.sessionId)}/takeover`, {method: "POST", body: {slot: state.slot}});
      state.notice = "Takeover requested. The caller hears the introduction before the agent begins.";
      await refreshSessions();
    }), true); takeover.id = "agent-takeover"; takeover.disabled = state.pending || !state.sessions.length || !state.callVoiceReady || state.config?.manualEnabled !== true;
    const release = button("Return to human · #0", () => perform(async () => {
      if (!state.sessionId) throw new Error("Choose a connected call.");
      await request(`/api/sessions/${encodeURIComponent(state.sessionId)}/mode`, {method: "POST", body: {mode: "human"}});
      state.notice = "Human control restored."; await refreshSessions();
    })); release.id = "agent-release"; release.disabled = state.pending || !state.sessions.length;
    actions.append(takeover, release); panel.append(row, actions);
    if (state.sessions.length && (!state.callVoiceReady || state.config?.manualEnabled !== true)) panel.append(node("p", "agent-help", "Voice takeover is not enabled. Human calls stay connected."));
    return panel;
  }
  function render() {
    if (!mount) return;
    const container = node("div", "agents-workspace");
    const heading = node("div", "agent-workspace-heading");
    const intro = node("div"); intro.append(node("h2", "", "Rotary agents"), node("p", "", "Your prompts, voices, and manual call shortcuts."));
    heading.append(intro, button("New agent", () => window.DashboardToolbar?.openCreateAgent(), true));
    const status = node("p", state.error ? "toolbar-error" : "agent-feedback", state.error || state.notice || state.workspaceError);
    status.id = "agent-feedback"; status.setAttribute("role", state.error ? "alert" : "status"); status.hidden = !status.textContent;
    container.append(heading, status);
    const layout = node("div", "agent-layout"); layout.append(renderList(), renderEditor()); container.append(layout, renderAccess());
    if (state.config && !state.config.enabled) container.append(node("p", "agent-help", "Agent management is disabled on this server. Drafts remain available."));
    if (state.config?.authenticated) container.append(renderVoices(), renderCalls());
    const foot = node("p", "agent-help agent-footnote", state.config?.manualEnabled
      ? "Automatic takeover is off. Changes apply when you manually select an agent."
      : "Manual takeover is not enabled yet. Agent settings can be prepared in advance.");
    container.append(foot); mount.replaceChildren(container);
    mount.setAttribute("aria-busy", String(state.pending));
  }
  function editAgent(id) {choose(id); render(); $("agent-edit-name")?.focus();}
  function init() {
    mount = $("agents-view"); if (initialized || !mount) return; initialized = true;
    window.DashboardWorkspace?.subscribe(({snapshot, error, loading}) => {
      state.workspaceError = error || "";
      if (snapshot) {state.drafts = snapshot.agents || []; state.loaded = true;}
      if (!state.selected && state.drafts.length) choose(state.drafts[0].id);
      if (!state.pending) render();
    });
    render();
    loadConfig().then(render).catch(error => {state.error = error.message; render();});
  }
  window.DashboardAgents = {init, editAgent};
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, {once: true});
  else init();
})();
