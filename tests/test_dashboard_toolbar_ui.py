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
  listeners: {},
  addEventListener(type, listener) {(this.listeners[type] ||= []).push(listener);},
  dispatch(type, event = {}) {for (const listener of this.listeners[type] || []) listener(event);},
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
const call = (index, overrides = {}) => ({call_sid: 'CA' + String(index).padStart(32, '0'),
  status: 'live', started_at: '2026-09-26T20:00:00Z',
  call_detail: {caller_number: '+19415550101'}, ...overrides});
const notificationRows = () => $('notifications-popover').all().filter(element => element.tagName === 'LI');
const notificationLinks = () => $('notifications-popover').all().filter(element => element.tagName === 'A');
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


def test_live_call_notifications_count_deduplicate_and_ignore_completed_history():
    run_toolbar(r"""
const completed = call(1, {status: 'completed'});
const active = call(2);
window.DashboardToolbar.setSessions([completed, active]);
assert.equal($('notification-count').hidden, false);
assert.equal($('notification-count').textContent, '1');
assert.equal($('notifications-button').getAttribute('data-unread'), 'true');
assert.equal($('notifications-button').getAttribute('aria-label'), 'Notifications, 1 unread notification');
assert.equal(notificationRows().length, 1);
assert.match(text($('notifications-popover')), /\+19415550101/);
assert.match(text($('notifications-popover')), /Sep 26/);
for (let i = 0; i < 5; i++) window.DashboardToolbar.setSessions([completed, active]);
assert.equal(notificationRows().length, 1);
window.DashboardToolbar.setSessions([completed, {...active, status: 'completed', ended_at: '2026-09-26T20:04:00Z'}]);
assert.equal($('notification-count').textContent, '1');
assert.match(text($('notifications-popover')), /Ended/);
window.DashboardToolbar.setSessions([completed]);
assert.equal(notificationRows().length, 1, 'Call remains in notifications after leaving recent history');
window.DashboardToolbar.setSessions([call(3), call(4, {voicemail_only: true}), call(5, {call_detail: {ended_at: '2026-09-26T20:04:00Z'}})]);
assert.equal($('notification-count').textContent, '2');
assert.equal($('notifications-button').getAttribute('aria-label'), 'Notifications, 2 unread notifications');
assert.equal(writes, 0);
""")


def test_opening_notifications_acknowledges_calls_and_navigation_uses_call_id():
    run_toolbar(r"""
let selected = '';
window.DashboardCalls = {openCall: id => {selected = id;}};
window.DashboardCRM.findContactByPhone = phone => phone === '+19415550101' ? {name: 'Shane McCarthy'} : null;
window.DashboardToolbar.setSessions([call(1)]);
$('notifications-button').click();
assert.equal($('notification-count').hidden, true);
assert.equal($('notifications-button').getAttribute('aria-label'), 'Notifications');
assert.equal($('notifications-button').getAttribute('data-unread'), 'false');
assert.match(text($('notifications-popover')), /Shane McCarthy/);
window.DashboardToolbar.setSessions([call(1), call(2)]);
assert.equal($('notification-count').hidden, true, 'New calls visible in the open panel are already read');
const open = notificationLinks()[0];
assert.equal(open.href, '#calls/recent/' + call(2).call_sid);
assert.equal(open.getAttribute('aria-label'), 'Open call from Shane McCarthy');
const modified = open.dispatch('click', {button: 0, ctrlKey: true});
assert.equal(modified.defaultPrevented, undefined);
assert.equal(selected, '');
const event = open.dispatch('click', {button: 0});
assert.equal(event.defaultPrevented, true);
assert.equal(selected, call(2).call_sid);
assert.equal($('notifications-popover').hidden, true);
window.DashboardToolbar.setSessions([call(3)]);
assert.equal($('notification-count').textContent, '1');
assert.equal($('notification-count').hidden, false);
""")


def test_notifications_update_late_contact_metadata_safely_and_keep_focus_on_poll():
    run_toolbar(r"""
window.DashboardToolbar.setSessions([call(1, {call_detail: {}})]);
assert.match(text($('notifications-popover')), /Unknown caller/);
window.DashboardToolbar.setSessions([call(1)]);
assert.match(text($('notifications-popover')), /\+19415550101/);
let contactName = '<img src=x onerror=alert(1)>';
window.DashboardCRM.findContactByPhone = () => ({name: contactName});
window.dispatch('dashboard-contacts-changed');
assert.match(text($('notifications-popover')), /<img src=x onerror=alert\(1\)>/);
assert.equal($('notifications-popover').all().some(element => ['SCRIPT', 'IMG'].includes(element.tagName)), false);
$('notifications-button').click();
const open = notificationLinks()[0];
open.focus();
window.DashboardToolbar.setSessions([call(1)]);
assert.equal(notificationLinks()[0], open, 'Identical polling data should preserve focused link');
assert.equal(document.activeElement, open);
contactName = 'Shane McCarthy';
window.dispatch('dashboard-contacts-changed');
assert.match(text($('notifications-popover')), /Shane McCarthy/);
window.DashboardToolbar.setSessions([call(1, {call_detail: {}})]);
assert.equal(notificationLinks()[0].getAttribute('aria-label'), 'Open call from Shane McCarthy');
""")


def test_recent_notifications_are_bounded_and_invalid_sessions_are_ignored():
    run_toolbar(r"""
window.DashboardToolbar.setSessions([null, {call_sid: '<script>'}, call(1, {status: 'FAILED'}), call(2, {status: 'disabled'})]);
assert.equal(notificationRows().length, 0);
assert.equal($('notification-count').hidden, true);
window.DashboardToolbar.setSessions(null);
for (let i = 3; i < 28; i++) window.DashboardToolbar.setSessions([call(i)]);
assert.equal(notificationRows().length, 20);
assert.equal($('notification-count').textContent, '20');
assert.equal(notificationLinks()[0].href, '#calls/recent/' + call(27).call_sid);
window.DashboardToolbar.setSessions([call(3)]);
assert.equal(notificationRows().length, 20);
assert.equal(notificationLinks().length, 0, 'Trimmed calls must not return as new notifications');
""")


def test_missing_calls_stay_in_notifications_without_a_dead_open_action():
    run_toolbar(r"""
window.DashboardToolbar.setSessions([call(1)]);
assert.equal(notificationLinks().length, 1);
window.DashboardToolbar.setSessions([]);
assert.equal(notificationRows().length, 1, 'Keep the notification when recent history drops its call');
assert.equal(notificationLinks().length, 0, 'Do not offer an Open call link that cannot navigate');
assert.match(text($('notifications-popover')), /Unavailable/);
assert.match(text($('notifications-popover')), /Outside recent history/);
assert.doesNotMatch(text($('notifications-popover')), /Live|Ended/);
assert.equal($('notification-count').textContent, '1');
window.DashboardToolbar.setSessions([call(1)]);
assert.equal(notificationLinks().length, 1, 'An available call restores its action');
assert.equal(notificationLinks()[0].href, '#calls/recent/' + call(1).call_sid);
assert.equal($('notification-count').textContent, '1', 'Restored history is not a new notification');
window.DashboardToolbar.setSessions([call(1, {ended_at: '2026-09-26T20:04:00Z'})]);
assert.equal(notificationLinks().length, 1);
assert.match(text($('notifications-popover')), /Ended/);
""")
