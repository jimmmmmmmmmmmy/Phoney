(() => {
  "use strict";

  const state = {drafts: [], workspaceError: "", config: null, configError: "", configLoading: true,
    saving: false, editingId: null, editingRevision: 0, reviewRequired: false, notice: ""};
  let mount, dialog, form, title, nameInput, promptInput, voiceInput, slotInput, errorMessage;
  let saveButton, closeButton, cancelButton, previousFocus, initialized = false, configPromise, dialogGeneration = 0, configEpoch = 0, renderedSignature = "";
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
    control.addEventListener("click", action);
    return control;
  }
  function option(value, text) {
    const element = node("option", "", text); element.value = String(value); return element;
  }
  function field(label, control) {
    const wrapper = node("label", "toolbar-field", label);
    wrapper.append(control); return wrapper;
  }
  function allAgents() {
    const records = new Map(state.drafts.map(agent => [agent.id, agent]));
    for (const agent of state.config?.agents || []) records.set(agent.id, agent);
    return [...records.values()];
  }
  function voiceName(id) {
    return (state.config?.voices || []).find(voice => voice.id === id)?.name || "No voice selected";
  }
  async function request(path, {method = "GET", body} = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(path, {method, credentials: "same-origin", cache: "no-store", signal: controller.signal,
        headers: {Accept: "application/json", ...(method === "GET" ? {} : {"X-Agent-Request": "1", "Content-Type": "application/json"})},
        ...(body === undefined ? {} : {body: JSON.stringify(body)})});
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const message = typeof data.detail === "string" ? data.detail
          : response.status === 401 || response.status === 403 ? "Agent editing is unavailable on this server. Your changes have not been saved."
          : response.status === 409 ? "That call shortcut is already assigned. Choose another shortcut."
          : response.status === 400 || response.status === 422 ? "Review the entered details and try again."
          : `Agent settings could not be ${method === "GET" ? "loaded" : "saved"}. Check your connection and try again.`;
        const error = new Error(message); error.status = response.status; throw error;
      }
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("The request timed out. Retry saving to confirm your changes.");
      throw error;
    } finally { clearTimeout(timer); }
  }
  function loadConfig() {
    if (configPromise) return configPromise;
    state.configLoading = true;
    const epoch = configEpoch;
    configPromise = request("/api/agents/config").then(config => {
      if (epoch !== configEpoch) return state.config;
      const changed = JSON.stringify(state.config) !== JSON.stringify(config);
      state.config = config;
      if (changed && dialog?.open && state.editingId && !state.saving) {
        const current = config.agents?.find(agent => agent.id === state.editingId);
        if (current && current.revision !== state.editingRevision) {
          errorMessage.textContent = "This agent changed in another browser. Your draft is unchanged. Close and reopen the editor to review the latest settings before saving.";
          errorMessage.hidden = false;
        }
      }
      state.configError = "";
      return config;
    }).catch(error => {
      if (epoch !== configEpoch) return state.config;
      state.configError = error.message || "Agent settings could not be loaded. Reload to try again.";
      throw error;
    }).finally(() => {
      state.configLoading = false;
      configPromise = null;
      render();
    });
    return configPromise;
  }
  function render() {
    if (!mount) return;
    const active = document.activeElement, scrollTop = mount.scrollTop;
    const focusId = mount.contains(active) ? active.id : null;
    const container = node("div", "agents-workspace");
    const heading = node("div", "agent-workspace-heading");
    const create = button("New agent", () => openCreateAgent(), true); create.id = "agent-new";
    heading.append(create);
    const feedback = node("p", state.configError ? "toolbar-error" : "agent-feedback", state.configError || state.notice || state.workspaceError);
    feedback.id = "agent-feedback"; feedback.hidden = !feedback.textContent;
    feedback.setAttribute("role", state.configError ? "alert" : "status");
    const list = node("ul", "agents-list"); list.setAttribute("aria-label", "Saved agents");
    const agents = allAgents();
    if (!agents.length) list.append(node("li", "agent-empty", state.configLoading
      ? "Loading agents…" : state.configError ? "Agents could not be loaded. Reload to try again."
      : "No agents yet. Create an agent to choose its prompt, voice, and call shortcut."));
    for (const agent of agents) {
      const row = node("li", "agent-row");
      const details = node("div", "agent-row-details");
      const heading = node("div", "agent-row-heading");
      heading.append(node("h3", "agent-name", agent.name));
      const shortcut = node("span", "agent-shortcut", agent.slot ? `#${agent.slot}` : "No shortcut");
      shortcut.setAttribute("aria-label", agent.slot ? `Call shortcut #${agent.slot}` : "No call shortcut");
      heading.append(shortcut);
      details.append(heading, node("p", "agent-voice", voiceName(agent.voiceProfileId)),
        node("p", "agent-prompt-preview", agent.prompt || "No prompt added."));
      const edit = button("Edit", () => editAgent(agent.id));
      edit.id = `agent-edit-${agent.id}`;
      edit.setAttribute("aria-label", `Edit ${agent.name}`);
      row.append(details, edit); list.append(row);
    }
    container.append(heading, feedback, list);
    // Do not replace unchanged rows on a background refresh (keyboard focus,
    // selection, and scroll position belong to the person using the dashboard).
    const signature = JSON.stringify([agents, state.config?.voices, state.configError, state.notice, state.workspaceError, state.configLoading]);
    if (renderedSignature === signature) return;
    renderedSignature = signature;
    mount.replaceChildren(container); mount.scrollTop = scrollTop;
    if (focusId) $(focusId)?.focus({preventScroll: true});
  }
  function showError(message, control) {
    errorMessage.textContent = message; errorMessage.hidden = false;
    if (control) {control.setAttribute("aria-invalid", "true"); control.focus();}
    else errorMessage.focus();
  }
  function clearError() {
    errorMessage.textContent = ""; errorMessage.hidden = true;
    for (const control of [nameInput, promptInput, voiceInput, slotInput]) control.setAttribute("aria-invalid", "false");
  }
  function setPending(pending) {
    state.saving = pending;
    for (const control of [nameInput, promptInput, voiceInput, slotInput, closeButton, cancelButton, saveButton]) control.disabled = pending;
    saveButton.textContent = pending ? "Saving…" : "Save agent";
    form.setAttribute("aria-busy", String(pending));
  }
  function populateChoices(agent = {}, preserve = false) {
    agent ||= {};
    const selectedVoice = preserve ? voiceInput.value : agent.voiceProfileId;
    const selectedSlot = preserve ? slotInput.value : agent.slot == null ? "" : String(agent.slot);
    const voices = state.config?.voices || [];
    const defaultVoice = state.config?.defaultVoiceClone;
    const defaultId = typeof defaultVoice === "string" ? defaultVoice : defaultVoice?.id || defaultVoice?.voiceProfileId;
    voiceInput.replaceChildren(option("", voices.length ? "Select a voice" : "No voices available"));
    for (const voice of voices) {
      const choice = option(voice.id, voice.name + (voice.ready ? "" : voice.requiresVerification ? " · Verification needed" : " · Unavailable"));
      choice.disabled = !voice.ready; voiceInput.append(choice);
    }
    voiceInput.value = selectedVoice || (preserve ? "" : voices.find(voice => voice.ready && voice.id === defaultId)?.id || voices.find(voice => voice.ready && voice.name.toLowerCase() === "owner")?.id || voices.find(voice => voice.ready)?.id || "");
    slotInput.replaceChildren(option("", "No shortcut"));
    for (let number = 1; number <= 9; number++) {
      const used = (state.config?.agents || []).find(item => item.slot === number && item.id !== state.editingId);
      const choice = option(number, `#${number}` + (used ? ` · ${used.name}` : ""));
      choice.disabled = Boolean(used); slotInput.append(choice);
    }
    slotInput.value = selectedSlot;
  }
  function openAgent(agent, returnFocus) {
    if (!initialized) init();
    if (!dialog || state.saving) return;
    if (dialog.open) {nameInput.focus(); return;}
    const generation = ++dialogGeneration;
    previousFocus = returnFocus || document.activeElement;
    state.editingId = agent?.id || null;
    state.editingRevision = state.config?.agents?.find(item => item.id === agent?.id)?.revision ?? 0;
    state.reviewRequired = false;
    form.reset(); clearError();
    title.textContent = agent ? "Edit agent" : "New agent";
    nameInput.value = agent?.name || "";
    promptInput.value = agent?.prompt || "";
    const initialName = nameInput.value, initialPrompt = promptInput.value;
    populateChoices(agent);
    dialog.showModal(); nameInput.focus();
    if (!state.config) {
      voiceInput.disabled = slotInput.disabled = saveButton.disabled = true;
      loadConfig().then(() => {
        if (dialog.open && generation === dialogGeneration) {
          const canonicalAgent = allAgents().find(item => item.id === agent?.id);
          if (canonicalAgent) {
            const published = state.config?.agents?.find(item => item.id === canonicalAgent.id);
            state.reviewRequired = Boolean(published && (nameInput.value !== initialName || promptInput.value !== initialPrompt));
            state.editingRevision = state.reviewRequired ? 0 : published?.revision ?? 0;
            if (nameInput.value === initialName) nameInput.value = canonicalAgent.name || "";
            if (promptInput.value === initialPrompt) promptInput.value = canonicalAgent.prompt || "";
          }
          populateChoices(canonicalAgent);
          if (state.reviewRequired) showError("Published settings loaded while you were editing. Your draft is unchanged. Close and reopen the editor to review the latest settings before saving.");
        }
      }).catch(error => {if (dialog.open && generation === dialogGeneration) showError(error.message);}).finally(() => {
        if (!state.saving && generation === dialogGeneration) voiceInput.disabled = slotInput.disabled = saveButton.disabled = false;
      });
    }
  }
  function openCreateAgent(returnFocus) {openAgent(null, returnFocus);}
  function editAgent(id) {
    const agent = allAgents().find(item => item.id === id);
    if (agent) openAgent(agent);
  }
  async function saveAgent(event) {
    event.preventDefault(); if (state.saving) return;
    clearError();
    if (state.reviewRequired) return showError("Published settings loaded while you were editing. Your draft is unchanged. Close and reopen the editor to review the latest settings before saving.");
    const name = nameInput.value.trim(), prompt = promptInput.value.trim();
    if (!name) return showError("Enter an agent name.", nameInput);
    if (name.length > 80) return showError("Use at most 80 characters for the name.", nameInput);
    if (!prompt) return showError("Add a prompt for this agent.", promptInput);
    if (prompt.length > 8000) return showError("Use at most 8,000 characters for the prompt.", promptInput);
    if (!state.config?.enabled) return showError("Agent editing is unavailable on this server. Your changes have not been saved.");
    const slot = slotInput.value ? Number(slotInput.value) : null;
    const voiceProfileId = voiceInput.value || null;
    const voice = (state.config.voices || []).find(item => item.id === voiceProfileId);
    if (slot !== null && (!Number.isInteger(slot) || slot < 1 || slot > 9)) return showError("Choose a call shortcut from #1 to #9.", slotInput);
    if (slot !== null && (!voice || !voice.ready)) return showError("Choose an available voice for this call shortcut.", voiceInput);
    if ((state.config.agents || []).some(item => slot !== null && item.slot === slot && item.id !== state.editingId)) {
      return showError("That call shortcut is already assigned. Choose another shortcut.", slotInput);
    }
    // Keep the ID for retries after a timeout or other ambiguous response.
    state.editingId ||= `agent-${crypto.randomUUID()}`;
    setPending(true); configEpoch += 1;
    let saved;
    try {
      saved = await request(`/api/agents/${encodeURIComponent(state.editingId)}`, {method: "PUT", body: {name, prompt, voiceProfileId, slot, expectedRevision: state.editingRevision}});
      state.config.agents = [...(state.config.agents || []).filter(item => item.id !== saved.id), saved];
      state.notice = `${saved.name} saved.`;
    } catch (error) {
      if (error.status === 409) {
        try {
          if (configPromise) await configPromise.catch(() => {});
          await loadConfig(); populateChoices({}, true);
        } catch (_) { /* Preserve the original save error. */ }
      }
      setPending(false); showError(error.message || "The agent could not be saved. Try again."); return;
    }
    setPending(false); render();
    previousFocus = $(`agent-edit-${saved.id}`);
    dialog.close();
    $("nav-agents")?.click();
    $(`agent-edit-${saved.id}`)?.focus();
  }
  function buildDialog() {
    dialog = node("dialog", "toolbar-dialog agent-dialog"); dialog.id = "create-agent-dialog";
    dialog.setAttribute("aria-labelledby", "create-agent-title");
    form = node("form", "toolbar-dialog-form"); form.noValidate = true;
    const heading = node("div", "toolbar-dialog-heading");
    title = node("h2", "", "New agent"); title.id = "create-agent-title";
    closeButton = button("×", () => {if (!state.saving) dialog.close();});
    closeButton.id = "create-agent-close"; closeButton.className = "toolbar-icon agent-dialog-close";
    closeButton.setAttribute("aria-label", "Close agent form");
    heading.append(title, closeButton);
    nameInput = node("input"); nameInput.id = "agent-name"; nameInput.name = "name";
    nameInput.required = true; nameInput.maxLength = 80; nameInput.autocomplete = "off";
    nameInput.placeholder = "e.g. Admissions assistant";
    promptInput = node("textarea"); promptInput.id = "agent-outbound-prompt"; promptInput.name = "prompt";
    promptInput.required = true; promptInput.maxLength = 8000; promptInput.rows = 5;
    promptInput.placeholder = "Describe the agent’s personality and how it should handle the call.";
    voiceInput = node("select"); voiceInput.id = "agent-edit-voice"; voiceInput.name = "voiceProfileId";
    slotInput = node("select"); slotInput.id = "agent-edit-slot"; slotInput.name = "slot";
    const mappings = node("div", "agent-field-row"); mappings.append(field("Voice", voiceInput), field("Call shortcut", slotInput));
    errorMessage = node("p", "toolbar-error"); errorMessage.id = "agent-form-error"; errorMessage.hidden = true;
    errorMessage.tabIndex = -1; errorMessage.setAttribute("role", "alert");
    for (const control of [nameInput, promptInput, voiceInput, slotInput]) control.setAttribute("aria-describedby", errorMessage.id);
    const actions = node("div", "toolbar-dialog-actions");
    cancelButton = button("Cancel", () => {if (!state.saving) dialog.close();}); cancelButton.id = "agent-cancel";
    saveButton = button("Save agent", () => {}, true); saveButton.id = "agent-save"; saveButton.type = "submit";
    actions.append(cancelButton, saveButton);
    form.append(heading, field("Name", nameInput), field("Prompt", promptInput), mappings, errorMessage, actions);
    form.addEventListener("submit", saveAgent); dialog.append(form); document.body.append(dialog);
    dialog.addEventListener("cancel", event => {if (state.saving) event.preventDefault();});
    dialog.addEventListener("close", () => {
      if (previousFocus?.isConnected) previousFocus.focus();
      else ($(`agent-edit-${state.editingId}`) || $("agent-new"))?.focus();
    });
  }
  function init() {
    mount = $("agents-view"); if (initialized || !mount) return; initialized = true;
    buildDialog();
    window.DashboardWorkspace?.subscribe(({snapshot, error, importError}) => {
      state.workspaceError = [error, importError].filter(Boolean).join(" ");
      if (snapshot) state.drafts = snapshot.agents || [];
      render();
    });
    render(); loadConfig().catch(() => {});
    const refreshVisible = () => {
      if (!document.hidden && !state.saving) loadConfig().catch(() => {});
    };
    window.setInterval?.(refreshVisible, 5000);
    window.addEventListener?.("focus", refreshVisible);
    window.addEventListener?.("online", refreshVisible);
    document.addEventListener?.("visibilitychange", refreshVisible);
  }
  window.DashboardAgents = {init, editAgent, openCreateAgent};
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, {once: true});
  else init();
})();
