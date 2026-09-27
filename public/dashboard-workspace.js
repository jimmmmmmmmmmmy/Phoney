(() => {
  "use strict";
  const CONTACT_KEY = "hacking-banyons.contacts.v1";
  const AGENT_KEY = "hacking-banyons.agent-drafts.v1";
  const listeners = new Set();
  let snapshot = null, error = "", importError = "", loading = false;
  let queue = Promise.resolve();
  const copy = value => value === null ? null : JSON.parse(JSON.stringify(value));
  const record = value => value && typeof value === "object" && !Array.isArray(value);
  function validSnapshot(value) {
    return record(value) && value.version === 1
      && ["contacts", "demoOverrides", "agents"].every(key => Array.isArray(value[key])
        && value[key].length <= (key === "contacts" ? 500 : key === "agents" ? 50 : 4)
        && value[key].every(item => record(item) && typeof item.id === "string"));
  }
  function state() { return {snapshot: copy(snapshot), error, importError, loading}; }
  function publish() { for (const listener of listeners) listener(state()); }
  function serial(operation) {
    const result = queue.then(operation, operation);
    queue = result.catch(() => {});
    return result;
  }
  function requestError(status) {
    return new Error(status === 409 ? "These details conflict with an existing workspace record. Refresh and try again."
      : status === 400 || status === 422 ? "Review the entered details and try again. The workspace did not save them."
      : "Workspace storage is unavailable. Save could not be confirmed. Check your connection and try again.");
  }
  async function request(path, method = "GET", body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(path, {method, credentials: "same-origin", cache: "no-store", signal: controller.signal,
        headers: {Accept: "application/json", ...(body === undefined ? {} : {"Content-Type": "application/json", "X-Workspace-Request": "1"})},
        ...(body === undefined ? {} : {body: JSON.stringify(body)})});
      if (!response.ok) throw requestError(response.status);
      return await response.json();
    } catch (failure) {
      if (failure instanceof Error && failure.message.startsWith("These details conflict")) throw failure;
      if (failure instanceof Error && failure.message.startsWith("Review the entered details")) throw failure;
      throw requestError();
    } finally { clearTimeout(timer); }
  }
  function legacyRecords() {
    const data = {contacts: [], demoOverrides: [], agents: []};
    const warnings = [];
    for (const [key, kind] of [[CONTACT_KEY, "contacts"], [AGENT_KEY, "agents"]]) {
      try {
        const raw = window.localStorage.getItem(key);
        if (!raw) continue;
        const value = JSON.parse(raw);
        if (kind === "contacts") {
          if (!record(value) || value.version !== 1 || !Array.isArray(value.contacts)
            || value.contacts.length > 500 || (value.demoOverrides !== undefined && !Array.isArray(value.demoOverrides))
            || (value.demoOverrides || []).length > 4) throw new Error("invalid");
          data.contacts = value.contacts;
          data.demoOverrides = value.demoOverrides || [];
        } else {
          if (!Array.isArray(value) || value.length > 50) throw new Error("invalid");
          data.agents = value;
        }
      } catch (_) { warnings.push(kind === "contacts" ? "contacts" : "agent drafts"); }
    }
    importError = warnings.length ? `Browser ${warnings.join(" and ")} could not be imported. The browser backup is unchanged; reopen the original dashboard to recover it.` : "";
    return data;
  }
  async function load() {
    loading = true; error = ""; publish();
    try {
      const current = await request("/api/workspace");
      if (!validSnapshot(current)) throw requestError();
      snapshot = copy(current);
      const legacy = legacyRecords();
      if (Object.values(legacy).some(items => items.length)) {
        try {
          const imported = await request("/api/workspace/import", "POST", legacy);
          if (!validSnapshot(imported)) throw requestError();
          snapshot = copy(imported);
        } catch (_) {
          importError = "Browser records could not be imported to the workspace. Your browser backup is unchanged. Check the connection and reload to retry.";
        }
      }
      return copy(snapshot);
    } catch (failure) { error = failure.message; throw failure; }
    finally { loading = false; publish(); }
  }
  function save(kind, value) {
    return serial(async () => {
      if (!snapshot) await load();
      try {
        if (!record(value) || typeof value.id !== "string") throw requestError(422);
        const saved = await request(`/api/workspace/${kind}/${encodeURIComponent(value.id)}`, "PUT", value);
        if (!record(saved) || saved.id !== value.id) throw requestError();
        const key = kind === "contacts" && saved.id.startsWith("demo-") ? "demoOverrides" : kind;
        const records = snapshot[key];
        snapshot = {...snapshot, [key]: records.some(item => item.id === saved.id)
          ? records.map(item => item.id === saved.id ? copy(saved) : item) : [copy(saved), ...records]};
        error = ""; publish();
        return copy(saved);
      } catch (failure) { error = failure.message; publish(); throw failure; }
    });
  }
  const api = {getSnapshot: () => copy(snapshot),
    subscribe(listener) { listeners.add(listener); listener(state()); return () => listeners.delete(listener); },
    reload: () => serial(load), saveContact: contact => save("contacts", contact), saveAgent: agent => save("agents", agent)};
  window.DashboardWorkspace = api;
  api.ready = api.reload();
  // Subscribers and form handlers surface failures without an unhandled rejection.
  api.ready.catch(() => {});
})();
