(() => {
  "use strict";

  const MAX_NOTIFICATIONS = 20;
  const endedStatuses = new Set(["completed", "ended", "closed", "failed", "error", "stopped", "disabled", "absent", "partial"]);
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
  let liveNotice;
  let notificationsButton;
  let notificationPopup;
  let notificationBadge;
  let notificationList;
  let notificationEmpty;
  let notificationError;
  let notificationRenderKey = "";
  let notificationRequest = false;
  let lastNotificationSync = 0;
  const pendingReadIds = new Set();
  const persistentNotifications = typeof window.fetch === "function";
  const seenCallIds = new Set();
  let callNotifications = [];

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

  function notificationCaller(item) {
    return window.DashboardCRM?.findContactByPhone?.(item.phone)?.name || item.phone || "Unknown caller";
  }

  function renderNotifications() {
    if (!notificationsButton) return;
    const unread = callNotifications.filter(item => item.unread).length;
    notificationBadge.hidden = !unread;
    notificationBadge.textContent = String(unread);
    notificationsButton.setAttribute("data-unread", String(unread > 0));
    const label = unread ? `Notifications, ${unread} unread notification${unread === 1 ? "" : "s"}` : "Notifications";
    notificationsButton.setAttribute("aria-label", label);
    notificationsButton.title = label;
    notificationEmpty.hidden = callNotifications.length > 0;
    const key = JSON.stringify(callNotifications.map(item => [item.id, notificationCaller(item), item.phone, item.startedAt, item.active, item.available, item.collection]));
    if (notificationRenderKey === key) return;
    notificationRenderKey = key;
    notificationList.replaceChildren();
    for (const item of callNotifications) {
      const row = node("li", "toolbar-notification");
      const heading = node("div", "toolbar-notification-heading");
      const caller = node("strong", "", notificationCaller(item));
      if (item.phone) caller.title = item.phone;
      heading.append(caller, node("span", "toolbar-notification-state", !item.available ? "Unavailable" : item.active ? "Live" : "Ended"));
      const timestamp = new Date(item.startedAt);
      const time = node("time", "", Number.isFinite(timestamp.getTime())
        ? timestamp.toLocaleString(undefined, {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"}) : "Time unavailable");
      if (Number.isFinite(timestamp.getTime())) time.dateTime = timestamp.toISOString();
      let open;
      if (item.available) {
        open = node("a", "toolbar-notification-open", "Open call →");
        open.href = `#calls/${item.collection === "voicemail" ? "voicemail" : "recent"}/${encodeURIComponent(item.id)}`;
        open.setAttribute("aria-label", `Open call from ${notificationCaller(item)}`);
        open.addEventListener("click", event => {
          if (event.button > 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
          closePopover();
          if (window.DashboardCalls?.openCall) {
            event.preventDefault();
            window.DashboardCalls.openCall(item.id);
          }
        });
      } else open = node("span", "toolbar-notification-unavailable", "Outside recent history");
      row.append(heading, time, open);
      notificationList.append(row);
    }
  }

  function markNotificationsRead() {
    for (const item of callNotifications) {
      if (item.unread && persistentNotifications) pendingReadIds.add(item.id);
      item.unread = false;
    }
    renderNotifications();
    if (pendingReadIds.size) syncNotifications(true);
  }

  async function syncNotifications(force = false) {
    if (!persistentNotifications || notificationRequest || (!force && Date.now() - lastNotificationSync < 5000)) return;
    notificationRequest = true;
    lastNotificationSync = Date.now();
    const readIds = [...pendingReadIds].slice(0, MAX_NOTIFICATIONS);
    for (const id of readIds) pendingReadIds.delete(id);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await window.fetch(readIds.length ? "/api/notifications/read" : "/api/notifications", {
        method: readIds.length ? "POST" : "GET", credentials: "same-origin", cache: "no-store", signal: controller.signal,
        headers: readIds.length ? {"Content-Type": "application/json", "X-Workspace-Request": "1"} : {},
        ...(readIds.length ? {body: JSON.stringify({ids: readIds})} : {})
      });
      if (!response.ok) throw new Error("unavailable");
      const data = await response.json();
      if (!Array.isArray(data.notifications)) throw new Error("invalid");
      const saved = data.notifications.filter(item => item && /^CA[0-9a-fA-F]{32}$/.test(item.id)
        && typeof item.startedAt === "string" && typeof item.phone === "string"
        && typeof item.unread === "boolean").slice(0, MAX_NOTIFICATIONS);
      const ids = new Set(saved.map(item => item.id));
      // A just-arrived live call can beat the server inbox response by one poll.
      const arriving = callNotifications.filter(item => item.active && !ids.has(item.id));
      callNotifications = [...arriving, ...saved].slice(0, MAX_NOTIFICATIONS).map(item => ({...item,
        unread: pendingReadIds.has(item.id) ? false : item.unread}));
      for (const item of saved) seenCallIds.add(item.id);
      notificationError.hidden = true;
      if (activePopover === notificationPopup) markNotificationsRead();
      renderNotifications();
    } catch (_) {
      for (const item of callNotifications) if (readIds.includes(item.id)) item.unread = true;
      notificationError.textContent = readIds.length
        ? "Read status was not saved. Reopen notifications to retry."
        : "Notification history is unavailable. Reconnecting…";
      notificationError.hidden = false;
      renderNotifications();
    } finally {
      clearTimeout(timeout);
      notificationRequest = false;
      if (pendingReadIds.size) syncNotifications(true);
    }
  }

  function setSessions(sessions) {
    if (!Array.isArray(sessions)) return;
    const added = [];
    if (!persistentNotifications) for (const item of callNotifications) item.available = false;
    for (const session of sessions) {
      if (!session || !/^CA[0-9a-fA-F]{32}$/.test(session.call_sid)) continue;
      const detail = session.call_detail || {};
      const active = !session.voicemail_only && !session.ended_at && !detail.ended_at
        && !endedStatuses.has(String(session.status || "").toLowerCase());
      const phone = typeof detail.caller_number === "string" ? detail.caller_number : "";
      const startedAt = detail.started_at || session.started_at || "";
      const existing = callNotifications.find(item => item.id === session.call_sid);
      if (existing) {
        // Metadata can arrive after the call itself; keep the last useful values.
        if (phone) existing.phone = phone;
        if (startedAt) existing.startedAt = startedAt;
        existing.active = active;
        existing.available = true;
        existing.collection = session.voicemail ? "voicemail" : "recent";
      }
      if (seenCallIds.has(session.call_sid)) continue;
      seenCallIds.add(session.call_sid);
      if (active) added.push({id: session.call_sid, phone, startedAt, active, available: true,
        collection: session.voicemail ? "voicemail" : "recent", unread: activePopover !== notificationPopup});
    }
    if (added.length) {
      callNotifications = [...added, ...callNotifications]
        .sort((a, b) => (new Date(b.startedAt).getTime() || 0) - (new Date(a.startedAt).getTime() || 0))
        .slice(0, MAX_NOTIFICATIONS);
      if (liveNotice) liveNotice.textContent = added.length === 1
        ? `New live call from ${notificationCaller(added[0])}. Open notifications to view it.`
        : `${added.length} new live calls. Open notifications to view them.`;
    }
    renderNotifications();
    syncNotifications();
  }

  function openCreateAgent() {
    if (!initialized) init();
    const returnFocus = activeTrigger || document.activeElement;
    closePopover();
    if (window.DashboardAgents?.openCreateAgent) window.DashboardAgents.openCreateAgent(returnFocus);
    else if (liveNotice) liveNotice.textContent = "The agent form is still loading. Try again in a moment.";
  }

  function init() {
    if (initialized) return;
    const mount = document.getElementById("header-actions");
    if (!mount) return;
    initialized = true;
    mount.classList.add("toolbar-actions");
    mount.setAttribute("role", "group");
    mount.setAttribute("aria-label", "Workspace actions");
    notificationsButton = iconButton("Notifications", "bell", "notifications-button");
    notificationBadge = node("span", "toolbar-notification-count");
    notificationBadge.id = "notification-count";
    notificationBadge.hidden = true;
    notificationBadge.setAttribute("aria-hidden", "true");
    notificationsButton.append(notificationBadge);
    const settings = iconButton("Settings", "gear", "settings-button");
    const create = iconButton("Create", "plus", "create-button", "toolbar-create");
    notificationPopup = node("section", "toolbar-popover toolbar-notifications-popover");
    notificationPopup.id = "notifications-popover";
    notificationPopup.hidden = true;
    notificationPopup.setAttribute("aria-label", "Notifications");
    notificationEmpty = node("p", "toolbar-popover-empty", "No new calls yet.");
    notificationError = node("p", "toolbar-popover-empty");
    notificationError.hidden = true;
    notificationError.setAttribute("role", "status");
    notificationList = node("ul", "toolbar-notification-list");
    notificationList.setAttribute("aria-label", "Recent call notifications");
    notificationPopup.append(node("h2", "", "Notifications"), notificationError, notificationEmpty, notificationList);
    const settingsPopup = node("section", "toolbar-popover");
    settingsPopup.id = "settings-popover";
    settingsPopup.hidden = true;
    settingsPopup.setAttribute("aria-label", "Settings");
    settingsPopup.append(node("h2", "", "Workspace settings"), node("p", "", "Edit agent prompts, voices, and call shortcuts from the Agents tab."));
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
    attachPopover(notificationsButton, notificationPopup);
    notificationsButton.addEventListener("click", () => {
      if (activePopover !== notificationPopup) return;
      markNotificationsRead();
    });
    attachPopover(settings, settingsPopup);
    attachPopover(create, createPopup, true);
    mount.append(notificationsButton, settings, create, notificationPopup, settingsPopup, createPopup);
    liveNotice = node("div", "sr-only");
    liveNotice.setAttribute("role", "status");
    liveNotice.setAttribute("aria-live", "polite");
    document.body.append(liveNotice);
    window.addEventListener("dashboard-contacts-changed", renderNotifications);
    window.addEventListener("focus", () => syncNotifications(true));
    renderNotifications();
    syncNotifications(true);
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
  }

  window.DashboardToolbar = {init, openCreateAgent, setSessions};
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, {once: true});
  else init();
})();
