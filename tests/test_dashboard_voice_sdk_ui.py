"""Focused product and boundary checks; test helpers live in support."""

import shutil
import subprocess

import pytest

from support.dashboard_browser_audio_ui import SCRIPT
from support.dashboard_voice_sdk_ui import run_sdk


def test_sdk_owns_wideband_media_and_uses_exact_authenticated_params_once():
    run_sdk(r"""
const audio=await prepareSdk();
assert.deepEqual(events,['microphone']);
assert.equal(contexts.length,0);assert.equal(captures.length,0);assert.equal(sockets.length,0);
assert.equal(devices.length,0,'Permission preparation never creates a provider call');
await sdkAttach(audio);
assert.equal(audio.connected,true);assert.equal(audio.transport,'twilio-voice-sdk');
assert.equal(audio.codec,'opus');
assert.deepEqual(devices[0].options.codecPreferences,['opus','pcmu']);
assert.equal(devices[0].options.logLevel,'silent');
assert.equal(devices[0].options.allowIncomingWhileBusy,false);
assert.deepEqual(devices[0].connects,[{params:{SessionId:sid,Token:'one-time-nonce'}}]);
assert.equal(await devices[0].options.getUserMedia(),stream,'SDK reuses click-granted microphone');
const incoming=new FakeCall();devices[0].emit('incoming',incoming);assert.equal(incoming.rejected,true);
audio.sendDigits('*');audio.sendDigits('#');audio.sendDigits('5');audio.sendDigits('#1');audio.sendDigits('abc');
assert.deepEqual(calls[0].digits,['*','#','5'],'Phone tones cannot become agent command strings');
assert.equal(contexts.length,0);assert.equal(captures.length,0);assert.equal(sockets.length,0,'SDK path never admits audio into legacy 8k transport');
await assert.rejects(audio.attach(sdkCredentials,sid),/already connecting/);
audio.stop();audio.stop();
assert.equal(devices[0].destroyed,true);assert.equal(calls[0].disconnects,1);
assert.equal(track.stopped,true);assert.equal(track.listeners.ended,undefined);
assert.equal(events.filter(value=>value==='track-stop').length,1);
assert.equal(audio.device,null);assert.equal(audio.call,null);assert.equal(timers.size,0);
""")


def test_cancelled_permission_releases_late_microphone_without_sdk_or_dial():
    run_sdk(r"""
let grant; navigator.mediaDevices.getUserMedia=()=>new Promise(resolve=>grant=resolve);
const controller=new AbortController();
const pending=prepareSdk({signal:controller.signal});controller.abort();
await assert.rejects(pending,/calling was cancelled/);
assert.equal(devices.length,0);grant(stream);await tick();
assert.equal(track.stopped,true);assert.equal(devices.length,0);assert.equal(captures.length,0);
""")


def test_stop_pending_connection_rejects_immediately_and_disconnects_late_sdk_call():
    run_sdk(r"""
let finishConnect;connectGate=new Promise(resolve=>finishConnect=resolve);
const audio=await prepareSdk();const pending=audio.attach(sdkCredentials,sid);await tick();
audio.stop();await assert.rejects(pending,/calling was cancelled/);
assert.equal(devices[0].destroyed,true);assert.equal(track.stopped,true);assert.equal(timers.size,0);
finishConnect();await tick();assert.equal(calls[0].disconnects,1);
assert.equal(audio.connected,false);assert.equal(devices[0].connects.length,1,'Late completion never redials');
""")


def test_actual_vendored_browser_bundle_installs_the_twilio_device_global():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for browser SDK tests")
    asset = SCRIPT.parents[1] / "public/vendor/twilio-voice-sdk-2.18.5.min.js"
    result = subprocess.run([node, "-e", r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const browser={console,setTimeout,clearTimeout,navigator:{}};
browser.window=browser;browser.self=browser;vm.createContext(browser);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),browser);
assert.equal(typeof browser.Twilio?.Device,'function');
assert.equal(typeof browser.Twilio?.Call,'function');
""", str(asset)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
