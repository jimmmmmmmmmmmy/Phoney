"""Callback dialer behavior with offline DOM and network fakes; never calls a phone."""

from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "public/dashboard-dialer.js"
HARNESS = r"""
const assert = require('node:assert/strict');
class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.attributes = {};
    this.listeners = {}; this.textContent = ''; this.hidden = false; this.value = '';
    this.disabled = false; this.open = false; this.className = '';
  }
  append(...items) {this.children.push(...items);}
  prepend(item) {this.children.unshift(item);}
  replaceChildren(...items) {this.children = items;}
  setAttribute(name, value) {this.attributes[name] = String(value);}
  getAttribute(name) {return this.attributes[name];}
  addEventListener(type, callback) {(this.listeners[type] ||= []).push(callback);}
  dispatch(type, event = {}) {event.target ||= this; event.preventDefault ||= () => {}; for (const callback of this.listeners[type] || []) callback(event);}
  click() {if (!this.disabled) this.dispatch('click');}
  focus() {document.activeElement = this;}
  showModal() {this.open = true;}
  close() {this.open = false; this.dispatch('close');}
  requestSubmit() {this.dispatch('submit');}
  all() {return [this, ...this.children.flatMap(child => child.all())];}
}
const document = {
  body: new Element('body'), hidden: false, readyState: 'complete', listeners: {},
  createElement: tag => new Element(tag),
  createElementNS: (_, tag) => new Element(tag),
  getElementById(id) {return this.body.all().find(item => item.id === id);},
  addEventListener(type, callback) {(this.listeners[type] ||= []).push(callback);},
  dispatch(type, event = {}) {for (const callback of this.listeners[type] || []) callback(event);}
};
const mount = new Element('div'); mount.id = 'header-actions'; document.body.append(mount);
const window = {
  listeners: {},
  addEventListener(type, callback) {(this.listeners[type] ||= []).push(callback);},
  dispatch(type, event = {}) {for (const callback of this.listeners[type] || []) callback(event);}
};
const navigator = {onLine: true};
let serial = 0;
const crypto = {randomUUID: () => '12345678-1234-1234-1234-' + String(++serial).padStart(12, '0')};
const intervals = [], timeouts = new Map(); let timeoutId = 0;
const setInterval = callback => intervals.push(callback);
const setTimeout = (callback, ms) => {timeouts.set(++timeoutId, {callback, ms}); return timeoutId;};
const clearTimeout = id => timeouts.delete(id);
const requests = [], sid = 'a'.repeat(32), destination = '+14155550123';
let currentConfig = {authenticated: true, public_calling: false, enabled: true, owner_label: '•••• 0101', destinations: [destination], countries: [], active_session: null, busy: false};
let currentSession = null, handler = null;
const clone = value => JSON.parse(JSON.stringify(value));
const response = (body, status = 200) => ({ok: status < 400, status, json: async () => clone(body)});
async function fetch(url, options) {
  const request = {url, ...options}; requests.push(request);
  if (handler) {const result = await handler(request); if (result) return result;}
  if (url === '/api/calls/config') return response(currentConfig);
  if (url === '/api/agents/session') {currentConfig.authenticated = true; return response({authenticated: true});}
  if (url === '/api/calls/outbound') {
    currentSession = {id: sid, phase: 'owner_ringing', direction: 'outbound'};
    currentConfig.active_session = currentSession; currentConfig.busy = true;
    return response({session_id: sid, phase: 'owner_ringing'}, 202);
  }
  if (url === `/api/sessions/${sid}/end`) {
    currentSession = {...currentSession, phase: 'ended', ended_reason: 'admin-end'};
    currentConfig.active_session = null; currentConfig.busy = false;
    return response(currentSession);
  }
  if (url === `/api/sessions/${sid}`) return currentSession ? response(currentSession) : response({detail: 'Unknown session'}, 404);
  throw new Error('Unexpected URL ' + url);
}
const $ = id => document.getElementById(id);
const tick = () => new Promise(resolve => setImmediate(resolve));
const settle = async () => {await tick(); await tick();};
const submit = async id => {$(id).dispatch('submit'); await settle();};
const writes = () => requests.filter(request => request.method === 'POST');
const outbound = () => writes().filter(request => request.url === '/api/calls/outbound');
const text = element => element.textContent + element.children.map(text).join(' ');
const enterNumber = value => {
  $('dialer-clear').click();
  for (const digit of value.replace(/[+\s().-]/g, '')) $('dialer-key-' + (digit === '*' ? 'star' : digit === '#' ? 'hash' : digit)).click();
};
const press = (key, target = $('dialer-number'), extra = {}) => $('dialer-dialog').dispatch('keydown', {key, target, ...extra});
"""


def run_dialer(checks, *, before=""):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for dashboard behavior tests")
    result = subprocess.run(
        [node, "-e", HARNESS + before + "\n" + SCRIPT.read_text()
         + "\n(async () => {await settle();\n" + checks
         + "\n})().catch(error => {console.error(error); process.exitCode = 1;});"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_private_calling_requires_actual_owner_unlock_and_sends_no_admin_secret():
    run_dialer(r"""
assert.equal($('dialer-unlock-form').hidden, false);
assert.equal($('dialer-call-form').hidden, true);
$('dialer-button').click();
assert.equal($('dialer-dialog').open, true);
$('dialer-code').value = 'one-time-code';
await submit('dialer-unlock-form');
assert.equal($('dialer-unlock-form').hidden, true);
assert.equal($('dialer-call-form').hidden, false);
assert.equal($('dialer-code').value, '');
assert.equal(document.activeElement, $('dialer-number'));
const unlock = writes()[0];
assert.deepEqual(JSON.parse(unlock.body), {code: 'one-time-code'});
assert.equal(unlock.headers['X-Agent-Request'], '1');
assert.equal(unlock.credentials, 'same-origin');
assert.equal(requests.some(request => 'Authorization' in request.headers), false);
$('dialer-close').click();
assert.equal(document.activeElement, $('dialer-button'));
""", before="currentConfig.authenticated = false; currentConfig.demoMode = true;")


def test_public_calling_allows_visitors_to_start_and_end_without_an_access_code():
    run_dialer(r"""
assert.equal(currentConfig.authenticated, false);
assert.equal($('dialer-unlock-form').hidden, true);
assert.equal($('dialer-call-form').hidden, false);
assert.equal($('dialer-start').getAttribute('aria-label'), "Call owner's phone");
assert.match(text($('dialer-description')), /ring the owner's phone first/);
$('dialer-button').click();
enterNumber(destination);
await submit('dialer-call-form');
assert.equal(outbound().length, 1);
assert.equal(writes().some(request => request.url === '/api/agents/session'), false);
assert.equal(outbound()[0].headers['X-Agent-Request'], '1');
assert.equal(currentConfig.authenticated, false, 'Public access never pretends to authenticate the visitor');
assert.match(text($('dialer-status')), /Ringing the owner's phone/);
currentSession.phase = 'owner_prompt';
window.dispatch('focus'); await settle();
assert.match(text($('dialer-status')), /Waiting for the owner/);
assert.match(text($('dialer-status')), /owner must press 1/);
currentSession.phase = 'connected';
window.dispatch('focus'); await settle();
assert.match(text($('dialer-status')), /owner and the other person are connected/);
$('dialer-end').click(); await settle();
assert.equal(writes().length, 2);
assert.equal(writes()[1].url, `/api/sessions/${sid}/end`);
assert.match(text($('dialer-status')), /Call ended/);
assert.equal($('dialer-unlock-form').hidden, true);
""", before="currentConfig.authenticated = false; currentConfig.public_calling = true; currentConfig.countries = ['US']; currentConfig.destinations = [];")


def test_public_calling_recovers_minimal_status_after_reload_and_network_change():
    run_dialer(r"""
assert.equal($('dialer-button').textContent, 'Call in progress');
assert.equal($('dialer-status').hidden, false);
assert.equal($('dialer-end').hidden, false);
assert.match(text($('dialer-status')), /Connected/);
navigator.onLine = false; window.dispatch('offline');
assert.equal($('dialer-end').disabled, true);
assert.match(text($('dialer-dialog')), /phone call can continue/);
navigator.onLine = true; window.dispatch('online'); await settle();
assert.equal($('dialer-end').disabled, false);
currentConfig.active_session = null; currentConfig.busy = false;
currentSession = {id: sid, direction: 'outbound', phase: 'ended', ended_reason: 'owner-no-answer'};
window.dispatch('focus'); await settle();
assert.match(text($('dialer-status')), /owner's phone wasn't answered/);
assert.equal($('dialer-another').hidden, false);
assert.equal(writes().length, 0, 'Recovery never requires an unlock or starts another call');
""", before="currentConfig.authenticated = false; currentConfig.public_calling = true; currentSession = {id: sid, direction: 'outbound', phase: 'connected', ended_reason: ''}; currentConfig.active_session = currentSession; currentConfig.busy = true;")


def test_switching_public_calling_off_does_not_leave_an_anonymous_call_control():
    run_dialer(r"""
assert.equal($('dialer-end').hidden, false);
currentConfig.public_calling = false; currentConfig.active_session = null;
window.dispatch('focus'); await settle();
assert.equal($('dialer-unlock-form').hidden, false);
assert.equal($('dialer-call-form').hidden, true);
assert.equal($('dialer-status').hidden, true);
assert.equal($('dialer-end').hidden, true);
enterNumber(destination);
await submit('dialer-call-form');
$('dialer-end').click(); await settle();
assert.equal(writes().length, 0);
""", before="currentConfig.authenticated = false; currentConfig.public_calling = true; currentConfig.active_session = {id: sid, phase: 'connected'}; currentConfig.busy = true;")


def test_call_lifecycle_closing_keeps_call_running_and_end_is_explicit():
    run_dialer(r"""
$('dialer-button').click();
enterNumber('4155550123');
$('dialer-goal').value = 'Discuss our meeting';
await submit('dialer-call-form');
assert.deepEqual(JSON.parse(outbound()[0].body), {to: destination, goal: 'Discuss our meeting'});
assert.equal(outbound()[0].headers['X-Agent-Request'], '1');
assert.equal(outbound()[0].headers['Idempotency-Key'], '12345678-1234-1234-1234-000000000001');
assert.match(text($('dialer-status')), /Ringing your phone/);
assert.equal($('dialer-call-form').hidden, true);
$('dialer-close').click();
assert.equal(writes().length, 1, 'Closing never ends a call');
for (const [phase, expected] of [['owner_prompt', 'Press 1 on your phone'], ['remote_setup', 'Calling the other person'], ['connected', 'Connected']]) {
  currentSession.phase = phase;
  window.dispatch('focus'); await settle();
  assert.match(text($('dialer-status')), new RegExp(expected));
}
assert.equal($('dialer-button').textContent, 'Call in progress');
$('dialer-end').click(); await settle();
assert.equal(writes().length, 2);
assert.equal(writes()[1].url, `/api/sessions/${sid}/end`);
assert.match(text($('dialer-status')), /Call ended/);
assert.equal($('dialer-end').hidden, true);
$('dialer-another').click(); await settle();
assert.equal($('dialer-call-form').hidden, false);
assert.equal(writes().length, 2);
""")


def test_ambiguous_failure_preserves_request_identity_and_payload_without_auto_redial():
    run_dialer(r"""
let attempts = 0;
handler = async request => {
  if (request.url === '/api/calls/outbound' && ++attempts === 1) throw new Error('Connection lost');
};
enterNumber(destination);
$('dialer-goal').value = 'Original goal';
await submit('dialer-call-form');
assert.equal(outbound().length, 1);
assert.equal($('dialer-start').getAttribute('aria-label'), 'Retry same request');
assert.equal($('dialer-key-1').disabled, true);
assert.equal($('dialer-backspace').disabled, true);
assert.match(text($('dialer-error')), /couldn't confirm/);
window.dispatch('online'); window.dispatch('focus'); intervals.forEach(callback => callback()); await settle();
assert.equal(outbound().length, 1, 'Reconnection only reads call status');
enterNumber('+14155559999');
$('dialer-goal').value = 'Changed after failure';
await submit('dialer-call-form');
assert.equal(outbound().length, 2);
assert.equal(outbound()[0].headers['Idempotency-Key'], outbound()[1].headers['Idempotency-Key']);
assert.equal(outbound()[0].body, outbound()[1].body);
""")


def test_country_scope_accepts_us_syntax_but_keeps_server_region_rejections_visible():
    run_dialer(r"""
assert.match(text($('dialer-number-help')), /US numbers/);
enterNumber('+442079460000');
await submit('dialer-call-form');
assert.equal(outbound().length, 0);
assert.match(text($('dialer-error')), /US phone number/);
handler = async request => request.url === '/api/calls/outbound'
  ? response({detail: 'Cannot start a call: destination-not-allowed'}, 403) : null;
enterNumber('+14165550123');
await submit('dialer-call-form');
assert.equal(outbound().length, 1, 'The server checks actual country, including Canada sharing +1');
assert.match(text($('dialer-error')), /allowed US phone number/);
assert.equal($('dialer-key-1').disabled, false);
""", before="currentConfig.destinations = []; currentConfig.countries = ['US'];")


def test_ambiguous_response_recovers_existing_call_instead_of_showing_retry():
    run_dialer(r"""
handler = async request => {
  if (request.url === '/api/calls/outbound') {
    currentConfig.active_session = {id: sid, phase: 'owner_ringing'};
    currentConfig.busy = true;
    throw new Error('Response lost after the call started');
  }
};
enterNumber(destination);
await submit('dialer-call-form');
assert.equal(outbound().length, 1);
assert.match(text($('dialer-status')), /Ringing your phone/);
assert.equal($('dialer-error').hidden, true);
assert.equal($('dialer-call-form').hidden, true);
""")


def test_reloads_recover_active_calls_offline_keeps_state_and_never_places_a_call():
    run_dialer(r"""
assert.equal($('dialer-button').textContent, 'Call in progress');
assert.match(text($('dialer-status')), /Connected/);
navigator.onLine = false; window.dispatch('offline');
assert.equal($('dialer-end').disabled, true);
assert.match(text($('dialer-dialog')), /phone call can continue/);
const reads = requests.length;
window.dispatch('focus'); intervals.forEach(callback => callback()); await settle();
assert.equal(requests.length, reads);
navigator.onLine = true; window.dispatch('online'); await settle();
assert.equal($('dialer-end').disabled, false);
assert.equal(writes().length, 0);
currentConfig.active_session = null; currentConfig.busy = false;
currentSession = {...currentSession, phase: 'ended', ended_reason: 'remote-no-answer'};
window.dispatch('focus'); await settle();
assert.match(text($('dialer-status')), /other person didn't answer/);
""", before="currentSession = {id: sid, phase: 'connected'}; currentConfig.active_session = currentSession; currentConfig.busy = true;")


def test_double_submit_and_stale_background_reads_cannot_duplicate_or_erase_call():
    run_dialer(r"""
let releaseDial, releaseConfig;
const dialGate = new Promise(resolve => releaseDial = resolve);
const configGate = new Promise(resolve => releaseConfig = resolve);
let gateRead = true;
handler = async request => {
  if (request.url === '/api/calls/config' && gateRead) {
    gateRead = false; const stale = clone(currentConfig); await configGate; return response(stale);
  }
  if (request.url === '/api/calls/outbound') await dialGate;
};
window.dispatch('focus'); await tick();
enterNumber(destination);
$('dialer-call-form').dispatch('submit');
$('dialer-call-form').dispatch('submit');
assert.equal(outbound().length, 1);
releaseDial(); await settle();
assert.match(text($('dialer-status')), /Ringing your phone/);
releaseConfig(); await settle();
assert.match(text($('dialer-status')), /Ringing your phone/);
assert.equal(outbound().length, 1);
""")


def test_hung_reads_abort_after_ten_seconds_and_resume_on_focus():
    run_dialer(r"""
let hang = true;
handler = request => {
  if (request.url === '/api/calls/config' && hang) return new Promise((_, reject) => {
    request.signal.addEventListener('abort', () => reject(new Error('Aborted')));
  });
};
window.dispatch('focus'); await tick();
assert.equal(timeouts.size, 1);
const timer = [...timeouts.values()][0]; assert.equal(timer.ms, 10000); timer.callback();
await settle();
assert.match(text($('dialer-error')), /unavailable right now/);
hang = false; window.dispatch('focus'); await settle();
assert.equal($('dialer-error').hidden, true);
assert.equal(timeouts.size, 0);
assert.equal(writes().length, 0);
""")


def test_polling_does_not_reannounce_unchanged_live_status():
    run_dialer(r"""
const title = $('dialer-status').children[0];
let updates = 0, value = title.textContent;
Object.defineProperty(title, 'textContent', {get: () => value, set: next => {value = next; updates++;}});
window.dispatch('focus'); await settle();
intervals.forEach(callback => callback()); await settle();
assert.equal(updates, 0);
currentConfig.active_session.phase = 'connected';
window.dispatch('focus'); await settle();
assert.equal(updates, 1);
assert.equal(value, 'Connected');
""", before="currentConfig.active_session = {id: sid, phase: 'owner_ringing'}; currentConfig.busy = true;")


def test_phone_keypad_has_all_twelve_keys_and_a_noneditable_number_display():
    run_dialer(r"""
const keys = $('dialer-keypad').children;
assert.deepEqual(keys.map(key => key.children[0].textContent), ['1','2','3','4','5','6','7','8','9','*','0','#']);
assert.equal(keys.every(key => key.tagName === 'BUTTON' && key.type === 'button'), true);
assert.equal($('dialer-key-star').getAttribute('aria-label'), 'Star (*)');
assert.equal($('dialer-key-hash').getAttribute('aria-label'), 'Hash (#)');
assert.equal($('dialer-number').tagName, 'OUTPUT');
assert.equal($('dialer-call-form').all().some(item => item.tagName === 'INPUT'), false);
assert.equal($('dialer-country').textContent, '+1');
assert.equal($('dialer-start').children[0].tagName, 'SVG');
assert.equal($('dialer-start').disabled, true);
assert.equal($('dialer-clear').disabled, true);
assert.equal($('dialer-backspace').disabled, true);
assert.equal($('dialer-call-form').all().find(item => item.tagName === 'DETAILS').open, false);
$('dialer-button').click();
assert.equal(document.activeElement, $('dialer-number'));
enterNumber('4155550123');
assert.equal($('dialer-number').textContent, '415 555 0123');
assert.equal($('dialer-start').disabled, false);
$('dialer-backspace').click();
assert.equal($('dialer-number').textContent, '415 555 012');
$('dialer-clear').click();
assert.equal($('dialer-number').textContent, 'Phone number');
assert.equal($('dialer-start').disabled, true);
assert.equal(writes().length, 0);
""")


@pytest.mark.parametrize("digits", ["4155550123", "14155550123"])
def test_us_keypad_normalizes_the_country_code_exactly_once(digits):
    run_dialer(r"""
enterNumber(DIGITS);
assert.equal($('dialer-country').textContent, '+1');
assert.equal($('dialer-number').textContent, '415 555 0123');
$('dialer-backspace').click();
assert.equal($('dialer-number').textContent, '415 555 012', 'Deleting after country-code entry edits only the phone number');
$('dialer-key-3').click();
await submit('dialer-call-form');
assert.equal(outbound().length, 1);
assert.deepEqual(JSON.parse(outbound()[0].body), {to: '+14155550123', goal: ''});
""".replace("DIGITS", repr(digits)))


def test_star_and_hash_are_visible_but_never_silently_removed_from_a_call_target():
    run_dialer(r"""
enterNumber('4155550123*#');
assert.equal($('dialer-number').textContent, '4155550123*#');
await submit('dialer-call-form');
assert.equal(outbound().length, 0);
assert.match(text($('dialer-error')), /Remove \* and # before calling/);
assert.equal($('dialer-number').textContent, '4155550123*#', 'Invalid symbols remain visible for correction');
$('dialer-backspace').click();
assert.equal($('dialer-number').textContent, '4155550123*');
await submit('dialer-call-form');
assert.equal(outbound().length, 0);
$('dialer-backspace').click();
await submit('dialer-call-form');
assert.equal(outbound().length, 1);
assert.equal(JSON.parse(outbound()[0].body).to, destination);
""")


def test_keyboard_digits_editing_and_enter_respect_text_fields_and_shortcuts():
    run_dialer(r"""
$('dialer-button').click();
for (const key of '4155550123') press(key);
assert.equal($('dialer-number').textContent, '415 555 0123');
press('9', $('dialer-goal')); press('9', $('dialer-code'));
press('9', $('dialer-number'), {ctrlKey: true});
press('9', $('dialer-number'), {metaKey: true});
assert.equal($('dialer-number').textContent, '415 555 0123');
press('Backspace'); assert.equal($('dialer-number').textContent, '415 555 012');
press('3'); press('*'); press('#');
assert.equal($('dialer-number').textContent, '4155550123*#');
press('Delete'); assert.equal($('dialer-number').textContent, 'Phone number');
for (const key of '4155550123') press(key);
press('Enter', $('dialer-goal')); await settle();
assert.equal(outbound().length, 0);
press('Enter'); await settle();
assert.equal(outbound().length, 1);
assert.equal(JSON.parse(outbound()[0].body).to, destination);
""")


def test_explicit_international_prefix_and_unsupported_paste_never_change_the_dial_target():
    run_dialer(r"""
$('dialer-button').click();
for (const key of '+4420794600') press(key);
assert.equal($('dialer-number').textContent, '+4420794600');
assert.equal($('dialer-country').hidden, true);
press('Enter'); await settle();
assert.equal(outbound().length, 0, 'An explicit +44 number must not become a US +1 target');
assert.match(text($('dialer-error')), /Other country codes are not supported/);
const paste = value => $('dialer-dialog').dispatch('paste', {target: $('dialer-number'), clipboardData: {getData: () => value}});
paste('415 555 0123 ext 9');
assert.equal($('dialer-number').textContent, '4155550123ext9');
press('Enter'); await settle();
assert.equal(outbound().length, 0, 'Extension text cannot be stripped to call a different number');
assert.match(text($('dialer-error')), /without extension text or letters/);
press('Delete');
for (const key of '+112345678901') press(key);
press('Enter'); await settle();
assert.equal(outbound().length, 0, 'A second leading 1 cannot be stripped after the country prefix has already been consumed');
paste('9'.repeat(40));
assert.equal($('dialer-number').textContent, 'Phone number');
assert.equal($('dialer-start').disabled, true, 'Oversized paste clears the previous target instead of truncating it');
paste('+1 (415) 555-0123');
assert.equal($('dialer-number').textContent, '415 555 0123');
assert.equal($('dialer-country').hidden, false);
press('Enter'); await settle();
assert.equal(outbound().length, 1);
assert.equal(JSON.parse(outbound()[0].body).to, destination);
""")
