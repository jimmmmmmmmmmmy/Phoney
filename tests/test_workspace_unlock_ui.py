"""Unlock UI behavior against explicit server responses; no production credentials."""

from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "public/workspace-unlock.js"

HARNESS = r"""
const assert = require('node:assert/strict');
class Element {
  constructor(id) {
    this.id = id; this.value = ''; this.disabled = false; this.required = false;
    this.hidden = false; this.checked = false; this.type = 'password';
    this.textContent = ''; this.dataset = {}; this.attributes = {}; this.listeners = {}; this.children = [];
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  getAttribute(name) { return this.attributes[name] ?? null; }
  addEventListener(name, listener) { (this.listeners[name] ||= []).push(listener); }
  emit(name, event = {}) {
    event.target ||= this;
    event.preventDefault ||= () => { event.defaultPrevented = true; };
    for (const listener of this.listeners[name] || []) listener(event);
    return event;
  }
  click() { this.emit('click'); }
  focus() { document.activeElement = this; }
}
const ids = ['unlock-form', 'unlock-controls', 'pin-input', 'pin-entry', 'pin-dots', 'password-input',
  'unlock-status', 'switch-method', 'retry-status', 'password-submit', 'password-visibility',
  'pin-progress', 'pin-panel', 'password-panel', 'unlock-description', 'delete-digit', 'remember-device'];
const elements = Object.fromEntries(ids.map(id => [id, new Element(id)]));
elements['pin-dots'].children = Array.from({length: 6}, (_, i) => new Element('dot-' + i));
elements['remember-device'].checked = true;
elements['unlock-controls'].disabled = true;
elements['password-panel'].hidden = true;
elements['password-input'].disabled = true;
const digitButtons = Array.from({length: 10}, (_, i) => {
  const element = new Element('digit-' + i); element.dataset.digit = String(i); return element;
});
const document = new Element('document');
document.body = new Element('body');
document.getElementById = id => elements[id];
document.querySelectorAll = () => digitButtons;
const navigations = [];
const window = new Element('window');
window.location = {replace: path => navigations.push(path)};
window.matchMedia = () => ({matches: false});
const $ = id => elements[id];
const tick = () => new Promise(resolve => setImmediate(resolve));
const responseQueue = [{status: 200, payload: {configured: true, authenticated: false,
  password_required: false, attempts_remaining: 3}}];
const requests = [];
function response(value) {
  return {ok: value.status >= 200 && value.status < 300, status: value.status,
    json: async () => value.payload, headers: {get: () => value.retryAfter || null}};
}
async function fetch(path, options = {}) {
  requests.push({path, options, body: options.body ? JSON.parse(options.body) : null});
  const next = responseQueue.shift();
  if (!next) throw new Error('Unexpected fetch');
  if (next instanceof Error) throw next;
  return response(await (typeof next === 'function' ? next() : next));
}
let now = 10000, timerCounter = 0;
Date.now = () => now;
const intervals = new Map();
function setTimeout() { return ++timerCounter; }
function clearTimeout() {}
function setInterval(callback) { const id = ++timerCounter; intervals.set(id, callback); return id; }
function clearInterval(id) { intervals.delete(id); }
function enterPin(value) { $('pin-input').value = value; $('pin-input').emit('input'); }
"""


def run_unlock(checks, *, before=""):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for unlock UI behavior tests")
    source = HARNESS + before + "\n" + SCRIPT.read_text() + "\n(async () => { await tick();\n" + checks
    source += "\n})().catch(error => { console.error(error); process.exitCode = 1; });"
    result = subprocess.run([node, "-e", source], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_pin_keyboard_paste_busy_state_and_server_only_unlock():
    run_unlock(r"""
assert.equal($('unlock-controls').disabled, false);
document.emit('keydown', {target: document.body, key: '1'});
digitButtons[2].click();
document.emit('keydown', {target: document.body, key: 'Backspace'});
assert.equal($('pin-input').value, '1');
let release;
responseQueue.push(() => new Promise(resolve => { release = resolve; }));
$('pin-input').emit('paste', {clipboardData: {getData: () => '123 456'}});
assert.equal(requests.length, 2);
assert.deepEqual(requests[1].body, {method: 'pin', credential: '123456', remember: true});
assert.equal(requests[1].options.headers['X-Workspace-Access'], '1');
assert.equal(requests[1].options.credentials, 'same-origin');
assert.equal($('unlock-controls').disabled, true);
$('unlock-form').emit('submit');
digitButtons[9].click();
assert.equal(requests.length, 2);
assert.deepEqual(navigations, []);
release({status: 200, payload: {authenticated: true, redirect: 'https://untrusted.invalid/'}});
await tick();
assert.deepEqual(navigations, ['/dashboard']);
assert.equal($('pin-input').value, '');
""")


def test_three_server_pin_failures_require_password_and_keep_remember_choice():
    run_unlock(r"""
for (let remaining = 2; remaining >= 0; remaining--) {
  responseQueue.push({status: remaining ? 401 : 403, payload: {
    error: remaining ? 'pin_invalid' : 'password_required', attempts_remaining: remaining,
    password_required: remaining === 0}});
  enterPin('123456');
  await tick();
  assert.equal($('pin-input').value, '');
  if (remaining) {
    assert.equal($('password-panel').hidden, true);
    assert.match($('unlock-status').textContent, new RegExp(String(remaining) + ' attempt'));
  }
}
assert.equal($('pin-panel').hidden, true);
assert.equal($('password-panel').hidden, false);
assert.equal($('switch-method').hidden, true);
assert.equal($('password-input').required, true);
assert.equal($('pin-input').disabled, true);
$('remember-device').checked = false;
$('password-input').value = 'a test credential, not a real password';
responseQueue.push({status: 200, payload: {authenticated: true}});
$('unlock-form').emit('submit');
await tick();
assert.equal(requests.at(-1).body.method, 'password');
assert.equal(requests.at(-1).body.remember, false);
assert.equal($('password-input').value, '');
assert.deepEqual(navigations, ['/dashboard']);
""")


def test_pin_throttle_allows_password_and_request_throttle_waits_for_server_delay():
    run_unlock(r"""
responseQueue.push({status: 429, payload: {error: 'throttled', password_required: true,
  attempts_remaining: 0, retry_after: 60, throttle_scope: 'pin'}});
enterPin('123456'); await tick();
assert.equal($('password-panel').hidden, false);
assert.equal($('unlock-controls').disabled, false);
$('password-input').value = 'a test credential, not a real password';
responseQueue.push({status: 429, payload: {error: 'throttled', password_required: true,
  retry_after: 8, throttle_scope: 'request'}});
$('unlock-form').emit('submit'); await tick();
assert.equal($('unlock-controls').disabled, true);
assert.match($('unlock-status').textContent, /8 seconds/);
const count = requests.length;
$('unlock-form').emit('submit');
assert.equal(requests.length, count);
now += 9000;
for (const callback of intervals.values()) callback();
assert.equal($('unlock-controls').disabled, false);
assert.match($('unlock-status').textContent, /try again now/);
""")


def test_unavailable_status_is_retryable_and_password_failure_never_navigates():
    run_unlock(r"""
assert.equal($('unlock-controls').disabled, true);
assert.equal($('retry-status').hidden, false);
assert.match($('unlock-status').textContent, /Unable to connect/);
responseQueue.push({status: 200, payload: {authenticated: false, configured: true,
  password_required: false, attempts_remaining: 3}});
$('retry-status').click(); await tick();
assert.equal($('unlock-controls').disabled, false);
$('switch-method').click();
$('password-input').value = 'a test credential, not a real password';
responseQueue.push({status: 401, payload: {error: 'password_invalid', password_required: false}});
$('unlock-form').emit('submit'); await tick();
assert.equal($('password-input').value, '');
assert.equal($('password-input').getAttribute('aria-invalid'), 'true');
assert.match($('unlock-status').textContent, /Incorrect password/);
assert.deepEqual(navigations, []);
""", before="responseQueue[0] = new Error('Network unavailable');")
