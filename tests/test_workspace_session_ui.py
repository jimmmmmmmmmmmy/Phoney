"""Device-lock UI recovers after stalled response headers or bodies."""

from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "public/workspace-session.js"

HARNESS = r"""
const assert = require('node:assert/strict');
class Element {
  constructor(id = '') {
    this.id = id; this.disabled = false; this.title = ''; this.children = [];
    this.attributes = {}; this.listeners = {}; this.style = {};
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  addEventListener(name, listener) { (this.listeners[name] ||= []).push(listener); }
  emit(name) { for (const listener of this.listeners[name] || []) listener({target: this}); }
  append(...children) { this.children.push(...children); }
  prepend(child) { this.children.unshift(child); }
  click() { this.emit('click'); }
}
const mount = new Element('header-actions');
const document = new Element('document');
document.hidden = false;
document.documentElement = new Element('html');
document.getElementById = id => id === 'header-actions' ? mount : null;
document.createElement = () => new Element();
document.createElementNS = () => new Element();
const navigations = [], timers = new Map(), intervals = new Map();
let timerId = 0;
const window = new Element('window');
window.location = {replace: path => navigations.push(path)};
window.setTimeout = (callback, delay) => {
  const id = ++timerId; timers.set(id, {callback, delay}); return id;
};
window.clearTimeout = id => timers.delete(id);
window.setInterval = (callback, delay) => {
  const id = ++timerId; intervals.set(id, {callback, delay}); return id;
};
const tick = () => new Promise(resolve => setImmediate(resolve));
async function deadline() {
  assert.ok(timers.size > 0, 'An active request must have a deadline');
  for (const {callback, delay} of [...timers.values()]) {
    assert.equal(delay, 12000); callback();
  }
  await tick();
}
function stall(signal) {
  return new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => reject(new Error('AbortError')), {once: true});
  });
}
const authenticated = {enabled: true, authenticated: true};
const responses = [{status: 200, payload: authenticated}];
const requests = [];
async function fetch(path, options) {
  requests.push({path, options});
  const next = responses.shift();
  if (!next) throw new Error('Unexpected request');
  if (next === 'stall') return await stall(options.signal);
  if (next instanceof Error) throw next;
  return {ok: next.status >= 200 && next.status < 300,
    json: () => next.bodyStalls ? stall(options.signal) : Promise.resolve(next.payload)};
}
const lockButton = () => mount.children.find(child => child.id === 'workspace-lock');
"""


def run_session(checks, *, before=""):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for workspace session UI tests")
    source = HARNESS + before + "\n" + SCRIPT.read_text()
    source += "\n(async () => { await tick();\n" + checks
    source += "\n})().catch(error => { console.error(error); process.exitCode = 1; });"
    result = subprocess.run([node, "-e", source], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_stalled_status_times_out_and_later_focus_checks_revocation():
    run_session(r"""
assert.equal(requests.length, 1);
window.emit('focus');
assert.equal(requests.length, 1, 'Do not issue overlapping status requests');
await deadline();
assert.equal(requests[0].options.signal.aborted, true);
assert.equal(timers.size, 0);
assert.deepEqual(navigations, [], 'Connection failures do not revoke an existing page');
responses.push({status: 200, payload: {enabled: true, authenticated: false}});
window.emit('focus'); await tick();
assert.equal(requests.length, 2);
assert.deepEqual(navigations, ['/unlock']);
assert.equal(document.documentElement.style.visibility, 'hidden');
assert.equal(timers.size, 0);
""", before="responses[0] = 'stall';")


def test_deadline_covers_status_body_and_allows_another_check():
    run_session(r"""
assert.equal(requests.length, 1);
assert.equal(lockButton(), undefined);
await deadline();
assert.equal(requests[0].options.signal.aborted, true);
assert.deepEqual(navigations, []);
responses.push({status: 200, payload: authenticated});
document.emit('visibilitychange'); await tick();
assert.equal(requests.length, 2);
assert.ok(lockButton());
assert.equal(timers.size, 0);
""", before="responses[0] = {status: 200, bodyStalls: true};")


def test_stalled_lock_can_be_retried_and_success_hides_workspace():
    run_session(r"""
const button = lockButton();
assert.ok(button);
assert.equal(timers.size, 0, 'A completed status request clears its timer');
responses.push('stall');
button.click();
assert.equal(button.disabled, true);
assert.equal(requests.length, 2);
button.click();
assert.equal(requests.length, 2, 'A busy Lock button must not submit twice');
await deadline();
assert.equal(button.disabled, false);
assert.match(button.title, /Could not lock this device/);
assert.equal(button.getAttribute('aria-label'), button.title);
assert.deepEqual(navigations, []);
responses.push({status: 200, payload: {authenticated: false}});
button.click(); await tick();
assert.equal(requests.at(-1).path, '/api/workspace-access/logout');
assert.equal(requests.at(-1).options.headers['X-Workspace-Access'], '1');
assert.equal(requests.at(-1).options.body, '{}');
assert.equal(requests.at(-1).options.credentials, 'same-origin');
assert.deepEqual(navigations, ['/unlock']);
assert.equal(document.documentElement.style.visibility, 'hidden');
assert.equal(timers.size, 0);
""")


def test_failed_status_preserves_page_and_completed_requests_clear_deadline():
    run_session(r"""
assert.equal(timers.size, 0);
responses.push({status: 503});
window.emit('focus'); await tick();
assert.deepEqual(navigations, []);
assert.equal(document.documentElement.style.visibility, undefined);
assert.equal(timers.size, 0);
responses.push({status: 200, payload: {enabled: true, authenticated: false}});
window.emit('focus'); await tick();
assert.deepEqual(navigations, ['/unlock']);
assert.equal(timers.size, 0);
""")
