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
  show() {this.open = true; this.presentation = 'nonmodal';}
  showModal() {throw new Error('The floating dialer must not block site navigation with showModal()');}
  close() {this.open = false; this.dispatch('close');}
  requestSubmit() {this.dispatch('submit');}
  all() {return [this, ...this.children.flatMap(child => child.all())];}
  contains(target) {return this === target || this.children.some(child => child.contains(target));}
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
const panelContent = () => $('dialer-dialog').all().find(item => item.className.split(' ').includes('dialer-content'));
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
$('dialer-minimize').click();
assert.equal(panelContent().hidden, true);
assert.equal($('dialer-restore').hidden, false);
""", before="currentConfig.authenticated = false; currentConfig.demoMode = true;")


def test_public_calling_allows_visitors_to_start_and_end_without_an_access_code():
    run_dialer(r"""
assert.equal(currentConfig.authenticated, false);
assert.equal($('dialer-unlock-form').hidden, true);
assert.equal($('dialer-call-form').hidden, false);
assert.equal($('dialer-start').getAttribute('aria-label'), "Call owner's phone");
assert.equal($('dialer-description'), undefined);
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


def test_call_lifecycle_minimizing_keeps_call_running_and_navigation_available():
    run_dialer(r"""
$('dialer-button').click();
assert.equal($('dialer-dialog').presentation, 'nonmodal');
enterNumber('4155550123');
await submit('dialer-call-form');
assert.deepEqual(JSON.parse(outbound()[0].body), {to: destination, goal: ''});
assert.equal(outbound()[0].headers['X-Agent-Request'], '1');
assert.equal(outbound()[0].headers['Idempotency-Key'], '12345678-1234-1234-1234-000000000001');
assert.match(text($('dialer-status')), /Ringing your phone/);
assert.equal($('dialer-call-form').hidden, true);
$('dialer-minimize').click();
assert.equal($('dialer-dialog').getAttribute('data-minimized'), 'true');
assert.equal(panelContent().hidden, true);
assert.equal($('dialer-restore').hidden, false);
assert.equal(writes().length, 1, 'Minimizing never ends a call');
const navigation = new Element('button'); let navigated = 0;
navigation.addEventListener('click', () => navigated++); document.body.append(navigation);
navigation.focus(); navigation.click();
assert.equal(navigated, 1);
for (const [phase, expected] of [['owner_prompt', 'Press 1 on your phone'], ['remote_setup', 'Calling the other person'], ['connected', 'Connected']]) {
  currentSession.phase = phase;
  window.dispatch('focus'); await settle();
  assert.match(text($('dialer-status')), new RegExp(expected));
}
assert.equal($('dialer-button').textContent, 'Call in progress');
$('dialer-restore').click();
assert.equal($('dialer-dialog').getAttribute('data-minimized'), 'false');
assert.equal(panelContent().hidden, false);
assert.equal($('dialer-restore').hidden, true);
$('dialer-minimize').click(); $('dialer-button').click();
assert.equal(panelContent().hidden, false, 'New call also restores the current call panel');
assert.equal(writes().length, 1);
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
await submit('dialer-call-form');
assert.equal(outbound().length, 1);
assert.equal($('dialer-start').getAttribute('aria-label'), 'Retry same request');
assert.equal($('dialer-key-1').disabled, true);
assert.equal($('dialer-backspace').disabled, true);
assert.match(text($('dialer-error')), /couldn't confirm/);
window.dispatch('online'); window.dispatch('focus'); intervals.forEach(callback => callback()); await settle();
assert.equal(outbound().length, 1, 'Reconnection only reads call status');
enterNumber('+14155559999');
await submit('dialer-call-form');
assert.equal(outbound().length, 2);
assert.equal(outbound()[0].headers['Idempotency-Key'], outbound()[1].headers['Idempotency-Key']);
assert.equal(outbound()[0].body, outbound()[1].body);
""")


def test_country_scope_accepts_us_syntax_but_keeps_server_region_rejections_visible():
    run_dialer(r"""
assert.equal($('dialer-number-help'), undefined);
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
for (const id of ['dialer-country', 'dialer-number-help', 'dialer-goal', 'dialer-description', 'dialer-close', 'dialer-title', 'dialer-start-label']) {
  assert.equal($(id), undefined, id + ' should be removed from the compact keypad');
}
assert.equal($('dialer-start').children[0].tagName, 'SVG');
assert.equal($('dialer-start').disabled, true);
assert.equal($('dialer-clear').disabled, true);
assert.equal($('dialer-backspace').disabled, true);
assert.equal($('dialer-number').textContent, '');
assert.equal($('dialer-call-form').all().some(item => item.tagName === 'DETAILS'), false);
$('dialer-button').click();
assert.equal($('dialer-dialog').presentation, 'nonmodal');
assert.equal(document.activeElement, $('dialer-number'));
enterNumber('4155550123');
assert.equal($('dialer-number').textContent, '415 555 0123');
assert.equal($('dialer-start').disabled, false);
$('dialer-backspace').click();
assert.equal($('dialer-number').textContent, '415 555 012');
$('dialer-clear').click();
assert.equal($('dialer-number').textContent, '');
assert.equal($('dialer-start').disabled, true);
const elsewhere = new Element('button'); document.body.append(elsewhere); elsewhere.focus();
document.dispatch('keydown', {key: 'Escape', target: elsewhere});
assert.equal(panelContent().hidden, false, 'Escape during site navigation must not minimize the dialer');
$('dialer-number').focus(); press('Escape');
assert.equal(panelContent().hidden, true);
assert.equal($('dialer-dialog').getAttribute('data-minimized'), 'true');
press('9', $('dialer-restore'));
$('dialer-restore').click();
assert.equal(panelContent().hidden, false);
assert.equal($('dialer-number').textContent, '');
assert.equal(writes().length, 0);
""")


@pytest.mark.parametrize("digits", ["4155550123", "14155550123"])
def test_us_keypad_normalizes_the_country_code_exactly_once(digits):
    run_dialer(r"""
enterNumber(DIGITS);
assert.equal($('dialer-country'), undefined);
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
const elsewhere = new Element('textarea'); document.body.append(elsewhere);
press('9', elsewhere); press('9', $('dialer-code'));
press('9', $('dialer-number'), {ctrlKey: true});
press('9', $('dialer-number'), {metaKey: true});
assert.equal($('dialer-number').textContent, '415 555 0123');
press('Backspace'); assert.equal($('dialer-number').textContent, '415 555 012');
press('3'); press('*'); press('#');
assert.equal($('dialer-number').textContent, '4155550123*#');
press('Delete'); assert.equal($('dialer-number').textContent, '');
for (const key of '4155550123') press(key);
press('Enter', elsewhere); await settle();
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
assert.equal($('dialer-country'), undefined);
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
assert.equal($('dialer-number').textContent, '');
assert.equal($('dialer-start').disabled, true, 'Oversized paste clears the previous target instead of truncating it');
paste('+1 (415) 555-0123');
assert.equal($('dialer-number').textContent, '415 555 0123');
assert.equal($('dialer-country'), undefined);
press('Enter'); await settle();
assert.equal(outbound().length, 1);
assert.equal(JSON.parse(outbound()[0].body).to, destination);
""")


BROWSER_SETUP = r"""
const callingConfig=currentConfig, reply=response;
const dial=value=>enterNumber(value);
let browserHandler;
handler=request=>browserHandler(request.url,request);
callingConfig.self_call_number = '+13125550101';
const audioEvents = [];
let microphoneFailure = null;
window.DashboardBrowserAudio = {async prepare() {
  audioEvents.push('prepare');
  if (microphoneFailure) throw new Error(microphoneFailure);
  const audio = {connected:false,sessionId:null,stopped:false,
    async attach(credentials, id) {audioEvents.push('attach'); this.sessionId=id; this.connected=true;},
    stop() {if(!this.stopped)audioEvents.push('stop'); this.stopped=true; this.connected=false;}};
  audioEvents.push(audio);
  return audio;
}};
const selfId = sid;
browserHandler = async(path, options) => {
  if(path==='/api/calls/config')return reply(callingConfig);
  if(path==='/api/calls/outbound') {
    audioEvents.push('outbound');
    callingConfig.active_session={id:selfId,phase:'owner_ringing',browser_audio:true};
    return reply({session_id:selfId,phase:'owner_ringing',browser_audio:true});
  }
  if(path===`/api/sessions/${selfId}/browser-token`)return reply({url:`/browser-media/${selfId}/`,token:'private-token'});
  if(path===`/api/sessions/${selfId}/end`) {
    callingConfig.active_session=null;
    return reply({id:selfId,phase:'ended',browser_audio:true});
  }
  if(path===`/api/sessions/${selfId}`)return reply({id:selfId,phase:'ended',browser_audio:true});
  throw Error('Unexpected request: '+path);
};
"""


def test_owner_number_uses_browser_before_outbound_and_minimize_keeps_audio():
    run_dialer(r"""
$('dialer-button').click(); await tick();
dial('3125550101');
assert.equal($('dialer-start').getAttribute('aria-label'),'Call from this browser');
assert.equal($('dialer-self-call-help').hidden,false);
assert.equal(audioEvents.length,0,'Number entry must not open the microphone');
$('dialer-call-form').dispatch('submit'); await tick();
assert.equal(audioEvents[0],'prepare');
assert.equal(audioEvents[2],'outbound');
assert.equal(audioEvents[3],'attach');
assert.deepEqual(JSON.parse(requests.find(item=>item.url==='/api/calls/outbound').body),{to:'+13125550101',goal:''});
assert.equal($('dialer-resume-audio').hidden,true);
assert.match(text($('dialer-status')), /no need to press 1/);
assert.match(text($('dialer-status')), /Speak and listen here/);
$('dialer-minimize').click(); await tick();
assert.equal(audioEvents[1].stopped,false,'Minimizing must keep browser audio running');
callingConfig.active_session.phase='connected';
window.dispatch('focus'); await tick();
$('dialer-restore').click(); await tick();
assert.match(text($('dialer-status')), /Keep this tab open/);
$('dialer-end').click(); await tick();
assert.equal(audioEvents[1].stopped,true);
assert.match(text($('dialer-status')), /browser and phone connections have been closed/);
""", before=BROWSER_SETUP)


def test_microphone_denial_never_reserves_or_dials_a_self_call():
    run_dialer(r"""
$('dialer-button').click(); await tick(); dial('3125550101');
$('dialer-call-form').dispatch('submit'); await tick();
assert.deepEqual(audioEvents,['prepare']);
assert.equal(requests.filter(item=>item.method==='POST').length,0);
assert.match(text($('dialer-error')), /Allow microphone access/);
assert.equal($('dialer-start').getAttribute('aria-label'),'Call from this browser');
""", before=BROWSER_SETUP + "microphoneFailure='Allow microphone access for this site, then try the call again.';\n")


def test_recovered_browser_call_requires_explicit_microphone_attach():
    run_dialer(r"""
assert.equal(audioEvents.length,0);
$('dialer-restore').click(); await tick();
assert.equal(audioEvents.length,0,'Restoring a recovered call must not steal its microphone/socket');
assert.equal($('dialer-resume-audio').hidden,false);
assert.match(text($('dialer-status')), /another tab/);
$('dialer-resume-audio').click(); await tick();
assert.equal(audioEvents[0],'prepare');
assert.equal(audioEvents[2],'attach');
assert.equal(requests.filter(item=>item.url==='/api/calls/outbound').length,0);
assert.equal($('dialer-resume-audio').hidden,true);
window.dispatch('pagehide');
assert.equal(audioEvents[1].stopped,true);
""", before=BROWSER_SETUP + "callingConfig.active_session={id:selfId,phase:'reserved',browser_audio:true};\n")


def test_unknown_self_call_outcome_stops_microphone_and_preserves_retry_key():
    run_dialer(r"""
$('dialer-button').click(); await tick(); dial('3125550101');
const normalHandler=browserHandler;
browserHandler=async(path,options)=>path==='/api/calls/outbound'?Promise.reject(Error('lost response')):normalHandler(path,options);
$('dialer-call-form').dispatch('submit'); await tick();
assert.equal(audioEvents[1].stopped,true);
assert.match(text($('dialer-error')), /Your microphone is off/);
const first=requests.find(item=>item.url==='/api/calls/outbound');
window.dispatch('focus'); await tick();
assert.equal(requests.filter(item=>item.url==='/api/calls/outbound').length,1);
$('dialer-call-form').dispatch('submit'); await tick();
const starts=requests.filter(item=>item.url==='/api/calls/outbound');
assert.equal(starts.length,2);
assert.equal(starts[0].headers['Idempotency-Key'],starts[1].headers['Idempotency-Key']);
assert.equal(audioEvents.filter(value=>value==='prepare').length,2);
assert.ok(audioEvents.filter(value=>typeof value==='object').every(audio=>audio.stopped));
""", before=BROWSER_SETUP)


def test_authenticated_access_loss_closes_browser_audio():
    run_dialer(r"""
$('dialer-button').click(); await tick(); dial('3125550101');
$('dialer-call-form').dispatch('submit'); await tick();
callingConfig.authenticated=false; callingConfig.public_calling=true;
window.dispatch('focus'); await tick();
assert.equal(audioEvents[1].stopped,true);
assert.equal($('dialer-resume-audio').hidden,true);
""", before=BROWSER_SETUP)


def test_us_owner_number_does_not_change_international_allowlist_normalization():
    run_dialer(r"""
$('dialer-button').click(); await tick();
$('dialer-dialog').dispatch('paste',{target:$('dialer-number'),clipboardData:{getData:()=>'+44 20 7946 0958'}});
$('dialer-call-form').dispatch('submit'); await tick();
const start=requests.find(item=>item.url==='/api/calls/outbound');
assert.equal(JSON.parse(start.body).to,'+442079460958');
assert.equal(audioEvents.length,0,'International callback calls must not acquire browser audio');
""", before=BROWSER_SETUP + r"""
callingConfig.destinations=['+442079460958'];
browserHandler=async(path,options)=>{
 if(path==='/api/calls/config')return reply(callingConfig);
 if(path==='/api/calls/outbound') {
  callingConfig.active_session={id:selfId,phase:'owner_ringing'};
  return reply({session_id:selfId,phase:'owner_ringing'});
 }
 throw Error('Unexpected request: '+path);
};
""")


def test_pagehide_while_microphone_permission_is_pending_cancels_without_a_post():
    run_dialer(r"""
$('dialer-button').click(); await tick(); dial('3125550101');
const originalPrepare=window.DashboardBrowserAudio.prepare;
let allowMicrophone;
window.DashboardBrowserAudio.prepare=async()=>{
 await new Promise(resolve=>allowMicrophone=resolve);
 return originalPrepare();
};
$('dialer-call-form').dispatch('submit'); await tick();
window.dispatch('pagehide');
allowMicrophone(); await tick();
assert.equal(audioEvents[1].stopped,true);
assert.equal(requests.filter(item=>item.url==='/api/calls/outbound').length,0);
assert.match(text($('dialer-error')), /calling was cancelled/);
assert.equal($('dialer-start').disabled,false);
""", before=BROWSER_SETUP)


def test_browser_token_failure_stops_mic_and_explicit_resume_does_not_create_another_call():
    run_dialer(r"""
$('dialer-button').click(); await tick(); dial('3125550101');
const normalHandler=browserHandler;
browserHandler=async(path,options)=>path.endsWith('/browser-token')?reply({detail:'Browser connection unavailable'},503):normalHandler(path,options);
$('dialer-call-form').dispatch('submit'); await tick();
assert.equal(audioEvents[1].stopped,true);
assert.equal($('dialer-resume-audio').hidden,false);
assert.match(text($('dialer-error')), /Browser connection unavailable/);
window.dispatch('focus'); await tick();
assert.equal(requests.filter(item=>item.url==='/api/calls/outbound').length,1);
browserHandler=normalHandler;
$('dialer-resume-audio').click(); await tick();
assert.equal(requests.filter(item=>item.url==='/api/calls/outbound').length,1);
assert.equal(audioEvents.filter(item=>item==='prepare').length,2);
assert.equal(audioEvents.at(-1),'attach');
assert.equal($('dialer-resume-audio').hidden,true);
""", before=BROWSER_SETUP)


def test_anonymous_owner_number_cannot_acquire_browser_audio():
    run_dialer(r"""
$('dialer-button').click(); await tick(); dial('3125550101');
assert.equal($('dialer-start').getAttribute('aria-label'),"Call owner's phone");
assert.equal($('dialer-self-call-help').hidden,true);
const normalHandler=browserHandler;
browserHandler=async(path,options)=>path==='/api/calls/outbound'?reply({detail:'Cannot start a call: destination-not-allowed'},403):normalHandler(path,options);
$('dialer-call-form').dispatch('submit'); await tick();
assert.deepEqual(audioEvents,[]);
assert.equal(requests.filter(item=>item.url.endsWith('/browser-token')).length,0);
assert.match(text($('dialer-error')), /owner's phone and the service number cannot be called/);
""", before=BROWSER_SETUP + "callingConfig.authenticated=false;callingConfig.public_calling=true;callingConfig.countries=['US'];\n")


SDK_BROWSER_SETUP = BROWSER_SETUP + r"""
callingConfig.browser_voice_enabled=true;
callingConfig.manual_takeover_enabled=true;
callingConfig.voice_ready=true;
callingConfig.agents=[{slot:'1',name:'James'}, {slot:'2',name:'Reception'}];
const preparedOptions=[], sentDigits=[];
const prepareLegacyFake=window.DashboardBrowserAudio.prepare;
window.DashboardBrowserAudio.prepare=async options=>{
 preparedOptions.push(options);
 const audio=await prepareLegacyFake();
 audio.transport='twilio-voice-sdk';audio.codec='opus';audio.sendDigits=digit=>sentDigits.push(digit);
 return audio;
};
const sdkBaseHandler=browserHandler;
browserHandler=async(path,options)=>{
 if(path.endsWith('/takeover')||path.endsWith('/mode')) {
  const body=JSON.parse(options.body);
  callingConfig.active_session.mode=path.endsWith('/takeover')?'agent':body.mode;
  return reply({session_id:selfId,mode:callingConfig.active_session.mode});
 }
 const result=await sdkBaseHandler(path,options);
 if(path==='/api/calls/outbound') {
  callingConfig.active_session.audio_path='native-conference';
  callingConfig.active_session.mode='human';
 }
 return result;
};
const connectBrowser=async()=>{
 $('dialer-button').click();dial(destination);
 await submit('dialer-call-form');
 callingConfig.active_session.phase='connected';
 window.dispatch('focus');await settle();
};
"""


def test_sdk_flag_uses_browser_audio_for_any_approved_destination_without_phone_callback():
    run_dialer(r"""
$('dialer-button').click();dial(destination);
assert.equal($('dialer-start').getAttribute('aria-label'),'Call from this browser');
assert.equal(audioEvents.length,0,'Entering a destination does not request microphone permission');
await submit('dialer-call-form');
assert.equal(preparedOptions[0].transport,'twilio-voice-sdk');
assert.ok(preparedOptions[0].signal instanceof AbortSignal);
assert.deepEqual(JSON.parse(outbound()[0].body),{to:destination,goal:'',browser_audio:true});
assert.equal(audioEvents[0],'prepare');assert.equal(audioEvents[2],'outbound');
callingConfig.active_session.phase='connected';window.dispatch('focus');await settle();
assert.match(text($('dialer-status')),/Browser audio connected/);
assert.match(text($('dialer-status')),/Browser audio connected \(Opus\)/);
assert.equal($('dialer-live-keypad').hidden,false);
assert.equal($('dialer-agent-controls').hidden,false);
assert.equal(text($('dialer-dialog')).includes('private-token'),false);
$('dialer-minimize').click();assert.equal(audioEvents[1].stopped,false);
$('dialer-restore').click();await settle();
$('dialer-end').click();await settle();
assert.equal(audioEvents[1].stopped,true);assert.equal(outbound().length,1);
""", before=SDK_BROWSER_SETUP)


def test_sdk_connected_status_displays_actual_pcmu_fallback_without_claiming_opus():
    run_dialer(r"""
await connectBrowser();audioEvents[1].codec='pcmu';window.dispatch('focus');await settle();
assert.match(text($('dialer-status')),/Browser audio connected \(PCMU\)/);
assert.equal(text($('dialer-status')).includes('Opus'),false);
""", before=SDK_BROWSER_SETUP)


def test_sdk_phone_tones_and_authenticated_agent_commands_use_separate_transports():
    run_dialer(r"""
await connectBrowser();
const writesBefore=writes().length;
$('dialer-live-key-5').click();$('dialer-live-key-star').click();$('dialer-live-key-hash').click();
assert.deepEqual(sentDigits,['5','*','#']);assert.equal(writes().length,writesBefore);
$('dialer-agent-1').click();$('dialer-agent-1').click();await settle();
const takeover=writes().filter(item=>item.url.endsWith('/takeover'));
assert.equal(takeover.length,1,'Double click cannot duplicate agent takeover');
assert.deepEqual(JSON.parse(takeover[0].body),{slot:'1'});
assert.equal(takeover[0].credentials,'same-origin');assert.equal(takeover[0].headers['X-Agent-Request'],'1');
assert.match(text($('dialer-status')),/AI agent speaking/);
assert.equal($('dialer-agent-human').disabled,false);
$('dialer-agent-human').click();await settle();
const release=writes().find(item=>item.url.endsWith('/mode'));
assert.deepEqual(JSON.parse(release.body),{mode:'human'});
assert.deepEqual(sentDigits,['5','*','#'],'Agent commands never send hash/star escape digits into conference');
assert.equal(audioEvents[1].stopped,false);assert.equal(outbound().length,1);
""", before=SDK_BROWSER_SETUP)


def test_sdk_disconnect_closes_human_session_once_without_auto_redial():
    run_dialer(r"""
await connectBrowser();const audio=audioEvents[1];
audio.stop();audio.onDisconnect('The call ended.');await settle();
assert.equal(writes().filter(item=>item.url.endsWith('/end')).length,1);
assert.equal(outbound().length,1);assert.match(text($('dialer-status')),/Call ended/);
assert.equal($('dialer-agent-controls').hidden,true);assert.equal($('dialer-live-keypad').hidden,true);
audio.onDisconnect('The call ended.');await settle();
assert.equal(writes().filter(item=>item.url.endsWith('/end')).length,1);
""", before=SDK_BROWSER_SETUP)


def test_sdk_microphone_denial_cannot_create_nonself_call_or_retry_it_automatically():
    run_dialer(r"""
$('dialer-button').click();dial(destination);await submit('dialer-call-form');
assert.deepEqual(audioEvents,['prepare']);assert.equal(outbound().length,0);
window.dispatch('focus');intervals.forEach(callback=>callback());await settle();
assert.equal(outbound().length,0);assert.match(text($('dialer-error')),/Allow microphone/);
""", before=SDK_BROWSER_SETUP + "microphoneFailure='Allow microphone access for this site, then try the call again.';")


def test_public_callers_cannot_enable_sdk_or_show_agent_controls_even_with_flag_present():
    run_dialer(r"""
$('dialer-button').click();dial(destination);await submit('dialer-call-form');
assert.equal(preparedOptions.length,0);assert.equal(audioEvents.includes('prepare'),false);
assert.deepEqual(JSON.parse(outbound()[0].body),{to:destination,goal:''});
assert.equal($('dialer-agent-controls').hidden,true);assert.equal($('dialer-live-keypad').hidden,true);
""", before=SDK_BROWSER_SETUP + r"""
callingConfig.authenticated=false;callingConfig.public_calling=true;
browserHandler=async(path,options)=>{
 if(path==='/api/calls/config')return reply(callingConfig);
 if(path==='/api/calls/outbound') {
  callingConfig.active_session={id:selfId,phase:'connected'};
  return reply({session_id:selfId,phase:'connected',browser_audio:false});
 }
 return sdkBaseHandler(path,options);
};
""")
