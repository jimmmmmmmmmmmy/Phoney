"""Contact editing, categories, caller matching, and browser-local persistence."""

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
  removeAttribute(key) {delete this.attributes[key];}
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
  reset() {for (const item of this.all()) if (['INPUT', 'TEXTAREA', 'SELECT'].includes(item.tagName)) item.value = '';}
  remove() {if (this.parentNode) {this.parentNode.children = this.parentNode.children.filter(child => child !== this); this.parentNode = null;}}
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
const byRole = role => contactRoot.all().filter(element => element.getAttribute('role') === role);
const tabs = () => byRole('tab');
const tab = label => tabs().find(element => text(element).trim() === label);
const rows = () => contactRoot.all().find(element => element.tagName === 'TBODY')?.children || [];
const names = () => rows().map(row => text(row.all().find(element => element.tagName === 'A')).trim());
const hasClass = (element, name) => element.className.split(/\s+/).includes(name);
const hasMetrics = () => contactRoot.all().some(element => hasClass(element, 'crm-metrics'));
const searchInput = () => contactRoot.all().find(element => element.getAttribute('aria-label') === 'Search contacts');
const search = value => {searchInput().value = value; searchInput().dispatch('input');};
const navigate = hash => {window.location.hash = hash; window.DashboardCRM.render();};
const dialogButton = label => $('crm-create-contact').all().find(element => element.tagName === 'BUTTON' && text(element).trim() === label);
const editContact = () => {$('crm-edit-contact').click(); assert.equal($('crm-create-contact').open, true);};
const assertSelectedTab = label => {
  assert.equal(byRole('tabpanel').length, 1);
  const panel = byRole('tabpanel')[0];
  for (const element of tabs()) {
    const selected = text(element).trim() === label;
    assert.equal(element.getAttribute('aria-selected'), String(selected));
    assert.equal(Number(element.tabIndex ?? element.getAttribute('tabindex')), selected ? 0 : -1);
    assert.equal(element.getAttribute('aria-controls'), panel.id);
    if (selected) assert.equal(panel.getAttribute('aria-labelledby'), element.id);
  }
};
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


def test_contact_list_uses_category_tabs_and_labels_without_workspace_fluff():
    run_crm(r"""
const originalStorage = stored;
assert.equal(byRole('tablist').length, 1);
assert.deepEqual(tabs().map(element => text(element).trim()), ['All contacts', 'Real Estate', 'Legal', 'Customers']);
assertSelectedTab('All contacts');
assert.equal(hasMetrics(), false);
assert.doesNotMatch(text(contactRoot), /Demo workspace|fictional contacts and|sample conversations|saved in this browser|Demo contacts|This browser|Talk time|Follow-ups/);
assert.deepEqual(names(), ['Avery Chen', 'Alex Morgan', 'Maya Patel', 'Casey Reed', 'Jordan Ellis']);
for (const row of rows()) assert.doesNotMatch(text(row), /\bDemo\b|This browser/);
assert.match(text(rows().find(row => text(row).includes('Alex Morgan'))), /Real Estate/);
assert.match(text(rows().find(row => text(row).includes('Casey Reed'))), /Legal/);
for (const name of ['Maya Patel', 'Jordan Ellis']) assert.match(text(rows().find(row => text(row).includes(name))), /Customers/);
assert.doesNotMatch(text(rows()[0]), /Real Estate|Legal|Customers/);
for (const [label, expected] of [['Real Estate', ['Alex Morgan']], ['Legal', ['Casey Reed']], ['Customers', ['Maya Patel', 'Jordan Ellis']]]) {
  tab(label).click();
  assertSelectedTab(label);
  assert.deepEqual(names(), expected);
}
tab('All contacts').click();
assert.equal(names().includes('Avery Chen'), true);
assert.equal(stored, originalStorage);
assert.equal(writes, 0);
assert.equal(window.DashboardCRM.findContactByPhone(CONTACT.phone).name, 'Avery Chen');
""", before="window.location.hash = '#contacts'; storeContacts([CONTACT]);")


def test_contact_category_tabs_support_keyboard_navigation_and_focus():
    run_crm(r"""
tab('All contacts').focus();
for (const [key, label, expected] of [
  ['ArrowRight', 'Real Estate', ['Alex Morgan']],
  ['ArrowRight', 'Legal', ['Casey Reed']],
  ['End', 'Customers', ['Maya Patel', 'Jordan Ellis']],
  ['ArrowRight', 'All contacts', ['Alex Morgan', 'Maya Patel', 'Casey Reed', 'Jordan Ellis']],
  ['ArrowLeft', 'Customers', ['Maya Patel', 'Jordan Ellis']],
  ['Home', 'All contacts', ['Alex Morgan', 'Maya Patel', 'Casey Reed', 'Jordan Ellis']]
]) {
  const event = document.activeElement.dispatch('keydown', {key});
  assert.equal(event.defaultPrevented, true);
  assertSelectedTab(label);
  assert.equal(document.activeElement, tab(label));
  assert.deepEqual(names(), expected);
}
const event = document.activeElement.dispatch('keydown', {key: 'Tab'});
assert.equal(Boolean(event.defaultPrevented), false);
assertSelectedTab('All contacts');
""", before="window.location.hash = '#contacts';")


def test_category_search_survives_call_refresh_and_profiles_keep_call_metrics():
    run_crm(r"""
tab('Customers').click();
search('Maya');
assert.deepEqual(names(), ['Maya Patel']);
search('Alex');
assert.deepEqual(names(), []);
assert.match(text(contactRoot), /No contacts found/);
search('Maya');
searchInput().focus();
contactRoot.scrollTop = 240;
const input = searchInput();
window.DashboardCRM.setSessions([{call_sid: 'CA11111111111111111111111111111111', status: 'completed',
  started_at: '2026-09-25T17:00:00Z', ended_at: '2026-09-25T17:02:00Z',
  call_detail: {caller_number: '+19415550102', started_at: '2026-09-25T17:00:00Z', duration_seconds: 120,
    summary: {text: 'Confirmed a follow-up appointment.'}}}]);
window.DashboardCRM.render();
assertSelectedTab('Customers');
assert.equal(searchInput(), input);
assert.equal(searchInput().value, 'Maya');
assert.equal(document.activeElement, input);
assert.equal(contactRoot.scrollTop, 240);
assert.deepEqual(names(), ['Maya Patel']);
assert.equal(text(rows()[0].children[3]).trim(), '2');
assert.equal(hasMetrics(), false);
window.location.hash = '#contacts/demo-maya-patel'; window.DashboardCRM.render();
assert.equal(hasMetrics(), true);
assert.match(text(contactRoot), /Talk time/);
assert.match(text(contactRoot), /Confirmed a follow-up appointment/);
assert.match(text(contactRoot), /Sample transcript/);
window.location.hash = '#contacts'; window.DashboardCRM.render();
assertSelectedTab('Customers');
assert.equal(searchInput().value, 'Maya');
assert.deepEqual(names(), ['Maya Patel']);
assert.equal(hasMetrics(), false);
assert.equal(writes, 0);
""", before="window.location.hash = '#contacts';")


def test_profile_removes_notices_relationship_and_id_and_exposes_editable_details():
    run_crm(r"""
assert.equal(contactRoot.all().some(element => hasClass(element, 'crm-notice')), false);
const metrics = contactRoot.all().find(element => hasClass(element, 'crm-metrics'));
assert.equal(metrics.children.length, 3);
assert.deepEqual(metrics.children.map(item => text(item.children[0])), ['Conversations', 'Talk time', 'Last contact']);
assert.doesNotMatch(text(contactRoot), /Relationship|Contact ID|local-12345678|This browser|not shared with other devices/);
assert.equal($('crm-edit-contact').getAttribute('aria-label'), 'Edit contact details');
assert.equal($('crm-edit-contact').all().some(element => element.tagName === 'SVG'), true);
editContact();
assert.equal(text($('crm-dialog-title')), 'Edit contact');
assert.ok(dialogButton('Save changes'));
for (const [key, value] of Object.entries({firstName: 'Avery', lastName: 'Chen', phone: CONTACT.phone,
  email: CONTACT.email, address: '', website: '', company: '', createdAt: '2026-09-26', status: 'New', labels: ''})) {
  assert.ok($('crm-' + key), key + ' exists');
  assert.equal($('crm-' + key).value, value, key + ' is prefilled');
}
assert.equal($('crm-createdAt').type, 'date');
assert.equal($('crm-status').tagName, 'SELECT');
assert.equal(document.activeElement, $('crm-firstName'));
dialogButton('Cancel').click();
assert.equal($('crm-create-contact').open, false);
assert.equal(writes, 0);
""", before="window.location.hash = '#contacts/' + CONTACT.id; storeContacts([CONTACT]);")


def test_edit_saves_every_contact_field_and_refreshes_matching_categories_and_reload():
    run_crm(r"""
window.addEventListener('dashboard-contacts-changed', () => window.DashboardCRM.render());
editContact();
const newPhone = '+16562520999';
for (const [key, value] of Object.entries({firstName: ' Avery Updated ', lastName: ' Lee ', phone: '+1 (656) 252-0999',
  email: 'avery.lee@example.com', address: '123 Example Lane', website: 'https://example.com/avery',
  company: 'Example Legal', createdAt: '2026-08-12', status: 'Active', labels: 'Legal, Customers'})) $('crm-' + key).value = value;
submit();
assert.equal(writes, 1);
assert.equal($('crm-create-contact').open, false);
assert.equal(notifications.length, 2);
assert.equal(notifications.at(-1).caller, null);
assert.equal(window.DashboardCRM.findContactByPhone(CONTACT.phone), null);
assert.deepEqual(window.DashboardCRM.findContactByPhone(newPhone), {id: CONTACT.id, name: 'Avery Updated Lee', phone: newPhone});
let data = JSON.parse(stored);
assert.equal(data.version, 1);
assert.equal(data.contacts.length, 1);
const saved = data.contacts[0];
assert.equal(saved.id, CONTACT.id);
for (const [key, value] of Object.entries({firstName: 'Avery Updated', lastName: 'Lee', phone: newPhone,
  email: 'avery.lee@example.com', address: '123 Example Lane', website: 'https://example.com/avery',
  company: 'Example Legal', status: 'Active'})) assert.equal(saved[key], value, key);
assert.deepEqual(saved.labels, ['Legal', 'Customers']);
assert.equal(saved.createdAt.slice(0, 10), '2026-08-12');
navigate('#contacts/' + CONTACT.id);
assert.match(text(contactRoot), /Avery Updated Lee updated\./);
assert.match(text(contactRoot), /Example Legal/);
assert.match(text(contactRoot), /123 Example Lane/);
changeStorage();
editContact();
assert.equal($('crm-createdAt').value, '2026-08-12');
assert.equal($('crm-status').value, 'Active');
assert.equal($('crm-labels').value, 'Legal, Customers');
assert.equal($('crm-company').value, 'Example Legal');
dialogButton('Cancel').click();
navigate('#contacts');
tab('Legal').click();
assert.deepEqual(names(), ['Avery Updated Lee', 'Casey Reed']);
tab('Customers').click();
assert.deepEqual(names(), ['Avery Updated Lee', 'Maya Patel', 'Jordan Ellis']);
assert.equal(writes, 1);
""", before="window.location.hash = '#contacts/' + CONTACT.id; storeContacts([CONTACT]);")


def test_edit_allows_unchanged_phone_and_preserves_original_creation_timestamp():
    run_crm(r"""
editContact();
$('crm-firstName').value = 'Renamed';
submit();
assert.equal(writes, 1);
const saved = JSON.parse(stored).contacts[0];
assert.equal(saved.id, CONTACT.id);
assert.equal(saved.createdAt, CONTACT.createdAt);
assert.equal(saved.phone, CONTACT.phone);
assert.equal(notifications.at(-1).caller.name, 'Renamed Chen');
navigate('#contacts/' + CONTACT.id);
editContact();
$('crm-phone').value = '+1 (941) 555-0101';
submit();
assert.equal(writes, 1);
assert.equal(notifications.length, 2);
assert.equal($('crm-create-contact').open, true);
assert.match(text($('crm-create-contact')), /already exists/);
assert.equal(window.DashboardCRM.findContactByPhone(CONTACT.phone).name, 'Renamed Chen');
assert.equal(window.DashboardCRM.findContactByPhone('+19415550101').name, 'Alex Morgan');
dialogButton('Cancel').click();
assert.equal(writes, 1);
""", before="window.location.hash = '#contacts/' + CONTACT.id; storeContacts([CONTACT]);")


def test_edit_cancel_validation_and_failed_storage_leave_contact_unchanged():
    run_crm(r"""
const original = stored;
editContact();
$('crm-firstName').value = 'Unsaved';
dialogButton('Cancel').click();
assert.equal(stored, original);
assert.equal(window.DashboardCRM.findContactByPhone(CONTACT.phone).name, 'Avery Chen');
editContact();
assert.equal($('crm-firstName').value, 'Avery');
for (const [key, value] of [['firstName', ''], ['phone', '6562520233'], ['website', 'javascript:alert(1)'],
  ['address', 'a'.repeat(401)], ['company', 'c'.repeat(401)], ['createdAt', 'not-a-date'], ['createdAt', '2026-02-30'],
  ['status', 'Not a status'], ['labels', 'a'.repeat(41)], ['labels', Array.from({length: 11}, (_, index) => 'Label ' + index).join(', ')]]) {
  const input = $('crm-' + key), previous = input.value;
  input.value = value;
  submit();
  assert.equal(writes, 0, key + ' validation blocks writes');
  assert.equal(notifications.length, 1, key + ' validation does not notify');
  assert.equal(stored, original);
  assert.equal($('crm-create-contact').open, true);
  input.value = previous;
}
const email = $('crm-email');
email.value = 'invalid-email'; email.validity.valid = false; submit();
assert.equal(writes, 0); email.value = CONTACT.email; email.validity.valid = true;
$('crm-firstName').value = 'Unsaved';
failWrites = true;
submit();
assert.equal(writes, 0);
assert.equal(notifications.length, 1);
assert.equal(stored, original);
assert.equal($('crm-create-contact').open, true);
assert.match(text($('crm-create-contact')), /has not been saved/);
assert.equal(window.DashboardCRM.findContactByPhone(CONTACT.phone).name, 'Avery Chen');
dialogButton('Cancel').click();
editContact();
assert.equal($('crm-firstName').value, 'Avery');
""", before="window.location.hash = '#contacts/' + CONTACT.id; storeContacts([CONTACT]);")


def test_demo_edits_persist_as_overrides_without_losing_sample_history_or_local_contacts():
    run_crm(r"""
assert.match(text(contactRoot), /Consultation follow-up/);
const originalPhone = '+19415550101', changedPhone = '+19415550999';
editContact();
assert.equal($('crm-labels').value, 'Real Estate');
$('crm-firstName').value = 'Alex Updated';
$('crm-phone').value = changedPhone;
$('crm-status').value = 'Active';
$('crm-labels').value = 'Legal';
submit();
assert.equal(writes, 1);
const data = JSON.parse(stored);
assert.equal(data.contacts.length, 1);
assert.equal(data.contacts[0].id, CONTACT.id);
assert.equal(data.demoOverrides.length, 1);
assert.equal(data.demoOverrides[0].id, 'demo-alex-morgan');
assert.equal(data.demoOverrides[0].firstName, 'Alex Updated');
assert.equal(window.DashboardCRM.findContactByPhone(originalPhone), null);
assert.equal(window.DashboardCRM.findContactByPhone(changedPhone).name, 'Alex Updated Morgan');
navigate('#contacts/demo-alex-morgan');
changeStorage();
assert.match(text(contactRoot), /Consultation follow-up/);
assert.match(text(contactRoot), /Sample transcript/);
assert.match(text(contactRoot), /Alex reviewed the consultation options/);
assert.match(text(contactRoot), /Alex Updated Morgan/);
assert.equal(contactRoot.all().some(element => hasClass(element, 'crm-notice')), false);
assert.equal(window.DashboardCRM.findContactByPhone(changedPhone).name, 'Alex Updated Morgan');
editContact();
assert.equal($('crm-status').value, 'Active');
assert.equal($('crm-labels').value, 'Legal');
dialogButton('Cancel').click();
navigate('#contacts');
assert.equal(names().filter(name => name.includes('Alex')).length, 1);
assert.equal(names().includes('Avery Chen'), true);
tab('Legal').click();
assert.deepEqual(names(), ['Alex Updated Morgan', 'Casey Reed']);
tab('Real Estate').click();
assert.deepEqual(names(), []);
assert.equal(writes, 1);
window.DashboardCRM.openCreateContact();
$('crm-firstName').value = 'New'; $('crm-lastName').value = 'Person'; $('crm-phone').value = '+19415550888';
submit();
assert.equal(writes, 2);
assert.equal(JSON.parse(stored).contacts.length, 2);
assert.equal(JSON.parse(stored).demoOverrides.length, 1);
changeStorage();
assert.equal(window.DashboardCRM.findContactByPhone(changedPhone).name, 'Alex Updated Morgan');
""", before="window.location.hash = '#contacts/demo-alex-morgan'; storeContacts([CONTACT]);")
