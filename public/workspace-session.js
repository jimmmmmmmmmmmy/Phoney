(() => {
  "use strict";
  let enabled = false;
  let checking = false;
  let button;

  async function request(path, options = {}, readJSON = false) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(path, {
        credentials: "same-origin", cache: "no-store", ...options, signal: controller.signal
      });
      // Keep the deadline active while reading status JSON, too: a server may
      // send headers and then stall before completing the response body.
      const state = readJSON && response.ok ? await response.json() : null;
      return {response, state};
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function redirectToUnlock() {
    // Hide the current workspace immediately, including a restored browser page.
    document.documentElement.style.visibility = "hidden";
    window.location.replace("/unlock");
  }

  async function checkSession() {
    if (checking || document.hidden) return;
    checking = true;
    try {
      const {response, state} = await request("/api/workspace-access/status", {}, true);
      if (!response.ok) return;
      enabled = state.enabled === true;
      if (!enabled) return;
      if (!state.authenticated) {
        redirectToUnlock();
        return;
      }
      if (!button) mountLockButton();
    } catch (_) {
      // A connection failure does not revoke a valid local page; APIs fail closed.
    } finally {
      checking = false;
    }
  }

  function mountLockButton() {
    const mount = document.getElementById("header-actions");
    if (!mount) return;
    button = document.createElement("button");
    button.type = "button";
    button.className = "toolbar-icon";
    button.id = "workspace-lock";
    button.title = "Lock this device";
    button.setAttribute("aria-label", "Lock this device");
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    for (const [key, value] of Object.entries({viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
      "stroke-width": "1.7", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true"})) {
      svg.setAttribute(key, value);
    }
    const body = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    for (const [key, value] of Object.entries({x: "5", y: "10", width: "14", height: "11", rx: "2"})) body.setAttribute(key, value);
    const shackle = document.createElementNS("http://www.w3.org/2000/svg", "path");
    shackle.setAttribute("d", "M8 10V7a4 4 0 0 1 8 0v3M12 14v3");
    svg.append(body, shackle);
    button.append(svg);
    button.addEventListener("click", async () => {
      if (button.disabled) return;
      button.disabled = true;
      try {
        const {response} = await request("/api/workspace-access/logout", {
          method: "POST",
          headers: {"Content-Type": "application/json", "X-Workspace-Access": "1"},
          body: "{}"
        });
        if (!response.ok) throw new Error("Lock failed");
        redirectToUnlock();
      } catch (_) {
        button.disabled = false;
        button.title = "Could not lock this device. Check the connection and retry.";
        button.setAttribute("aria-label", button.title);
      }
    });
    mount.prepend(button);
  }

  window.addEventListener("pageshow", checkSession);
  window.addEventListener("focus", checkSession);
  document.addEventListener("visibilitychange", checkSession);
  window.setInterval(() => {if (enabled) checkSession();}, 60000);
  checkSession();
})();
