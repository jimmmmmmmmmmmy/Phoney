"""Shared fixtures and fakes for focused integration checks."""

import pytest


from support.dashboard_playback_ui import run_browser_logic


TIMERS = r"""
const timers = new Map(); let timerId = 0;
setTimeout = (callback, delay) => {const id = ++timerId; timers.set(id, {callback, delay}); return id;};
clearTimeout = id => timers.delete(id);
const tick = () => new Promise(resolve => setImmediate(resolve));
function fireTimer(delay) {
 const entry = [...timers].find(([, timer]) => timer.delay === delay);
 assert.ok(entry, `Expected a ${delay}ms timer`);
 timers.delete(entry[0]); return entry[1].callback();
}
"""
