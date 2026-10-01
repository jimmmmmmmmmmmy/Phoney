"""Focused product and boundary checks; test helpers live in support."""

from support.workspace_unlock_ui import (run_unlock)


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
