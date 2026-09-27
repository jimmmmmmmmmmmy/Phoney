(function () {
  "use strict";
  const MAX_CONTACTS = 500;
  const contactCategories = [["all", "All contacts"], ["real-estate", "Real Estate"], ["legal", "Legal"], ["customers", "Customers"]];
  const demoContacts = [
    {id: "demo-alex-morgan", labels: ["Real Estate"], firstName: "Alex", lastName: "Morgan", phone: "+19415550101", email: "alex.morgan@example.com", company: "Cedar House Studio", address: "Sarasota, FL", website: "https://example.com", status: "Follow up", createdAt: "2026-07-06T14:00:00Z", note: "Send the updated consultation times before the next call.", demo: true},
    {id: "demo-maya-patel", labels: ["Customers"], firstName: "Maya", lastName: "Patel", phone: "+19415550102", email: "maya.patel@example.com", company: "Harbor Community Market", address: "Bradenton, FL", status: "Active", createdAt: "2026-07-12T14:00:00Z", note: "Prefers a short afternoon check-in.", demo: true},
    {id: "demo-casey-reed", labels: ["Legal"], firstName: "Casey", lastName: "Reed", phone: "+19415550103", email: "casey.reed@example.com", company: "Northside Workshop", address: "Tampa, FL", status: "Follow up", createdAt: "2026-08-01T14:00:00Z", note: "Confirm the workshop attendee count at the next check-in.", demo: true},
    {id: "demo-jordan-ellis", labels: ["Customers"], firstName: "Jordan", lastName: "Ellis", phone: "+19415550104", email: "jordan.ellis@example.com", company: "Sunroom Books", address: "St. Petersburg, FL", status: "Active", createdAt: "2026-08-20T14:00:00Z", note: "Interested in a follow-up demonstration after the event.", demo: true}
  ];
  const demoCalls = [
    {id: "demo-call-sep08", contactId: "demo-alex-morgan", title: "Consultation follow-up", startedAt: "2026-09-08T18:30:00Z", duration: 284, direction: "Outbound", outcome: "Follow-up needed", summary: "Alex reviewed the consultation options and asked for two revised appointment times. A follow-up is needed once the schedule is confirmed.", transcript: [["New College", "Hi Alex, I’m following up on the consultation we discussed."], ["Alex", "Thanks for calling. I’m interested, but the original time no longer works."], ["New College", "Would a morning appointment or a late afternoon appointment be easier?"], ["Alex", "Late afternoon is best. Could you send me two options?"], ["New College", "Absolutely. We’ll follow up with two afternoon times."], ["Alex", "Perfect, thank you."]]},
    {id: "demo-call-aug22", contactId: "demo-jordan-ellis", title: "Event introduction", startedAt: "2026-08-22T15:15:00Z", duration: 195, direction: "Inbound", outcome: "Completed", summary: "Jordan called to learn about the upcoming community demonstration. Event details were shared, and Jordan requested a later product walkthrough.", transcript: [["New College", "Thanks for calling New College. How can we help?"], ["Jordan", "I saw the community event announcement. Is the demonstration open to local businesses?"], ["New College", "Yes, you’re welcome to attend. We’ll introduce the project and answer questions."], ["Jordan", "That sounds useful. I would also like a more detailed walkthrough afterward."], ["New College", "We’ve noted your interest and can coordinate a follow-up."], ["Jordan", "Great, I’ll look out for the event details."]]},
    {id: "demo-call-aug04", contactId: "demo-casey-reed", title: "Workshop planning", startedAt: "2026-08-04T19:00:00Z", duration: 362, direction: "Outbound", outcome: "Follow-up needed", summary: "Casey discussed the workshop format and estimated a group of eight. The final attendee count still needs confirmation.", transcript: [["New College", "Hi Casey, do you have a few minutes to discuss the workshop?"], ["Casey", "Yes. We’re expecting around eight people, though I’m still waiting on two replies."], ["New College", "We can plan for eight. Would a hands-on session work for your group?"], ["Casey", "Definitely. A short introduction followed by time to try it would be ideal."], ["New College", "That works. Please confirm the final number when you have it."], ["Casey", "I’ll send the final count before our next check-in."]]},
    {id: "demo-call-jul17", contactId: "demo-maya-patel", title: "Welcome conversation", startedAt: "2026-07-17T17:45:00Z", duration: 218, direction: "Inbound", outcome: "Completed", summary: "Maya introduced the market’s community outreach work. Contact details and a preference for afternoon calls were confirmed.", transcript: [["New College", "Hello, thanks for reaching out. Who am I speaking with?"], ["Maya", "This is Maya from Harbor Community Market. I’d like to learn more about your project."], ["New College", "We’re building tools to help teams manage calls and follow-up conversations."], ["Maya", "That could be helpful for our outreach. Afternoon calls are usually easiest for me."], ["New College", "Thanks, we’ve noted that preference and your contact information."], ["Maya", "Thank you. I’m looking forward to hearing more."]]}
  ];
  let localContacts = [], demoOverrides = [], storageWarning = "", realCalls = [], lastSessions = [], revision = 0, renderedKey = "";
  let listFilter = "all", searchText = "", root, dialog, form, opener, successMessage = "", savedContactId = "", listUI = null, renderedRoute = "";
  let editingContactId = null, savingContact = false, draftContactId = null, workspaceApplied = false;
  const expandedCalls = new Set();
  const callerHistories = new Map();
  let callerMetrics = new Map(), callerMetricsComplete = false;
  const contactStatuses = ["New", "Active", "Follow up"];
  const phonePattern = /^\+[1-9][0-9]{7,14}$/;
  const iconPaths = {pencil: ["m15 5 4 4M4 20l4-1L20 7a2.8 2.8 0 0 0-4-4L4 15v5Z"], plus: ["M12 5v14M5 12h14"], close: ["m6 6 12 12M6 18 18 6"], search: ["m16 16 5 5"], chevron: ["m6 9 6 6 6-6"], phone: ["M7 3H4a1 1 0 0 0-1 1c0 9.4 7.6 17 17 17a1 1 0 0 0 1-1v-3l-5-2-2 2a13 13 0 0 1-7-7l2-2-2-5Z"], email: ["M3 5h18v14H3z", "m3 6 9 7 9-7"], address: ["M20 10c0 6-8 11-8 11S4 16 4 10a8 8 0 1 1 16 0Z"], website: ["M3 12h18M12 3c5 5 5 13 0 18-5-5-5-13 0-18Z"]};
  function node(tag, className, text) { const el = document.createElement(tag); if (className) el.className = className; if (text != null) el.textContent = String(text); return el; }
  function icon(name) { const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"); for (const [key, value] of Object.entries({viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "1.7", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", class: "crm-icon"})) svg.setAttribute(key, value); for (const d of iconPaths[name] || []) { const p = document.createElementNS(svg.namespaceURI, "path"); p.setAttribute("d", d); svg.append(p); } if (["search", "address", "website"].includes(name)) { const c = document.createElementNS(svg.namespaceURI, "circle"); c.setAttribute("cx", name === "search" ? "10.5" : "12"); c.setAttribute("cy", name === "search" ? "10.5" : name === "address" ? "10" : "12"); c.setAttribute("r", name === "search" ? "6.5" : name === "address" ? "2.5" : "9"); svg.append(c); } return svg; }
  function button(text, className, action) { const b = node("button", className, text); b.type = "button"; if (action) b.addEventListener("click", action); return b; }
  function link(text, href, className) { const a = node("a", className, text); a.href = href; return a; }
  function fullName(contact) { return `${contact.firstName} ${contact.lastName}`; }
  function badge() { return node("span", "crm-demo", "Demo"); }
  function contactLabels(contact) { return (contact.labels || []).map(label => node("span", "crm-label", label)); }
  function initials(contact) { return (contact.firstName.slice(0, 1) + contact.lastName.slice(0, 1)).toUpperCase(); }
  function contacts() { return [...localContacts, ...demoContacts.map(contact => ({...contact, ...demoOverrides.find(override => override.id === contact.id), demo: true}))]; }
  function normalizePhone(value) { return typeof value === "string" ? value.replace(/[\s().-]/g, "") : ""; }
  function findContactByPhone(rawPhone) {
    const phone = normalizePhone(rawPhone);
    if (!phonePattern.test(phone)) return null;
    const contact = contacts().find(candidate => candidate.phone === phone);
    return contact ? {id: contact.id, name: fullName(contact), phone: contact.phone} : null;
  }
  function notifyContactsChanged() { window.dispatchEvent(new CustomEvent("dashboard-contacts-changed")); }
  function date(value, withYear = true) { const parsed = new Date(value); return Number.isFinite(parsed.getTime()) ? parsed.toLocaleDateString("en-US", {month: "short", day: "numeric", ...(withYear ? {year: "numeric"} : {})}) : "—"; }
  function dateTime(value) { const parsed = new Date(value); return Number.isFinite(parsed.getTime()) ? parsed.toLocaleString("en-US", {month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit"}) : "—"; }
  function duration(seconds) { if (!Number.isFinite(seconds)) return "—"; return seconds < 60 ? `${Math.round(seconds)}s` : `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`; }
  function callsFor(contact) {
    const archived = callerHistories.get(contact.phone)?.calls || [];
    const calls = new Map([...demoCalls.filter(c => c.contactId === contact.id), ...archived,
      ...realCalls.filter(c => c.phone === contact.phone)].map(call => [call.id, call]));
    return [...calls.values()].sort((a, b) => new Date(b.startedAt) - new Date(a.startedAt));
  }
  function contactMetrics(contact) {
    const history = callsFor(contact), archive = callerHistories.get(contact.phone);
    const shared = callerMetrics.get(contact.phone);
    const source = shared || (callerMetricsComplete ? {total: 0, duration_seconds: 0} : archive);
    const demos = demoCalls.filter(call => call.contactId === contact.id);
    const complete = source?.complete !== false && Number.isInteger(source?.total);
    const completeDuration = complete && Number.isFinite(source.duration_seconds);
    const lastContact = [source?.last_contact_at, history[0]?.startedAt].filter(Boolean)
      .sort((a, b) => new Date(b) - new Date(a))[0];
    return {count: complete ? source.total + demos.length : history.length, complete, completeDuration,
      duration: completeDuration ? source.duration_seconds + demos.reduce((total, call) => total + (call.duration || 0), 0)
        : history.reduce((total, call) => total + (call.duration || 0), 0), lastContact};
  }
  function setHistoryMetrics(metrics, {complete = true, caller = ""} = {}) {
    if (!metrics || typeof metrics !== "object" || Array.isArray(metrics)) return;
    const next = caller ? new Map(callerMetrics) : new Map();
    for (const [phone, value] of Object.entries(metrics)) {
      if (!phonePattern.test(phone) || !value || !Number.isInteger(value.total) || value.total < 0) continue;
      next.set(phone, {total: value.total, duration_seconds: value.duration_seconds,
        last_contact_at: value.last_contact_at || null, complete});
    }
    if (caller && complete && !Object.hasOwn(metrics, caller)) next.set(caller, {total: 0, duration_seconds: 0, last_contact_at: null, complete: true});
    const allComplete = caller ? callerMetricsComplete : complete;
    if (JSON.stringify([...next]) !== JSON.stringify([...callerMetrics]) || allComplete !== callerMetricsComplete) {
      callerMetrics = next; callerMetricsComplete = allComplete; revision += 1;
    }
  }
  function callerHistory(phone) {
    if (!callerHistories.has(phone)) callerHistories.set(phone,
      {calls: [], loaded: false, loading: false, error: "", has_more: false, next_cursor: null});
    return callerHistories.get(phone);
  }
  async function loadContactHistory(contact, older = false) {
    if (!window.DashboardCalls?.fetchHistory) return;
    const history = callerHistory(contact.phone);
    if (history.loading || (older && (!history.has_more || !history.next_cursor))) return;
    const restoreFocus = document.activeElement?.id === "crm-load-history";
    const cursor = older ? history.next_cursor : "";
    history.loading = true; history.error = "";
    revision += 1; render();
    try {
      const page = await window.DashboardCalls.fetchHistory({caller: contact.phone,
        cursor});
      if (page.history.has_more && page.history.next_cursor === cursor) throw new Error("Call history could not advance. Reload to try again.");
      const calls = new Map(history.calls.map(call => [call.id, call]));
      for (const session of page.sessions) {
        if (normalizePhone(session.call_detail?.caller_number) !== contact.phone) continue;
        const call = callFromSession(session); if (call) calls.set(call.id, call);
      }
      history.calls = [...calls.values()];
      Object.assign(history, page.history, {loaded: true});
      setHistoryMetrics({[contact.phone]: page.history}, {complete: page.history.complete !== false, caller: contact.phone});
    } catch (failure) {history.error = failure.message || "Call history could not be loaded. Try again.";}
    finally {
      history.loading = false; revision += 1; render();
      if (restoreFocus && window.location.hash === `#contacts/${contact.id}`) {
        (document.getElementById("crm-load-history") || document.getElementById("crm-profile-title"))?.focus({preventScroll: true});
      }
    }
  }
  function metric(label, value, caption) { const box = node("div", "crm-metric"), dd = node("dd", "", value); if (caption) dd.append(node("small", "", caption)); box.append(node("dt", "", label), dd); return box; }
  function status(contact) { return node("span", `crm-status ${contact.status === "Follow up" ? "crm-status-followup" : contact.status === "New" ? "crm-status-new" : ""}`, contact.status); }
  function validContactFields(value) {
    return value && typeof value === "object" && ["firstName", "lastName"].every(key => typeof value[key] === "string" && value[key].trim() && value[key].length <= 80) &&
      typeof value.phone === "string" && phonePattern.test(value.phone) && typeof value.createdAt === "string" && Number.isFinite(Date.parse(value.createdAt)) &&
      ["email", "address", "website", "company"].every(key => value[key] === undefined || (typeof value[key] === "string" && value[key].length <= 400)) &&
      (!value.website || /^https?:\/\//i.test(value.website)) && (value.status === undefined || contactStatuses.includes(value.status)) &&
      (value.labels === undefined || (Array.isArray(value.labels) && value.labels.length <= 10 && value.labels.every(label => typeof label === "string" && label.trim() && label.length <= 40)));
  }
  function validStoredContact(value) { return validContactFields(value) && /^local-[a-zA-Z0-9-]{8,80}$/.test(value.id); }
  function storedContact(value, defaults = {}) {
    return {...defaults, id: value.id, firstName: value.firstName.trim(), lastName: value.lastName.trim(), phone: value.phone,
      email: value.email || "", address: value.address || "", website: value.website || "", company: value.company || "",
      createdAt: value.createdAt, status: value.status || defaults.status || "New",
      labels: [...new Set((value.labels || defaults.labels || []).map(label => label.trim()))], demo: Boolean(defaults.demo)};
  }
  function applyWorkspace(state) {
    const previousWarning = storageWarning;
    storageWarning = state.error || state.importError || (state.loading && !state.snapshot ? "Loading workspace contacts…" : "");
    const data = state.snapshot;
    if (data) {
      if (!Array.isArray(data.contacts) || !data.contacts.every(validStoredContact)
        || !Array.isArray(data.demoOverrides) || !data.demoOverrides.every(value => validContactFields(value)
          && demoContacts.some(contact => contact.id === value.id))) {
        storageWarning = "Workspace contacts could not be read. Reload the dashboard and try again.";
      } else {
        const nextContacts = data.contacts.map(contact => storedContact(contact));
        const nextOverrides = data.demoOverrides.map(contact => storedContact(contact, demoContacts.find(original => original.id === contact.id)));
        if (!workspaceApplied || JSON.stringify([nextContacts, nextOverrides]) !== JSON.stringify([localContacts, demoOverrides])) {
          localContacts = nextContacts;
          demoOverrides = nextOverrides;
          workspaceApplied = true;
          setSessions(lastSessions);
          revision += 1;
          notifyContactsChanged();
        }
      }
    }
    if (storageWarning !== previousWarning) { revision += 1; listUI = null; }
    render();
  }
  function filteredContacts() {
    const query = searchText.trim().toLowerCase();
    const category = contactCategories.find(([key]) => key === listFilter)?.[1];
    return contacts().filter(contact => (listFilter === "all" || (contact.labels || []).includes(category)) &&
      (!query || [fullName(contact), contact.phone, contact.email, contact.company].some(value => String(value || "").toLowerCase().includes(query))));
  }
  function renderTable(container, count) {
    const matches = filteredContacts();
    container.replaceChildren();
    count.textContent = `${matches.length} ${matches.length === 1 ? "contact" : "contacts"}`;
    if (!matches.length) {
      const empty = node("div", "crm-empty");
      empty.append(node("strong", "", "No contacts found"), node("p", "", searchText || listFilter !== "all" ? "Try another search or view all contacts." : "Create a contact to get started."));
      container.append(empty);
      return;
    }
    const table = node("table", "crm-table"), thead = node("thead"), hr = node("tr");
    table.setAttribute("aria-label", "Contacts and call activity");
    for (const [label, cls] of [["Contact", ""], ["Type", "crm-type-column"], ["Phone number", "crm-phone-column"], ["Status", "crm-status-column"], ["Calls", ""], ["Last contact", ""]]) {
      const th = node("th", cls, label);
      th.scope = "col";
      hr.append(th);
    }
    thead.append(hr);
    const tbody = node("tbody");
    for (const contact of matches) {
      const activity = contactMetrics(contact), row = node("tr"), cell = node("td"), person = node("div", "crm-person"), body = node("div"), name = node("div", "crm-person-name");
      name.append(link(fullName(contact), `#contacts/${contact.id}`, "crm-link"));
      body.append(name, node("div", "crm-person-sub", contact.email || contact.phone));
      person.append(node("span", "crm-avatar", initials(contact)), body);
      cell.append(person);
      const typeCell = node("td", "crm-type-column"), labels = contactLabels(contact);
      if (labels.length) {
        const types = node("div", "crm-type-labels");
        types.append(...labels);
        typeCell.append(types);
      } else typeCell.append(node("span", "crm-muted", "—"));
      const statusCell = node("td", "crm-status-column");
      statusCell.append(status(contact));
      row.append(cell, typeCell, node("td", "crm-phone-column", contact.phone), statusCell, node("td", "", activity.count), node("td", "crm-muted", activity.lastContact ? date(activity.lastContact, false) : "No calls yet"));
      tbody.append(row);
    }
    table.append(thead, tbody);
    container.append(table);
  }
  function renderList() {
    const section = node("div", "crm"), topline = node("div", "crm-topline");
    const filters = node("div", "crm-filters"), panel = node("div");
    filters.setAttribute("role", "tablist");
    filters.setAttribute("aria-label", "Contact categories");
    panel.id = "crm-contact-list";
    panel.setAttribute("role", "tabpanel");
    const tableWrap = node("div", "crm-table-wrap"), resultCount = node("p", "crm-result-count");
    resultCount.setAttribute("aria-live", "polite");
    function selectCategory(key, filter) {
      listFilter = key;
      for (const sibling of filters.children) {
        const selected = sibling === filter;
        sibling.setAttribute("aria-selected", String(selected));
        sibling.tabIndex = selected ? 0 : -1;
      }
      panel.setAttribute("aria-labelledby", filter.id);
      renderTable(tableWrap, resultCount);
    }
    for (const [key, label] of contactCategories) {
      const filter = button(label, "crm-filter", () => selectCategory(key, filter));
      const selected = listFilter === key;
      filter.id = `crm-category-${key}`;
      filter.setAttribute("role", "tab");
      filter.setAttribute("aria-controls", panel.id);
      filter.setAttribute("aria-selected", String(selected));
      filter.tabIndex = selected ? 0 : -1;
      if (selected) panel.setAttribute("aria-labelledby", filter.id);
      filter.addEventListener("keydown", event => {
        const tabs = [...filters.children], index = tabs.indexOf(filter);
        let next;
        if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
        else if (event.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length;
        else if (event.key === "Home") next = 0;
        else if (event.key === "End") next = tabs.length - 1;
        else return;
        event.preventDefault();
        tabs[next].click();
        tabs[next].focus();
      });
      filters.append(filter);
    }
    const add = button("New contact", "crm-button crm-button-primary", openCreateContact);
    add.prepend(icon("plus"));
    topline.append(filters, add);
    section.append(topline);
    if (successMessage) {
      const success = node("p", "crm-save-status", successMessage);
      success.setAttribute("role", "status");
      section.append(success);
    }
    if (storageWarning) section.append(node("p", "crm-error", storageWarning));
    const search = node("label", "crm-search"), input = node("input");
    input.type = "search";
    input.placeholder = "Search by name, phone, or email";
    input.setAttribute("aria-label", "Search contacts");
    input.value = searchText;
    input.addEventListener("input", () => { searchText = input.value; renderTable(tableWrap, resultCount); });
    search.append(icon("search"), input);
    panel.append(search, tableWrap, resultCount);
    section.append(panel);
    renderTable(tableWrap, resultCount);
    listUI = {tableWrap, resultCount};
    return section;
  }
  function historyCard(call, contact) { const item = node("details", "crm-call"); item.open = expandedCalls.has(call.id); item.addEventListener("toggle", () => { if (item.open) expandedCalls.add(call.id); else expandedCalls.delete(call.id); }); const summary = node("summary"), callIcon = node("span", "crm-call-icon"), main = node("div", "crm-call-main"), meta = node("div", "crm-call-meta"), chevron = icon("chevron"); summary.id = `crm-summary-${call.id}`; chevron.classList.add("crm-call-chevron"); callIcon.append(icon("phone")); meta.append(node("span", "", dateTime(call.startedAt)), node("span", "", "·"), node("span", "", call.direction), node("span", "", "·"), node("span", "", call.outcome)); main.append(node("div", "crm-call-title", call.title), meta); summary.append(callIcon, main, node("span", "crm-call-duration", duration(call.duration)), chevron); const content = node("div", "crm-call-content"); content.append(node("p", "crm-call-summary", call.summary || "No summary available yet.")); if (call.real) {
      const transcriptLink = link("Open call transcript →", `#calls/${call.collection || "recent"}/${call.id}`, "crm-button");
      transcriptLink.addEventListener("click", event => {
        if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || !window.DashboardCalls?.openCall) return;
        event.preventDefault();
        window.DashboardCalls.openCall(call.id);
      });
      content.append(transcriptLink);
    } else { content.append(node("p", "crm-conversation-note", "Sample transcript · Fictional conversation · No audio recording")); const transcript = node("div", "crm-transcript"); transcript.setAttribute("aria-label", `Sample transcript with ${fullName(contact)}`); for (const [speaker, text] of call.transcript) { const turn = node("div", "crm-turn"); turn.append(node("strong", "", speaker), node("p", "", text)); transcript.append(turn); } content.append(transcript); } item.append(summary, content); return item; }
  function detailItem(label, value) { const item = node("div"), dd = node("dd"); dd.append(value instanceof Node ? value : document.createTextNode(value || "—")); item.append(node("dt", "", label), dd); return item; }
  function renderProfile(contact) {
    const section = node("div", "crm"), heading = node("div", "crm-profile-heading");
    heading.append(link("← All contacts", "#contacts", "crm-link crm-back"));
    const title = node("div", "crm-profile-title"), titleText = node("div"), titleLine = node("div", "crm-profile-name");
    const profileTitle = node("h2", "", fullName(contact));
    profileTitle.id = "crm-profile-title";
    profileTitle.tabIndex = -1;
    titleLine.append(profileTitle);
    if (contact.demo) titleLine.append(badge());
    titleText.append(titleLine, node("p", "crm-profile-subtitle", contact.company || contact.phone));
    title.append(node("span", "crm-avatar", initials(contact)), titleText);
    heading.append(title);
    section.append(heading);
    if (successMessage && contact.id === savedContactId) {
      const saved = node("p", "crm-save-status", successMessage);
      saved.setAttribute("role", "status");
      section.append(saved);
    }
    const history = callsFor(contact), archive = callerHistory(contact.phone), activity = contactMetrics(contact);
    const completeCount = activity.complete ? activity.count : null;
    const partial = window.DashboardCalls?.fetchHistory && (!archive.loaded || archive.has_more || archive.complete === false);
    const metrics = node("dl", "crm-metrics crm-profile-metrics");
    metrics.append(metric(!activity.complete && partial ? "Conversations loaded" : "Conversations", activity.count),
      metric(!activity.completeDuration && partial ? "Talk time loaded" : "Talk time", duration(activity.duration)),
      metric("Last contact", activity.lastContact ? date(activity.lastContact, false) : "—"));
    heading.append(metrics);
    const layout = node("div", "crm-profile-layout"), main = node("section"), callHeading = node("div", "crm-section-title"), historyList = node("div", "crm-history");
    callHeading.append(node("h3", "", "Conversations"), node("span", "", `${history.length} ${history.length === 1 ? "call" : "calls"}`));
    main.append(callHeading);
    if (history.length) history.forEach(call => historyList.append(historyCard(call, contact)));
    else {
      const empty = node("div", "crm-empty");
      empty.append(node("strong", "", "No conversations yet"), node("p", "", "Calls from this phone number will appear here when available."));
      historyList.append(empty);
    }
    main.append(historyList);
    if (archive.loading || archive.error || archive.has_more || archive.complete === false) {
      const controls = node("div", "crm-history-controls");
      const status = node("p", archive.error ? "crm-error" : "crm-muted", archive.error || (archive.loading
        ? "Loading call history…" : archive.complete === false ? "Some saved calls could not be loaded. History may be incomplete." : `${history.length} ${history.length === 1 ? "conversation" : "conversations"} loaded${completeCount === null ? "." : ` of ${completeCount}.`}`));
      status.setAttribute("role", "status"); controls.append(status);
      const more = button(archive.loading ? "Loading…" : archive.error ? "Retry loading calls" : "Load older calls", "crm-button",
        () => loadContactHistory(contact, archive.loaded));
      more.id = "crm-load-history"; more.disabled = archive.loading;
      if (archive.loading || archive.error || archive.has_more) controls.append(more);
      main.append(controls);
    }
    if (contact.note) {
      const note = node("div", "crm-note");
      note.append(node("strong", "", "Next step"), node("p", "", contact.note));
      main.append(note);
    }
    const aside = node("section", "crm-profile-details"), detailsHeading = node("div", "crm-section-title"), details = node("dl", "crm-details");
    const edit = button("", "crm-icon-button crm-edit-button", () => openEditContact(contact.id));
    edit.id = "crm-edit-contact";
    edit.setAttribute("aria-label", "Edit contact details");
    edit.title = "Edit contact details";
    edit.append(icon("pencil"));
    detailsHeading.append(node("h3", "", "Contact details"), edit);
    details.append(detailItem("Phone number", contact.phone), detailItem("Email", contact.email), detailItem("Address", contact.address), detailItem("Website", contact.website),
      ...(contact.company ? [detailItem("Company", contact.company)] : []), detailItem("Contact since", date(contact.createdAt)), detailItem("Status", status(contact)),
      ...((contact.labels || []).length ? [detailItem("Labels", contact.labels.join(", "))] : []));
    aside.append(detailsHeading, details);
    layout.append(main, aside);
    section.append(layout);
    return section;
  }
  function render() {
    if (!root) return;
    const route = window.location.hash;
    const parts = route.slice(1).split("/");
    if (parts[0] !== "contacts") {renderedRoute = ""; renderedKey = ""; return;}
    const key = `${route}|${revision}`;
    if (key === renderedKey) return;
    const sameRoute = renderedRoute === route;
    const scrollTop = root.scrollTop;
    const active = document.activeElement;
    const restoreFocus = sameRoute && root.contains(active) ? {id: active.id, href: active.getAttribute("href")} : null;
    renderedKey = key;
    renderedRoute = route;
    const contact = contacts().find(candidate => candidate.id === parts[1]);
    if (contact && window.DashboardCalls?.fetchHistory) {
      const history = callerHistory(contact.phone);
      if (!history.loading && ((!history.loaded && !history.error) || !sameRoute)) {
        loadContactHistory(contact);
        return;
      }
    }
    if (sameRoute && !parts[1] && listUI && root.contains(listUI.tableWrap)) {
      renderTable(listUI.tableWrap, listUI.resultCount);
    } else if (parts[1] && !contact) {
      const missing = node("div", "crm crm-empty");
      missing.append(node("strong", "", "Contact not found"), node("p", "", storageWarning || "This contact is not in the workspace. Return to the contacts list or reload to try again."), link("Back to contacts", "#contacts", "crm-button"));
      root.replaceChildren(missing);
    } else root.replaceChildren(contact ? renderProfile(contact) : renderList());
    if (restoreFocus && !active.isConnected) {
      const target = restoreFocus.id ? document.getElementById(restoreFocus.id) : [...root.querySelectorAll("a[href]")].find(anchor => anchor.getAttribute("href") === restoreFocus.href);
      target?.focus({preventScroll: true});
    }
    root.scrollTop = sameRoute ? scrollTop : 0;
  }
  function field(key, label, type, placeholder, required, optional) {
    const wrap = node("div", "crm-field"), labelEl = node("label", "", label);
    labelEl.htmlFor = `crm-${key}`;
    const input = node(key === "address" ? "textarea" : key === "status" ? "select" : "input");
    input.id = `crm-${key}`;
    input.name = key;
    if (input.tagName === "INPUT") input.type = type;
    if (key === "status") for (const value of contactStatuses) { const option = node("option", "", value); option.value = value; input.append(option); }
    input.placeholder = placeholder || "";
    input.required = Boolean(required);
    input.maxLength = key === "labels" ? 418 : ["address", "website", "company"].includes(key) ? 400 : key === "email" ? 254 : key === "phone" ? 40 : 80;
    input.autocomplete = {firstName: "given-name", lastName: "family-name", phone: "tel", email: "email", address: "street-address", website: "url", company: "organization"}[key];
    if (optional) {
      const labelRow = node("div", "crm-field-label");
      labelRow.append(labelEl, button("Remove", "crm-remove", () => { wrap.remove(); syncAdditionalMenu(); additionalToggle.focus(); }));
      wrap.append(labelRow);
    } else wrap.append(labelEl);
    wrap.append(input);
    return wrap;
  }
  const optionalContactFields = [["email", "Email", "email", "alex@example.com"], ["address", "Address", "text", "Street, city, state, postal code"], ["website", "Website", "url", "https://example.com"]];
  let optionalFields, editFields, additional, additionalToggle, additionalMenu, errorMessage, dialogTitle, dialogDescription, dialogClose, submitButton;
  function closeAdditionalMenu() { additionalMenu.hidden = true; additionalToggle.setAttribute("aria-expanded", "false"); }
  function syncAdditionalMenu() { for (const item of additionalMenu.children) item.disabled = Boolean(form.elements.namedItem(item.dataset.field)); }
  function buildDialog() {
    dialog = node("dialog", "crm-dialog");
    dialog.id = "crm-create-contact";
    dialog.setAttribute("aria-labelledby", "crm-dialog-title");
    dialog.setAttribute("aria-describedby", "crm-dialog-description");
    form = node("form");
    form.noValidate = true;
    const header = node("div", "crm-dialog-header"), text = node("div");
    dialogTitle = node("h2", "", "Create contact");
    dialogDescription = node("p", "", "Keep contact details and conversations together in this workspace.");
    dialogTitle.id = "crm-dialog-title";
    dialogDescription.id = "crm-dialog-description";
    text.append(dialogTitle, dialogDescription);
    dialogClose = button("", "crm-icon-button", closeDialog);
    dialogClose.setAttribute("aria-label", "Close create contact");
    dialogClose.append(icon("close"));
    header.append(text, dialogClose);
    const body = node("div", "crm-form-body"), nameGrid = node("div", "crm-field-grid");
    nameGrid.append(field("firstName", "First name", "text", "Alex", true), field("lastName", "Last name", "text", "Morgan", true));
    const phone = field("phone", "Phone number", "tel", "+1 941 555 0123", true), phoneHelp = node("span", "crm-field-help", "Include the country code, for example +1 for the United States.");
    phoneHelp.id = "crm-phone-help";
    phone.querySelector("input").setAttribute("aria-describedby", phoneHelp.id);
    phone.append(phoneHelp);
    optionalFields = node("div");
    editFields = node("div", "crm-edit-fields");
    editFields.hidden = true;
    additional = node("div", "crm-additional");
    additionalToggle = button("Additional contact info", "crm-additional-toggle", () => {
      const opening = additionalMenu.hidden;
      additionalMenu.hidden = !opening;
      additionalToggle.setAttribute("aria-expanded", String(opening));
      if (opening) additionalMenu.querySelector("button:not(:disabled)")?.focus();
    });
    additionalToggle.prepend(icon("plus"));
    additionalToggle.append(icon("chevron"));
    additionalToggle.setAttribute("aria-controls", "crm-additional-menu");
    additionalToggle.setAttribute("aria-expanded", "false");
    additionalMenu = node("div", "crm-additional-menu");
    additionalMenu.id = "crm-additional-menu";
    additionalMenu.hidden = true;
    additionalMenu.setAttribute("aria-label", "Additional contact fields");
    for (const [key, label, type, placeholder] of optionalContactFields) {
      const option = button(label, "", () => {
        optionalFields.append(field(key, label, type, placeholder, false, true));
        syncAdditionalMenu();
        closeAdditionalMenu();
        form.elements.namedItem(key).focus();
      });
      option.dataset.field = key;
      option.prepend(icon(key));
      additionalMenu.append(option);
    }
    additional.append(additionalToggle, additionalMenu);
    errorMessage = node("p", "crm-error crm-form-error");
    errorMessage.hidden = true;
    errorMessage.setAttribute("role", "alert");
    body.append(nameGrid, phone, optionalFields, editFields, additional, errorMessage);
    const footer = node("div", "crm-dialog-footer");
    submitButton = button("Create contact", "crm-button crm-button-primary");
    submitButton.type = "submit";
    footer.append(button("Cancel", "crm-button", closeDialog), submitButton);
    form.append(header, body, footer);
    dialog.append(form);
    document.body.append(dialog);
    form.addEventListener("submit", saveContact);
    dialog.addEventListener("cancel", event => { if (savingContact) event.preventDefault(); closeAdditionalMenu(); });
    dialog.addEventListener("close", () => { closeAdditionalMenu(); editingContactId = null; opener?.focus({preventScroll: true}); });
    dialog.addEventListener("click", event => {
      if (event.target === dialog) {
        const rect = dialog.getBoundingClientRect();
        if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeDialog();
      }
      if (!additional.contains(event.target)) closeAdditionalMenu();
    });
    dialog.addEventListener("keydown", event => {
      if (event.key === "Escape" && !additionalMenu.hidden) {
        event.preventDefault(); event.stopPropagation(); closeAdditionalMenu(); additionalToggle.focus();
      }
    });
  }
  function closeDialog() { if (!savingContact && dialog.open) dialog.close(); }
  function prepareDialog(contact) {
    if (savingContact) return;
    draftContactId = contact?.id || `local-${crypto.randomUUID()}`;
    opener = document.activeElement;
    form.reset();
    optionalFields.replaceChildren();
    editFields.replaceChildren();
    editingContactId = contact?.id || null;
    dialogTitle.textContent = contact ? "Edit contact" : "Create contact";
    submitButton.textContent = contact ? "Save changes" : "Create contact";
    dialogDescription.textContent = contact ? "Update contact details and labels." : "Keep contact details and conversations together in this workspace.";
    dialogClose.setAttribute("aria-label", contact ? "Close edit contact" : "Close create contact");
    additional.hidden = Boolean(contact);
    editFields.hidden = !contact;
    errorMessage.hidden = true;
    errorMessage.textContent = "";
    closeAdditionalMenu();
    if (contact) {
      for (const [key, label, type, placeholder] of optionalContactFields) optionalFields.append(field(key, label, type, placeholder));
      editFields.append(field("company", "Company", "text"), field("createdAt", "Contact since", "date", "", true), field("status", "Status", "text", "", true), field("labels", "Labels", "text", "Real Estate, Legal, Customers"));
      const help = node("span", "crm-field-help", "Separate labels with commas. Up to 10 labels, 40 characters each.");
      help.id = "crm-labels-help";
      editFields.append(help);
      form.elements.namedItem("labels").setAttribute("aria-describedby", help.id);
      for (const key of ["firstName", "lastName", "phone", "email", "address", "website", "company", "status"]) form.elements.namedItem(key).value = contact[key] || "";
      form.elements.namedItem("createdAt").value = dateInputValue(contact.createdAt);
      form.elements.namedItem("labels").value = (contact.labels || []).join(", ");
    }
    syncAdditionalMenu();
    dialog.showModal();
    form.elements.namedItem("firstName").focus();
  }
  function dateInputValue(value) {
    const parsed = new Date(value);
    return `${parsed.getFullYear()}-${String(parsed.getMonth() + 1).padStart(2, "0")}-${String(parsed.getDate()).padStart(2, "0")}`;
  }
  function openCreateContact() { if (dialog) prepareDialog(null); }
  function openEditContact(id) { const contact = contacts().find(candidate => candidate.id === id); if (dialog && contact) prepareDialog(contact); }
  function formError(message, input) { errorMessage.textContent = message; errorMessage.hidden = false; input?.focus(); }
  async function saveContact(event) {
    event.preventDefault();
    if (savingContact) return;
    const data = new FormData(form), get = name => String(data.get(name) || "").trim();
    const editing = editingContactId !== null, original = editing ? contacts().find(contact => contact.id === editingContactId) : null;
    if (editing && !original) { formError("This contact is no longer available. Close this dialog and reopen the contacts list."); return; }
    const firstName = get("firstName"), lastName = get("lastName"), phone = normalizePhone(get("phone"));
    for (const [name, value, label] of [["firstName", firstName, "first name"], ["lastName", lastName, "last name"]]) {
      if (!value || value.length > 80) { formError(`Enter a ${label} of up to 80 characters.`, form.elements.namedItem(name)); return; }
    }
    if (!phonePattern.test(phone)) { formError("Enter a valid phone number with a + country code, such as +1 941 555 0123.", form.elements.namedItem("phone")); return; }
    const email = get("email"), address = get("address"), website = get("website"), company = get("company");
    if (email && (!form.elements.namedItem("email").validity.valid || email.length > 254)) { formError("Enter a valid email address.", form.elements.namedItem("email")); return; }
    if (website) {
      try { const url = new URL(website); if (!["https:", "http:"].includes(url.protocol) || !url.hostname || website.length > 400) throw new Error("invalid"); }
      catch (_) { formError("Enter a website beginning with https:// or http://.", form.elements.namedItem("website")); return; }
    }
    for (const [key, value] of [["address", address], ["company", company]]) {
      if (value.length > 400) { formError(`Use 400 characters or fewer for the ${key}.`, form.elements.namedItem(key)); return; }
    }
    let createdAt = original?.createdAt || new Date().toISOString(), contactStatus = "New", labels = [];
    if (editing) {
      const inputDate = get("createdAt"), parsed = new Date(`${inputDate}T12:00:00Z`);
      if (!/^\d{4}-\d{2}-\d{2}$/.test(inputDate) || !Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== inputDate) {
        formError("Enter a valid contact date.", form.elements.namedItem("createdAt")); return;
      }
      if (inputDate !== dateInputValue(original.createdAt)) createdAt = parsed.toISOString();
      contactStatus = get("status");
      if (!contactStatuses.includes(contactStatus)) { formError("Choose a valid contact status.", form.elements.namedItem("status")); return; }
      labels = [...new Set(get("labels").split(",").map(label => label.trim()).filter(Boolean))];
      if (labels.length > 10 || labels.some(label => label.length > 40)) { formError("Use up to 10 labels, with 40 characters or fewer per label.", form.elements.namedItem("labels")); return; }
    }
    if (contacts().some(contact => contact.id !== original?.id && contact.phone === phone)) { formError("A contact with this phone number already exists.", form.elements.namedItem("phone")); return; }
    if (!editing && localContacts.length >= MAX_CONTACTS) { formError("This workspace has reached its limit of 500 contacts. Your contact has not been saved."); return; }
    const contact = {id: original?.id || draftContactId || `local-${crypto.randomUUID()}`, firstName, lastName, phone, email, address, website, company, createdAt, status: contactStatus, labels, demo: Boolean(original?.demo)};
    savingContact = true;
    submitButton.disabled = true;
    submitButton.textContent = "Saving…";
    form.inert = true;
    form.setAttribute("aria-busy", "true");
    errorMessage.hidden = true;
    try {
      if (!window.DashboardWorkspace) throw new Error("Workspace storage did not load. Reload the dashboard and try again.");
      const saved = await window.DashboardWorkspace.saveContact(contact);
      successMessage = editing ? `${fullName(saved)} updated.` : `${fullName(saved)} created. Saved to the workspace.`;
      savedContactId = saved.id;
      revision += 1;
    } catch (failure) {
      formError(`Could not confirm your contact was saved. ${failure.message || "Check your connection and try again."}`);
      return;
    } finally {
      savingContact = false;
      submitButton.disabled = false;
      submitButton.textContent = editing ? "Save changes" : "Create contact";
      form.inert = false;
      form.removeAttribute("aria-busy");
    }
    listFilter = "all";
    searchText = "";
    closeDialog();
    window.location.hash = `contacts/${contact.id}`;
    render();
    setTimeout(() => document.getElementById("crm-profile-title")?.focus({preventScroll: true}), 0);
  }
  function callFromSession(session) {
    if (!session || !/^CA[0-9a-fA-F]{32}$/.test(session.call_sid) || !session.call_detail) return null;
    const detail = session.call_detail;
    const terminalStatuses = new Set(["completed", "ended", "closed", "failed", "error", "stopped", "disabled", "absent", "partial"]);
    const live = !session.ended_at && !detail.ended_at && !terminalStatuses.has(String(session.status).toLowerCase());
    const failed = ["failed", "error"].includes(session.status);
    return {id: session.call_sid, real: true, collection: session.voicemail || session.voicemail_only ? "voicemail" : "recent", phone: normalizePhone(detail.caller_number), startedAt: detail.started_at || session.started_at,
      duration: Number.isFinite(detail.duration_seconds) ? detail.duration_seconds : null, direction: "Call",
      outcome: live ? "Live" : failed ? "Failed" : "Completed", title: live ? "Live conversation" : "Call conversation", summary: detail.summary?.text || ""};
  }
  function setSessions(sessions) {
    lastSessions = Array.isArray(sessions) ? sessions : [];
    const knownPhones = new Set(contacts().map(contact => contact.phone));
    const next = lastSessions.map(callFromSession).filter(call => call && knownPhones.has(call.phone));
    if (JSON.stringify(next) !== JSON.stringify(realCalls)) { realCalls = next; revision += 1; }
  }
  function initialize() {
    root = document.getElementById("contacts-view");
    if (!root) return;
    buildDialog();
    if (window.DashboardWorkspace) window.DashboardWorkspace.subscribe(applyWorkspace);
    else applyWorkspace({error: "Workspace storage did not load. Reload the dashboard and try again."});
    render();
    window.addEventListener("hashchange", render);
  }
  window.DashboardCRM = {openCreateContact, openEditContact, render, setSessions, setHistoryMetrics, findContactByPhone};
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize, {once: true}); else initialize();
})();
