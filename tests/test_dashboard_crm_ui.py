"""Caller/contact matching and browser-local contact change notifications."""

from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "public/dashboard-crm.js"

HARNESS = r"""
const assert = require('node:assert/strict');
const crypto = require('node:crypto').webcrypto;
class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.attributes = {};
    this.listeners = {}; this.className = ''; this.textContent = ''; this.hidden = false;
    this.value = ''; this.open = false; this.parentNode = null; this.dataset = {};
    this.scrollTop = 0; this.validity = {valid: true};
    this.classList = {add: name => {this.className += ' ' + name;}};
  }
  get isConnected() { return this === document.body || !!this.parentNode?.isConnected; }
  get elements() {return {namedItem: name => this.all().find(item => item.name === name) || null};}
  append(...elements) {for (const element of elements) {element.parentNode = this; this.children.push(element);}}
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
  focus() {document.activeElement = this;}
  all() {return [this, ...this.children.flatMap(child => child.all())];}
  querySelectorAll(selector) {
    return this.all().filter(child => selector === 'a[href]' ? child.tagName === 'A' && child.href :
      selector === 'button:not(:disabled)' ? child.tagName === 'BUTTON' && !child.disabled : child.tagName === selector.toUpperCase());
  }
  querySelector(selector) {return this.querySelectorAll(selector)[0] || null;}
  reset() {for (const item of this.all()) if (['INPUT', 'TEXTAREA'].includes(item.tagName)) item.value = '';}
  showModal() {this.open = true;}
  close() {this.open = false; this.dispatch('close');}
}
const Node = Element;
const FormData = class {constructor(form) {this.form = form;} get(name) {return this.form.elements.namedItem(name)?.value ?? null;}};
const CustomEvent = class {constructor(type) {this.type = type;}};
const document = {
  body: new Element('body'), readyState: 'complete', activeElement: null, listeners: {},
  createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag),
  createTextNode: value => {const node = new Element('#text'); node.textContent = String(value); return node;},
  getElementById(id) {return this.body.all().find(element => element.id === id) || null;},
  addEventListener(type, listener) {(this.listeners[type] ||= []).push(listener);},
  dispatch(type) {for (const listener of this.listeners[type] || []) listener();}
};
const contactRoot = new Element('section'); contactRoot.id = 'contacts-view'; document.body.append(contactRoot);
let stored = null, failWrites = false, writes = 0;
const localStorage = {
  getItem: () => stored,
  setItem: (key, value) => {if (failWrites) throw new Error('quota'); stored = value; writes++;}
};
const window = {
  location: {hash: '#calls/recent'}, localStorage, listeners: {},
  addEventListener(type, listener) {(this.listeners[type] ||= []).push(listener);},
  dispatchEvent(event) {for (const listener of this.listeners[event.type] || []) listener(event);}
};
const STORAGE_KEY = 'hacking-banyons.contacts.v1';
const CONTACT = {id: 'local-12345678', firstName: '  Avery ', lastName: ' Chen ', phone: '+16562520233',
  email: 'avery@example.com', address: '', website: '', createdAt: '2026-09-26T17:00:00Z'};
const storeContacts = contacts => {stored = JSON.stringify({version: 1, contacts});};
const changeStorage = (key = STORAGE_KEY, storageArea = localStorage) => window.dispatchEvent({type: 'storage', key, storageArea});
const notifications = [];
window.addEventListener('dashboard-contacts-changed', event => notifications.push({
  event, caller: window.DashboardCRM.findContactByPhone(CONTACT.phone),
  dialogReady: !!document.getElementById('crm-create-contact')
}));
const $ = id => document.getElementById(id);
const text = element => element.textContent + element.children.map(text).join(' ');
const submit = () => $('crm-create-contact').children[0].dispatch('submit');
"""


def run_crm(checks, *, before=""):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for dashboard behavior tests")
    result = subprocess.run(
        [node, "-e", HARNESS + before + "\n" + SCRIPT.read_text() + "\n" + checks],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_lookup_matches_exact_normalized_phone_and_returns_only_a_detached_identity():
    run_crm(r"""
const lookup = window.DashboardCRM.findContactByPhone;
const identity = lookup(' +1 (656) 252-0233 ');
assert.deepEqual(identity, {id: CONTACT.id, name: 'Avery Chen', phone: CONTACT.phone});
assert.deepEqual(lookup('+1 941.555.0101'), {id: 'demo-alex-morgan', name: 'Alex Morgan', phone: '+19415550101'});
for (const invalid of [null, undefined, 16562520233, '', 'Unknown caller', '6562520233', '16562520233',
  '+26562520233', '+165625202330', '+16562520233 ext 1', 'sip:+16562520233', '+016562520233']) {
  assert.equal(lookup(invalid), null, String(invalid));
}
identity.name = 'Changed'; identity.phone = '+15555550111';
assert.equal(lookup(CONTACT.phone).name, 'Avery Chen');
assert.equal(lookup(CONTACT.phone).phone, CONTACT.phone);
assert.equal(writes, 0);
""", before="storeContacts([CONTACT]);")


def test_initialization_notifies_after_stored_contacts_and_dialog_are_ready():
    run_crm(r"""
assert.equal(notifications.length, 0);
storeContacts([CONTACT]);
document.dispatch('DOMContentLoaded');
assert.equal(notifications.length, 1);
assert.equal(notifications[0].event instanceof CustomEvent, true);
assert.equal(notifications[0].caller.name, 'Avery Chen');
assert.equal(notifications[0].dialogReady, true);
assert.equal(window.location.hash, '#calls/recent');
assert.equal(writes, 0);
""", before="document.readyState = 'loading';")


def test_created_contact_is_visible_to_callers_only_after_successful_save():
    run_crm(r"""
window.DashboardCRM.openCreateContact();
$('crm-firstName').value = 'Avery'; $('crm-lastName').value = 'Chen';
$('crm-phone').value = '+1 (656) 252-0233';
failWrites = true; submit();
assert.equal(notifications.length, 1);
assert.equal(window.DashboardCRM.findContactByPhone(CONTACT.phone), null);
assert.equal($('crm-create-contact').open, true);
assert.match(text($('crm-create-contact')), /has not been saved/);
failWrites = false; submit();
assert.equal(writes, 1);
assert.equal(notifications.length, 2);
assert.equal(notifications[1].caller.name, 'Avery Chen');
assert.equal(notifications[1].caller.phone, CONTACT.phone);
assert.equal($('crm-create-contact').open, false);
assert.match(window.location.hash, /^contacts\/local-/);
window.DashboardCRM.openCreateContact();
$('crm-firstName').value = 'Duplicate'; $('crm-lastName').value = 'Person';
$('crm-phone').value = CONTACT.phone; submit();
assert.equal(writes, 1); assert.equal(notifications.length, 2);
assert.match(text($('crm-create-contact')), /already exists/);
""")


def test_storage_sync_refreshes_lookup_ignores_unrelated_changes_and_handles_removal():
    run_crm(r"""
assert.equal(notifications.length, 1);
storeContacts([CONTACT]);
changeStorage('other-key'); changeStorage(STORAGE_KEY, {});
assert.equal(notifications.length, 1);
assert.equal(window.DashboardCRM.findContactByPhone(CONTACT.phone), null);
changeStorage();
assert.equal(notifications.length, 2);
assert.equal(notifications.at(-1).caller.name, 'Avery Chen');
storeContacts([{...CONTACT, firstName: 'Avery Updated'}]); changeStorage();
assert.equal(notifications.at(-1).caller.name, 'Avery Updated Chen');
stored = null; changeStorage();
assert.equal(notifications.at(-1).caller, null);
storeContacts([CONTACT]); changeStorage();
stored = null; changeStorage(null);
assert.equal(notifications.at(-1).caller, null);
assert.equal(window.DashboardCRM.findContactByPhone('+19415550101').name, 'Alex Morgan');
assert.equal(writes, 0);
""")


def test_invalid_stored_contacts_cannot_supply_stale_caller_names():
    run_crm(r"""
assert.equal(window.DashboardCRM.findContactByPhone(CONTACT.phone).name, 'Avery Chen');
stored = '{broken'; changeStorage();
assert.equal(notifications.at(-1).caller, null);
assert.equal(window.DashboardCRM.findContactByPhone(CONTACT.phone), null);
window.DashboardCRM.openCreateContact();
$('crm-firstName').value = 'Avery'; $('crm-lastName').value = 'Chen'; $('crm-phone').value = CONTACT.phone;
submit();
assert.equal(writes, 0); assert.equal(stored, '{broken');
assert.match(text($('crm-create-contact')), /could not be read/);
assert.equal(window.DashboardCRM.findContactByPhone('+19415550101').name, 'Alex Morgan');
""", before="storeContacts([CONTACT]);")
