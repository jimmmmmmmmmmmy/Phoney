"""Creation menu and local agent drafts; the fake never calls a provider or server."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "public/dashboard-toolbar.js"

HARNESS = r"""
const assert = require('node:assert/strict');
class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.attributes = {};
    this.listeners = {}; this.className = ''; this.textContent = ''; this.hidden = false;
    this.value = ''; this.open = false; this.parentNode = null;
    this.classList = {add: name => {this.className += ' ' + name;}};
  }
  get isConnected() { return this === document.body || !!this.parentNode?.isConnected; }
  append(...elements) { for (const element of elements) {element.parentNode = this; this.children.push(element);} }
  prepend(element) {element.parentNode = this; this.children.unshift(element);}
  replaceChildren(...elements) {for (const child of this.children) child.parentNode = null; this.children = []; this.append(...elements);}
  setAttribute(key, value) {this.attributes[key] = String(value);}
  getAttribute(key) {return this.attributes[key] ?? null;}
  contains(target) {return target === this || this.children.some(child => child.contains(target));}
  addEventListener(type, listener) {(this.listeners[type] ||= []).push(listener);}
  dispatch(type, event = {}) {
    event.target ||= this;
    event.preventDefault ||= () => {event.defaultPrevented = true;};
    for (const listener of this.listeners[type] || []) listener(event);
    return event;
  }
  click() {this.dispatch('click');}
  focus() {document.activeElement = this; document.dispatch('focusin', {target: this});}
  all() {return [this, ...this.children.flatMap(child => child.all())];}
  querySelectorAll(selector) {return this.all().filter(child => child.attributes.role === 'menuitem');}
  querySelector(selector) {return this.querySelectorAll(selector)[0] || null;}
  reset() {for (const item of this.all()) if (['INPUT', 'TEXTAREA'].includes(item.tagName)) item.value = '';}
  showModal() {this.open = true;}
  close() {this.open = false; this.dispatch('close');}
}
const document = {
  body: new Element('body'), readyState: 'complete', activeElement: null, listeners: {},
  createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag),
  getElementById(id) {return this.body.all().find(element => element.id === id) || null;},
  addEventListener(type, listener) {(this.listeners[type] ||= []).push(listener);},
  dispatch(type, event) {event.preventDefault ||= () => {event.defaultPrevented = true;}; for (const listener of this.listeners[type] || []) listener(event);}
};
for (const id of ['header-actions', 'agents-view', 'nav-agents', 'page-title']) {
  const element = new Element(id === 'nav-agents' ? 'button' : 'div'); element.id = id; document.body.append(element);
}
let stored = null, failWrites = false, contactOpened = 0, navigated = 0, writes = 0;
const window = {
  localStorage: {
    getItem: () => stored,
    setItem: (key, value) => {if (failWrites) throw new Error('quota'); stored = value; writes++;}
  },
  DashboardCRM: {openCreateContact: () => contactOpened++}
};
document.getElementById('nav-agents').addEventListener('click', () => navigated++);
const $ = id => document.getElementById(id);
const text = element => element.textContent + element.children.map(text).join(' ');
const menuItems = () => $('create-menu').querySelectorAll('[role="menuitem"]');
const submit = () => $('create-agent-dialog').children[0].dispatch('submit');
"""


def run_toolbar(checks, *, before=""):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for dashboard behavior tests")
    result = subprocess.run(
        [node, "-e", HARNESS + before + "\n" + SCRIPT.read_text() + "\n" + checks],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_create_menu_keyboard_outside_click_and_contact_action():
    run_toolbar(r"""
$('create-button').click();
assert.equal($('create-button').getAttribute('aria-expanded'), 'true');
assert.equal(document.activeElement, menuItems()[0]);
$('create-menu').dispatch('keydown', {key: 'ArrowDown'});
assert.equal(document.activeElement, menuItems()[1]);
$('create-menu').dispatch('keydown', {key: 'Home'});
assert.equal(document.activeElement, menuItems()[0]);
menuItems()[0].click();
assert.equal(contactOpened, 1);
assert.equal($('create-menu').hidden, true);
assert.equal(document.activeElement, $('create-button'));
$('create-button').dispatch('keydown', {key: 'ArrowUp'});
assert.equal(document.activeElement, menuItems()[1]);
document.dispatch('keydown', {key: 'Escape'});
assert.equal($('create-menu').hidden, true);
assert.equal(document.activeElement, $('create-button'));
$('notifications-button').click();
assert.equal($('notifications-popover').hidden, false);
$('settings-button').click();
assert.equal($('notifications-popover').hidden, true);
assert.equal($('settings-popover').hidden, false);
document.dispatch('pointerdown', {target: document.body});
assert.equal($('settings-popover').hidden, true);
$('create-button').click();
$('create-menu').dispatch('keydown', {key: 'Tab'});
assert.equal($('create-menu').hidden, true);
assert.equal(document.activeElement, $('create-button'));
""")


def test_agent_draft_validates_then_saves_text_without_executing_markup():
    run_toolbar(r"""
$('create-button').click(); menuItems()[1].click();
assert.equal($('create-agent-dialog').open, true);
assert.equal(document.activeElement, $('agent-name'));
$('agent-name').value = '    '; submit();
assert.equal(writes, 0);
assert.equal($('create-agent-dialog').open, true);
assert.match(text($('create-agent-dialog')), /Enter an agent name/);
$('agent-name').value = ' <img src=x onerror=alert(1)> ';
$('agent-outbound-prompt').value = '<script>bad()</script>'; submit();
assert.equal(writes, 1);
assert.equal($('create-agent-dialog').open, false);
assert.equal(navigated, 1);
assert.equal(document.activeElement, $('page-title'));
assert.equal(JSON.parse(stored)[0].name, '<img src=x onerror=alert(1)>');
assert.match(text($('agents-view')), /<img src=x onerror=alert\(1\)>/);
assert.match(text($('agents-view')), /<script>bad\(\)<\/script>/);
assert.match(text($('agents-view')), /Draft · not connected/);
assert.equal($('agents-view').all().some(element => ['SCRIPT', 'IMG'].includes(element.tagName)), false);
""")


def test_agent_draft_failure_keeps_form_and_unsaved_input():
    run_toolbar(r"""
window.DashboardToolbar.openCreateAgent();
$('agent-name').value = 'Admissions';
$('agent-outbound-prompt').value = 'Hello.';
failWrites = true; submit();
assert.equal(writes, 0);
assert.equal(stored, null);
assert.equal($('create-agent-dialog').open, true);
assert.equal($('agent-name').value, 'Admissions');
assert.equal(navigated, 0);
assert.match(text($('create-agent-dialog')), /draft could not be saved/);
assert.match(text($('agents-view')), /No agent drafts yet/);
""")


def test_unreadable_drafts_are_preserved_instead_of_overwritten():
    run_toolbar(r"""
assert.match(text($('agents-view')), /existing data has been preserved/);
window.DashboardToolbar.openCreateAgent();
$('agent-name').value = 'Admissions'; submit();
assert.equal(writes, 0);
assert.equal(stored, '{broken');
assert.equal($('create-agent-dialog').open, true);
assert.match(text($('create-agent-dialog')), /No changes were made/);
""", before="stored = '{broken';")


def test_agent_drafts_reload_and_cancel_returns_to_create_trigger():
    drafts = [{"name": "Admissions", "prompt": "Hello from New College.", "createdAt": "2026-09-26T20:00:00Z"}]
    run_toolbar(r"""
assert.match(text($('agents-view')), /Admissions/);
assert.match(text($('agents-view')), /Hello from New College/);
assert.match(text($('agents-view')), /browser only/);
$('create-button').click(); menuItems()[1].click();
$('create-agent-close').click();
assert.equal($('create-agent-dialog').open, false);
assert.equal(document.activeElement, $('create-button'));
assert.equal(writes, 0);
""", before="stored = " + json.dumps(json.dumps(drafts)) + ";")
