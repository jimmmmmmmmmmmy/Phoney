"""Outgoing SDK media lifecycle with fakes; never creates a real provider call."""

import hashlib
from pathlib import Path
import shutil
import subprocess

import pytest

from test_dashboard_browser_audio_ui import HARNESS, SCRIPT


SDK_HARNESS = HARNESS + r"""
const timers = new Map(); let timerId = 0;
const setTimeout = (callback, ms) => {timers.set(++timerId, {callback, ms}); return timerId;};
const clearTimeout = id => timers.delete(id);
class Emitter {
  constructor(){this.listeners={};}
  on(name, handler){(this.listeners[name] ||= []).push(handler);return this;}
  emit(name, ...args){for(const handler of [...(this.listeners[name] || [])])handler(...args);}
  removeAllListeners(){this.listeners={};}
}
class FakeCall extends Emitter {
  constructor(){super();this.state='pending';this.codec='opus';this.digits=[];this.disconnects=0;}
  status(){return this.state;}
  accept(){this.state='open';this.emit('accept');}
  disconnect(){this.disconnects++;this.state='closed';this.emit('disconnect');}
  sendDigits(value){this.digits.push(value);}
  reject(){this.rejected=true;}
}
const devices=[], calls=[];let connectGate=null, connectFailure=false;
class Device extends Emitter {
  constructor(token, options){super();this.token=token;this.options=options;this.connects=[];this.destroyed=false;devices.push(this);}
  async connect(options){this.connects.push(options);if(connectFailure)throw Error('jwt-secret');
    const call=new FakeCall();calls.push(call);if(connectGate)await connectGate;return call;}
  register(){throw Error('Outgoing browser must not register for incoming calls');}
  destroy(){this.destroyed=true;this.emit('error',Error('jwt-secret'));}
}
window.Twilio={Device};
window.localStorage={getItem(){throw Error('Token persistence forbidden');},setItem(){throw Error('Token persistence forbidden');}};
window.sessionStorage=window.localStorage;
const sdkCredentials={transport:'twilio-voice-sdk',token:'jwt-secret',params:{SessionId:sid,Token:'one-time-nonce'}};
const prepareSdk=options=>window.DashboardBrowserAudio.prepare({transport:'twilio-voice-sdk',...options});
const sdkAttach=async audio=>{const pending=audio.attach(sdkCredentials,sid);await tick();calls.at(-1).accept();await pending;};
"""


def run_sdk(checks, *, before=""):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for browser SDK tests")
    result = subprocess.run(
        [node, "-e", SDK_HARNESS + before + "\n" + SCRIPT.read_text()
         + "\n(async()=>{\n" + checks
         + "\n})().catch(error=>{console.error(error);process.exitCode=1;});"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("before,message", [
    ("window.isSecureContext=false;", "Browser calling needs HTTPS"),
    ("window.Twilio=undefined;", "The calling library did not load"),
    ("window.Twilio={Device:{}};", "The calling library did not load"),
    ("navigator.mediaDevices=undefined;", "This browser cannot access the microphone"),
])
def test_sdk_capability_failures_identify_the_missing_requirement_without_dialing(before, message):
    run_sdk(r"""
await assert.rejects(prepareSdk(),error=>error.message.startsWith(MESSAGE));
assert.deepEqual(events,[],'Capability failure never asks for the microphone');
assert.equal(devices.length,0);assert.equal(calls.length,0);assert.equal(sockets.length,0);
""".replace("MESSAGE", repr(message)), before=before)


def test_mobile_webkit_capabilities_can_use_sdk_without_desktop_audio_worklet():
    run_sdk(r"""
const audio=await prepareSdk();await sdkAttach(audio);
assert.equal(audio.connected,true);assert.equal(audio.codec,'opus');
assert.deepEqual(events,['microphone']);
assert.equal(contexts.length,0);assert.equal(captures.length,0);assert.equal(sockets.length,0);
audio.stop();assert.equal(track.stopped,true);
""", before=r"""
navigator.userAgent='Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 Version/18.6 Mobile/15E148 Safari/604.1';
navigator.vendor='Apple Computer, Inc.';
window.AudioContext=undefined;window.webkitAudioContext=undefined;AudioWorkletNode=undefined;
""")


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


@pytest.mark.parametrize("codec", ["opus", "pcmu"])
def test_codec_reports_the_accepted_sdk_negotiation_not_the_preference(codec):
    run_sdk(r"""
const audio=await prepareSdk();assert.equal(audio.codec,null);
const pending=audio.attach(sdkCredentials,sid);await tick();
assert.equal(audio.codec,null,'No codec label before call acceptance');
calls[0].codec=CODEC;calls[0].accept();await pending;
assert.equal(audio.codec,CODEC);assert.deepEqual(devices[0].options.codecPreferences,['opus','pcmu']);
audio.stop();assert.equal(audio.codec,null);
""".replace("CODEC", repr(codec)))


def test_sdk_validation_rejects_another_session_and_extra_dial_targets_before_connect():
    run_sdk(r"""
const audio=await prepareSdk();
for(const params of [{SessionId:'b'.repeat(32),Token:'nonce'}, {SessionId:sid,Token:'nonce',To:'+15555550123'}, {SessionId:sid,Token:''}]) {
 await assert.rejects(audio.attach({...sdkCredentials,params},sid),/Invalid browser audio response/);
}
await assert.rejects(audio.attach({...sdkCredentials,transport:'legacy'},sid),/Invalid browser audio response/);
assert.equal(devices.length,0);assert.equal(sockets.length,0);audio.stop();
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


@pytest.mark.parametrize("event", ["disconnect", "error", "microphone"])
def test_sdk_disconnect_revocation_and_failure_notify_once_and_stop_every_device(event):
    trigger = "track.dispatch('ended')" if event == "microphone" else f"calls[0].emit('{event}',Error('jwt-secret'))"
    run_sdk(r"""
const audio=await prepareSdk();await sdkAttach(audio);const notifications=[];
audio.onDisconnect=message=>notifications.push(message);
""" + trigger + r""";
assert.equal(notifications.length,1);assert.equal(notifications[0].includes('jwt-secret'),false);
assert.equal(audio.stopped,true);assert.equal(audio.connected,false);assert.equal(track.stopped,true);
assert.equal(devices[0].destroyed,true);assert.equal(devices[0].connects.length,1);
audio.stop();assert.equal(notifications.length,1);
""")


def test_sdk_connect_failure_and_timeout_never_leak_token_or_redial():
    run_sdk(r"""
connectFailure=true;const first=await prepareSdk();
await assert.rejects(first.attach(sdkCredentials,sid),error=>/could not connect/.test(error.message)&&!error.message.includes('jwt-secret'));
assert.equal(first.stopped,true);assert.equal(devices[0].connects.length,1);
connectFailure=false;track.readyState='live';const second=await prepareSdk();
const pending=second.attach(sdkCredentials,sid);await tick();
const timeout=[...timers.values()][0];assert.equal(timeout.ms,10000);timeout.callback();
await assert.rejects(pending,/did not connect/);assert.equal(second.stopped,true);
assert.equal(devices[1].connects.length,1);assert.equal(timers.size,0);
""")


def test_vendored_sdk_is_pinned_unmodified_and_loaded_before_the_adapter():
    root = SCRIPT.parents[1]
    asset = root / "public/vendor/twilio-voice-sdk-2.18.5.min.js"
    assert hashlib.sha256(asset.read_bytes()).hexdigest() == "35d3cb1b22e309f9884724a89250aecd2de4f1556ad7b67a7bc5e06c73dcb74a"
    assert "Apache License" in (asset.parent / "twilio-voice-sdk-LICENSE.md").read_text()
    html = (root / "dashboard.html").read_text()
    assert html.index('/assets/vendor/twilio-voice-sdk-2.18.5.min.js') < html.index('/assets/dashboard-browser-audio.js')
    assert "https://sdk.twilio.com" not in html


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
