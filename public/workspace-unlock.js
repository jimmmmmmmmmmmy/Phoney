(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  const form = $('unlock-form');
  const controls = $('unlock-controls');
  const pinInput = $('pin-input');
  const pinEntry = $('pin-entry');
  const pinDots = Array.from($('pin-dots').children);
  const passwordInput = $('password-input');
  const status = $('unlock-status');
  const methodButton = $('switch-method');
  const retryButton = $('retry-status');
  const passwordSubmit = $('password-submit');
  const visibilityButton = $('password-visibility');
  const state = {method: 'pin', ready: false, busy: true, passwordRequired: false, retryUntil: 0};
  let retryTimer = null;
  let feedbackTimer = null;

  function say(message, tone = '') {
    status.textContent = message;
    status.dataset.tone = tone;
  }

  function renderControls() {
    const waiting = state.retryUntil > Date.now();
    controls.disabled = state.busy || !state.ready || waiting;
    form.setAttribute('aria-busy', String(state.busy));
    pinInput.disabled = state.method !== 'pin';
    pinInput.required = state.method === 'pin';
    passwordInput.disabled = state.method !== 'password';
    passwordInput.required = state.method === 'password';
    passwordSubmit.textContent = state.busy && state.method === 'password' ? 'Unlocking…' : 'Unlock workspace';
    retryButton.disabled = state.busy;
  }

  function renderPin() {
    pinDots.forEach((dot, index) => { dot.dataset.filled = String(index < pinInput.value.length); });
    $('pin-progress').textContent = pinInput.value.length ? `${pinInput.value.length} of 6 digits entered.` : '';
  }

  function clearCredentials() {
    pinInput.value = '';
    passwordInput.value = '';
    passwordInput.type = 'password';
    visibilityButton.setAttribute('aria-pressed', 'false');
    visibilityButton.setAttribute('aria-label', 'Show password');
    renderPin();
  }

  function showMethod(method, focus = true) {
    state.method = method;
    clearCredentials();
    pinInput.removeAttribute('aria-invalid');
    passwordInput.removeAttribute('aria-invalid');
    pinEntry.dataset.invalid = 'false';
    $('pin-panel').hidden = method !== 'pin';
    $('password-panel').hidden = method !== 'password';
    $('unlock-description').textContent = method === 'pin' ? 'Enter your 6-digit PIN.' : 'Enter your workspace password.';
    methodButton.textContent = method === 'pin' ? 'Use password' : 'Use PIN';
    methodButton.hidden = method === 'password' && state.passwordRequired;
    renderControls();
    if (focus && !controls.disabled) {
      // The custom keypad is ready on touch devices without opening a second keyboard.
      if (method === 'password' || !window.matchMedia('(pointer: coarse)').matches) {
        (method === 'password' ? passwordInput : pinInput).focus();
      }
    }
  }

  function clearFeedback() {
    pinEntry.dataset.invalid = 'false';
    pinEntry.dataset.feedback = 'false';
    pinInput.removeAttribute('aria-invalid');
    passwordInput.removeAttribute('aria-invalid');
    if (status.dataset.tone === 'error' && !state.retryUntil) say('');
  }

  function pinChanged() {
    pinInput.value = pinInput.value.replace(/[^0-9]/g, '').slice(0, 6);
    clearFeedback();
    renderPin();
    if (pinInput.value.length === 6 && state.ready && !state.busy && !controls.disabled) {
      void unlock();
    }
  }

  function addDigit(digit) {
    if (state.method !== 'pin' || controls.disabled || pinInput.value.length >= 6) return;
    pinInput.value += digit;
    pinChanged();
  }

  function deleteDigit() {
    if (state.method !== 'pin' || controls.disabled) return;
    pinInput.value = pinInput.value.slice(0, -1);
    clearFeedback();
    renderPin();
  }

  function invalidPin(message) {
    pinInput.value = '';
    renderPin();
    pinInput.setAttribute('aria-invalid', 'true');
    pinEntry.dataset.invalid = 'true';
    pinEntry.dataset.feedback = 'true';
    clearTimeout(feedbackTimer);
    feedbackTimer = setTimeout(() => { pinEntry.dataset.feedback = 'false'; }, 400);
    say(message, 'error');
    if (!window.matchMedia('(pointer: coarse)').matches) pinInput.focus();
  }

  function startRequestCooldown(seconds) {
    clearInterval(retryTimer);
    const duration = Math.max(1, Math.min(Number(seconds) || 30, 3600));
    state.retryUntil = Date.now() + duration * 1000;
    const tick = () => {
      const remaining = Math.max(0, Math.ceil((state.retryUntil - Date.now()) / 1000));
      renderControls();
      if (remaining) {
        say(`Too many attempts. Try again in ${remaining} second${remaining === 1 ? '' : 's'}.`, 'error');
      } else {
        state.retryUntil = 0;
        clearInterval(retryTimer);
        retryTimer = null;
        say('You can try again now.');
        renderControls();
      }
    };
    tick();
    retryTimer = setInterval(tick, 1000);
  }

  async function request(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(path, {
        credentials: 'same-origin', cache: 'no-store', ...options, signal: controller.signal,
      });
      const payload = await response.json().catch(() => ({}));
      return {response, payload};
    } finally {
      clearTimeout(timeout);
    }
  }

  function rememberServerState(payload) {
    if (typeof payload.password_required === 'boolean') state.passwordRequired = payload.password_required;
    if (payload.password_required || payload.attempts_remaining === 0) {
      state.passwordRequired = true;
      showMethod('password', false);
    }
  }

  async function checkAccess() {
    if (state.busy && state.ready) return;
    state.busy = true;
    state.ready = false;
    retryButton.hidden = true;
    renderControls();
    say('Checking workspace access…');
    try {
      const {response, payload} = await request('/api/workspace-access/status');
      if (response.ok && payload.authenticated === true) {
        say('Workspace unlocked. Opening…', 'success');
        window.location.replace('/dashboard');
        return;
      }
      if (!response.ok || payload.configured === false) {
        say(payload.error === 'unconfigured' || payload.configured === false
          ? 'Workspace access is not configured. Contact the workspace owner.'
          : 'Workspace access is unavailable. Try again.', 'error');
        retryButton.hidden = false;
        return;
      }
      state.ready = true;
      rememberServerState(payload);
      say(state.passwordRequired ? 'PIN attempts used. Unlock with your password.' : '');
      if (payload.retry_after && payload.throttle_scope === 'request') {
        startRequestCooldown(payload.retry_after);
      }
    } catch (_) {
      say('Unable to connect. Check your connection and try again.', 'error');
      retryButton.hidden = false;
    } finally {
      state.busy = false;
      renderControls();
    }
  }

  async function unlock() {
    if (!state.ready || state.busy || controls.disabled) return;
    const method = state.method;
    const credential = method === 'pin' ? pinInput.value : passwordInput.value;
    if (method === 'pin' && !/^[0-9]{6}$/.test(credential)) {
      say('Enter all 6 digits of your PIN.', 'error');
      return;
    }
    if (method === 'password' && !credential) {
      say('Enter your workspace password.', 'error');
      passwordInput.focus();
      return;
    }
    state.busy = true;
    renderControls();
    say('Unlocking…');
    try {
      const {response, payload} = await request('/api/workspace-access/unlock', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-Workspace-Access': '1'},
        body: JSON.stringify({method, credential, remember: $('remember-device').checked}),
      });
      clearCredentials();
      if (response.ok && payload.authenticated === true) {
        say('Workspace unlocked. Opening…', 'success');
        window.location.replace('/dashboard');
        return;
      }
      rememberServerState(payload);
      if (response.status === 429 || payload.error === 'throttled') {
        if (payload.throttle_scope === 'pin' && state.passwordRequired) {
          say('PIN attempts used. Unlock with your password.', 'error');
        } else {
          startRequestCooldown(payload.retry_after || response.headers.get('Retry-After'));
        }
      } else if (payload.error === 'password_required' || state.passwordRequired && method === 'pin') {
        say('PIN attempts used. Unlock with your password.', 'error');
      } else if (payload.error === 'pin_invalid') {
        const remaining = Number.isInteger(payload.attempts_remaining) ? payload.attempts_remaining : null;
        invalidPin(remaining === null ? 'Incorrect PIN. Try again.'
          : `Incorrect PIN. ${remaining} attempt${remaining === 1 ? '' : 's'} left before password is required.`);
      } else if (payload.error === 'password_invalid') {
        passwordInput.setAttribute('aria-invalid', 'true');
        say('Incorrect password. Try again.', 'error');
      } else if (payload.error === 'unconfigured') {
        state.ready = false;
        say('Workspace access is not configured. Contact the workspace owner.', 'error');
        retryButton.hidden = false;
      } else if (payload.error === 'request_rejected') {
        say('Access request was rejected. Reload this page and try again.', 'error');
      } else {
        say('Workspace access is unavailable. Try again.', 'error');
      }
    } catch (_) {
      clearCredentials();
      say('Unable to connect. Check your connection and try again.', 'error');
    } finally {
      state.busy = false;
      renderControls();
      if (state.method === 'password' && !controls.disabled) passwordInput.focus();
    }
  }

  form.addEventListener('submit', event => { event.preventDefault(); void unlock(); });
  pinInput.addEventListener('input', pinChanged);
  pinInput.addEventListener('paste', event => {
    if (controls.disabled) return;
    const pasted = event.clipboardData?.getData('text');
    if (pasted === undefined) return;
    event.preventDefault();
    const digits = pasted.replace(/\s/g, '');
    if (!/^[0-9]{6}$/.test(digits)) {
      say('Paste a 6-digit PIN.', 'error');
      return;
    }
    pinInput.value = digits;
    pinChanged();
  });
  document.querySelectorAll('[data-digit]').forEach(button => {
    button.addEventListener('click', () => addDigit(button.dataset.digit));
    button.addEventListener('mousedown', event => event.preventDefault());
  });
  $('delete-digit').addEventListener('click', deleteDigit);
  $('delete-digit').addEventListener('mousedown', event => event.preventDefault());
  methodButton.addEventListener('click', () => {
    if (controls.disabled) return;
    say('');
    showMethod(state.method === 'pin' ? 'password' : 'pin');
  });
  passwordInput.addEventListener('input', clearFeedback);
  visibilityButton.addEventListener('click', () => {
    const visible = passwordInput.type === 'password';
    passwordInput.type = visible ? 'text' : 'password';
    visibilityButton.setAttribute('aria-pressed', String(visible));
    visibilityButton.setAttribute('aria-label', visible ? 'Hide password' : 'Show password');
  });
  retryButton.addEventListener('click', () => { if (!state.busy) void checkAccess(); });
  document.addEventListener('keydown', event => {
    if (state.method !== 'pin' || controls.disabled || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.target === pinInput || event.target === $('remember-device')) return;
    if (/^[0-9]$/.test(event.key)) { event.preventDefault(); addDigit(event.key); }
    else if (event.key === 'Backspace') { event.preventDefault(); deleteDigit(); }
    else if (event.key === 'Enter' && event.target === document.body) { event.preventDefault(); void unlock(); }
  });
  window.addEventListener('pageshow', event => { if (event.persisted) { clearCredentials(); void checkAccess(); } });
  void checkAccess();
})();
