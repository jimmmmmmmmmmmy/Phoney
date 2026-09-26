(() => {
  "use strict";

  const STORAGE_KEY = "hacking-banyons.agent-drafts.v1";
  const MAX_DRAFTS = 50;
  const icons = {
    bell: [{tag: "path", d: "M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"}],
    gear: [{tag: "circle", cx: "12", cy: "12", r: "3"}, {tag: "path", d: "m9 3-.6 2.1-1.7 1-2.1-.5-2 3.4 1.5 1.6v2.8L2.6 15l2 3.4 2.1-.5 1.7 1L9 21h6l.6-2.1 1.7-1 2.1.5 2-3.4-1.5-1.6v-2.8L21.4 9l-2-3.4-2.1.5-1.7-1L15 3Z"}],
    plus: [{tag: "path", d: "M12 5v14M5 12h14"}],
    close: [{tag: "path", d: "m6 6 12 12M6 18 18 6"}],
    contact: [{tag: "circle", cx: "9", cy: "8", r: "3"}, {tag: "path", d: "M3 21v-2a6 6 0 0 1 12 0v2M19 8v6m-3-3h6"}],
    agent: [{tag: "rect", x: "4", y: "7", width: "16", height: "13", rx: "3"}, {tag: "path", d: "M12 3v4M8 12h.01M16 12h.01M9 16h6"}]
  };

  let initialized = false;
  let activePopover = null;
  let activeTrigger = null;
  let draftDialog;
  let draftForm;
  let draftName;
  let draftPrompt;
  let draftError;
  let previousFocus;
  let agentsView;
  let draftList;
  let storageNotice;
  let liveNotice;
  let drafts = [];
  let storageReadFailed = false;

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    for (const [key, value] of Object.entries({viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "1.7", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true"})) svg.setAttribute(key, value);
    for (const definition of icons[name]) {
      const part = document.createElementNS("http://www.w3.org/2000/svg", definition.tag);
      for (const [key, value] of Object.entries(definition)) if (key !== "tag") part.setAttribute(key, value);
      svg.append(part);
    }
    return svg;
  }

  function button(label, className) {
    const element = node("button", className, label);
    element.type = "button";
    return element;
  }

  function iconButton(label, symbol, id, className = "") {
    const element = button(undefined, "toolbar-icon " + className);
    element.id = id;
    element.setAttribute("aria-label", label);
    element.title = label;
    element.append(icon(symbol));
    return element;
  }

  function closePopover(restoreFocus = false) {
    if (!activePopover) return;
    const trigger = activeTrigger;
    activePopover.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    activePopover = null;
    activeTrigger = null;
    if (restoreFocus) trigger.focus();
  }

  function attachPopover(trigger, popup, menu = false) {
    trigger.setAttribute("aria-controls", popup.id);
    trigger.setAttribute("aria-expanded", "false");
    if (menu) trigger.setAttribute("aria-haspopup", "menu");
    trigger.addEventListener("click", () => {
      const wasOpen = activePopover === popup;
      closePopover();
      if (wasOpen) return;
      popup.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
      activePopover = popup;
      activeTrigger = trigger;
      if (menu) popup.querySelector('[role="menuitem"]').focus();
    });
    if (menu) trigger.addEventListener("keydown", event => {
      if (!["ArrowDown", "ArrowUp"].includes(event.key)) return;
      event.preventDefault();
      closePopover();
      popup.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
      activePopover = popup;
      activeTrigger = trigger;
      const items = popup.querySelectorAll('[role="menuitem"]');
      items[event.key === "ArrowUp" ? items.length - 1 : 0].focus();
    });
  }

  function readDrafts() {
    try {
      const stored = window.localStorage.getItem(STORAGE_KEY);
      const parsed = stored ? JSON.parse(stored) : [];
      if (!Array.isArray(parsed) || parsed.length > MAX_DRAFTS || parsed.some(item => !item || typeof item.name !== "string" || typeof item.prompt !== "string" || !item.name.trim() || item.name.length > 80 || item.prompt.length > 8000)) throw new Error("invalid drafts");
      drafts = parsed.map(item => ({name: item.name, prompt: item.prompt, createdAt: typeof item.createdAt === "string" ? item.createdAt : ""}));
    } catch (_) {
      storageReadFailed = true;
    }
  }

  function renderDrafts() {
    draftList.replaceChildren();
    storageNotice.hidden = !storageReadFailed;
    if (!drafts.length) {
      const empty = node("div", "agent-drafts-empty");
      empty.append(node("h3", "", "No agent drafts yet"), node("p", "", "Save a name and outbound prompt to prepare your first agent."));
      draftList.append(empty);
      return;
    }
    for (const draft of drafts) {
      const article = node("article", "agent-draft");
      const heading = node("div", "agent-draft-heading");
      heading.append(node("h3", "", draft.name), node("span", "toolbar-label", "Draft · not connected"));
      const prompt = node("p", "agent-draft-prompt", draft.prompt || "No outbound prompt added.");
      article.append(heading, prompt);
      draftList.append(article);
    }
  }

  function renderAgents() {
    const top = node("div", "agent-workspace-heading");
    const intro = node("div");
    intro.append(node("h2", "", "Rotary agents"), node("p", "", "Prepare the agents and prompts for your conversations."));
    const create = button("New agent", "toolbar-primary-button");
    create.prepend(icon("plus"));
    create.addEventListener("click", openCreateAgent);
    top.append(intro, create);
    const note = node("p", "agent-local-note", "Drafts are saved in this browser only. Provider connections and calling will be added later.");
    storageNotice = node("p", "toolbar-error", "Saved agent drafts could not be read. Browser storage is unavailable or contains unsupported data; existing data has been preserved.");
    storageNotice.setAttribute("role", "status");
    draftList = node("div", "agent-draft-list");
    draftList.setAttribute("aria-label", "Agent drafts");
    const shortcuts = node("section", "agent-shortcuts");
    const shortcutHeading = node("div", "agent-shortcut-heading");
    shortcutHeading.append(node("h3", "", "Call shortcuts"), node("span", "toolbar-label", "Planned"));
    const list = node("dl", "shortcut-list");
    for (const [key, description] of [["#1", "Add an agent to the conversation"], ["#2", "Transfer the conversation to an agent"]]) {
      const row = node("div");
      const term = node("dt");
      term.append(node("kbd", "", key));
      row.append(term, node("dd", "", description));
      list.append(row);
    }
    shortcuts.append(shortcutHeading, list, node("p", "", "Keypad routing and agent connections are not active yet."));
    agentsView.replaceChildren(top, note, storageNotice, draftList, shortcuts);
    renderDrafts();
  }

  function openCreateAgent() {
    if (!initialized) init();
    if (!draftDialog) return;
    previousFocus = activeTrigger || document.activeElement;
    closePopover();
    draftForm.reset();
    draftError.hidden = true;
    draftError.textContent = "";
    if (!draftDialog.open) draftDialog.showModal();
    draftName.focus();
  }

  function buildAgentDialog() {
    draftDialog = node("dialog", "toolbar-dialog");
    draftDialog.id = "create-agent-dialog";
    draftDialog.setAttribute("aria-labelledby", "create-agent-title");
    draftDialog.setAttribute("aria-describedby", "create-agent-description");
    draftForm = node("form", "toolbar-dialog-form");
    const heading = node("div", "toolbar-dialog-heading");
    const title = node("h2", "", "Create agent");
    title.id = "create-agent-title";
    const close = iconButton("Close create agent", "close", "create-agent-close");
    close.addEventListener("click", () => draftDialog.close());
    heading.append(title, close);
    const description = node("p", "toolbar-dialog-description", "Save a draft in this browser. Connect its voice provider later.");
    description.id = "create-agent-description";
    const nameLabel = node("label", "toolbar-field", "Agent name");
    draftName = node("input");
    draftName.id = "agent-name";
    draftName.name = "name";
    draftName.required = true;
    draftName.maxLength = 80;
    draftName.autocomplete = "off";
    draftName.placeholder = "e.g. Admissions assistant";
    nameLabel.append(draftName);
    const promptLabel = node("label", "toolbar-field", "Outbound prompt");
    const optional = node("span", "toolbar-field-optional", "Optional");
    draftPrompt = node("textarea");
    draftPrompt.id = "agent-outbound-prompt";
    draftPrompt.name = "prompt";
    draftPrompt.maxLength = 8000;
    draftPrompt.rows = 5;
    draftPrompt.placeholder = "Describe the agent’s role and how it should start a conversation.";
    promptLabel.append(optional, draftPrompt);
    draftError = node("p", "toolbar-error");
    draftError.hidden = true;
    draftError.setAttribute("role", "alert");
    const actions = node("div", "toolbar-dialog-actions");
    const cancel = button("Cancel", "toolbar-secondary-button");
    cancel.addEventListener("click", () => draftDialog.close());
    const submit = button("Save draft", "toolbar-primary-button");
    submit.type = "submit";
    actions.append(cancel, submit);
    draftForm.append(heading, description, nameLabel, promptLabel, draftError, actions);
    draftDialog.append(draftForm);
    document.body.append(draftDialog);
    draftDialog.addEventListener("close", () => {
      if (previousFocus && previousFocus.isConnected) previousFocus.focus();
    });
    draftForm.addEventListener("submit", event => {
      event.preventDefault();
      draftError.hidden = true;
      const name = draftName.value.trim();
      if (!name) {
        draftError.textContent = "Enter an agent name.";
        draftError.hidden = false;
        draftName.focus();
        return;
      }
      if (storageReadFailed || drafts.length >= MAX_DRAFTS) {
        draftError.textContent = storageReadFailed ? "Agent drafts cannot be saved because browser storage could not be read. No changes were made." : "This browser already has 50 agent drafts. No changes were made.";
        draftError.hidden = false;
        return;
      }
      const next = [...drafts, {name, prompt: draftPrompt.value.trim(), createdAt: new Date().toISOString()}];
      try {
        window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      } catch (_) {
        draftError.textContent = "The draft could not be saved. Allow browser storage or free some space, then try again.";
        draftError.hidden = false;
        return;
      }
      drafts = next;
      renderDrafts();
      draftDialog.close();
      document.getElementById("nav-agents").click();
      document.getElementById("page-title").focus({preventScroll: true});
      previousFocus = null;
      liveNotice.textContent = "Agent draft saved in this browser. Provider connection is pending.";
    });
  }

  function init() {
    if (initialized) return;
    const mount = document.getElementById("header-actions");
    agentsView = document.getElementById("agents-view");
    if (!mount || !agentsView) return;
    initialized = true;
    mount.classList.add("toolbar-actions");
    mount.setAttribute("role", "group");
    mount.setAttribute("aria-label", "Workspace actions");
    const notifications = iconButton("Notifications", "bell", "notifications-button");
    const settings = iconButton("Settings", "gear", "settings-button");
    const create = iconButton("Create", "plus", "create-button", "toolbar-create");
    const notificationPopup = node("section", "toolbar-popover");
    notificationPopup.id = "notifications-popover";
    notificationPopup.hidden = true;
    notificationPopup.setAttribute("aria-label", "Notifications");
    notificationPopup.append(node("h2", "", "Notifications"), node("p", "toolbar-popover-empty", "You’re all caught up."), node("p", "", "No notifications yet."));
    const settingsPopup = node("section", "toolbar-popover");
    settingsPopup.id = "settings-popover";
    settingsPopup.hidden = true;
    settingsPopup.setAttribute("aria-label", "Settings");
    settingsPopup.append(node("h2", "", "Workspace settings"), node("p", "", "Voice provider connections and workspace preferences will be available here later."));
    const createPopup = node("div", "toolbar-popover toolbar-create-menu");
    createPopup.id = "create-menu";
    createPopup.hidden = true;
    createPopup.setAttribute("role", "menu");
    createPopup.setAttribute("aria-labelledby", "create-button");
    const contact = button("New contact", "toolbar-menu-item");
    const agent = button("New agent", "toolbar-menu-item");
    contact.prepend(icon("contact"));
    agent.prepend(icon("agent"));
    for (const item of [contact, agent]) {
      item.setAttribute("role", "menuitem");
      item.tabIndex = -1;
    }
    contact.addEventListener("click", () => {
      const trigger = activeTrigger;
      closePopover();
      if (trigger) trigger.focus();
      if (window.DashboardCRM && typeof window.DashboardCRM.openCreateContact === "function") window.DashboardCRM.openCreateContact();
      else liveNotice.textContent = "The contact form is still loading. Try again in a moment.";
    });
    agent.addEventListener("click", openCreateAgent);
    createPopup.append(contact, agent);
    createPopup.addEventListener("keydown", event => {
      const items = [contact, agent];
      const index = items.indexOf(document.activeElement);
      let next;
      if (event.key === "ArrowDown") next = (index + 1) % items.length;
      else if (event.key === "ArrowUp") next = (index - 1 + items.length) % items.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = items.length - 1;
      else if (event.key === "Tab") {
        closePopover(true);
        return;
      } else return;
      event.preventDefault();
      items[next].focus();
    });
    attachPopover(notifications, notificationPopup);
    attachPopover(settings, settingsPopup);
    attachPopover(create, createPopup, true);
    mount.append(notifications, settings, create, notificationPopup, settingsPopup, createPopup);
    liveNotice = node("div", "sr-only");
    liveNotice.setAttribute("role", "status");
    liveNotice.setAttribute("aria-live", "polite");
    document.body.append(liveNotice);
    document.addEventListener("pointerdown", event => {
      if (activePopover && !activePopover.contains(event.target) && !activeTrigger.contains(event.target)) closePopover();
    });
    document.addEventListener("focusin", event => {
      if (activePopover && !activePopover.contains(event.target) && !activeTrigger.contains(event.target)) closePopover();
    });
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && activePopover) {
        event.preventDefault();
        closePopover(true);
      }
    });
    readDrafts();
    renderAgents();
    buildAgentDialog();
  }

  window.DashboardToolbar = {init, openCreateAgent};
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, {once: true});
  else init();
})();
