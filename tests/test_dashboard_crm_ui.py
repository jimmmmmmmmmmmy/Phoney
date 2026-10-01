"""Focused product and boundary checks; test helpers live in support."""

from support.dashboard_crm_ui import (run_crm)


def test_created_contact_is_visible_to_callers_only_after_successful_save():
    run_crm(r"""
window.DashboardCRM.openCreateContact();
$('crm-firstName').value = 'Avery'; $('crm-lastName').value = 'Chen';
$('crm-phone').value = '+1 (656) 252-0233';
failWrites = true; await submit();
assert.equal(notifications.length, 1);
assert.equal(window.DashboardCRM.findContactByPhone(CONTACT.phone), null);
assert.equal($('crm-create-contact').open, true);
assert.match(text($('crm-create-contact')), /Could not confirm your contact was saved/);
failWrites = false; await submit();
assert.equal(writes, 1);
assert.equal(notifications.length, 2);
assert.equal(notifications[1].caller.name, 'Avery Chen');
assert.equal(notifications[1].caller.phone, CONTACT.phone);
assert.equal($('crm-create-contact').open, false);
assert.match(window.location.hash, /^contacts\/local-/);
window.DashboardCRM.openCreateContact();
$('crm-firstName').value = 'Duplicate'; $('crm-lastName').value = 'Person';
$('crm-phone').value = CONTACT.phone; await submit();
assert.equal(writes, 1); assert.equal(notifications.length, 2);
assert.match(text($('crm-create-contact')), /already exists/);
""")


def test_edit_saves_every_contact_field_and_refreshes_matching_categories_and_reload():
    run_crm(r"""
window.addEventListener('dashboard-contacts-changed', () => window.DashboardCRM.render());
editContact();
const newPhone = '+16562520999';
for (const [key, value] of Object.entries({firstName: ' Avery Updated ', lastName: ' Lee ', phone: '+1 (656) 252-0999',
  email: 'avery.lee@example.com', address: '123 Example Lane', website: 'https://example.com/avery',
  company: 'Example Legal', createdAt: '2026-08-12', status: 'Active', labels: 'Legal, Customers'})) $('crm-' + key).value = value;
await submit();
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
assert.deepEqual(rows()[0].children[1].all().filter(element => hasClass(element, 'crm-label')).map(text), ['Legal', 'Customers']);
tab('Customers').click();
assert.deepEqual(names(), ['Avery Updated Lee', 'Maya Patel', 'Jordan Ellis']);
assert.equal(writes, 1);
""", before="window.location.hash = '#contacts/' + CONTACT.id; storeContacts([CONTACT]);")
