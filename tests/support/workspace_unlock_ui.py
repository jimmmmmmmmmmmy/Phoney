"""Shared fixtures and fakes for focused integration checks."""

from pathlib import Path


import shutil


import subprocess


import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "public/workspace-unlock.js"


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
