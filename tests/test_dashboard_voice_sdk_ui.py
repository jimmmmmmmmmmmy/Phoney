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


def test_sdk_quality_samples_warnings_metadata_and_final_report_exclude_private_fields():
    run_sdk(r"""
const reports=[];window.fetch=async(url,options)=>{reports.push({url,options,body:JSON.parse(options.body)});return {ok:true};};
navigator.userAgent='Mozilla/5.0 Macintosh Chrome/140.0 Safari/537.36';
Device.version='2.18.5';
track.getSettings=()=>({deviceId:'private-device',groupId:'private-group',label:'Private microphone',
  sampleRate:48000,channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:false});
const audio=await prepareSdk();await sdkAttach(audio);
calls[0].emit('sample',{jitter:37,rtt:470,mos:2.7,packetsLostFraction:7,packetsLost:3,
  packetsReceived:44,packetsSent:50,audioInputLevel:2200,audioOutputLevel:1100,
  codecName:'opus',timestamp:123456,totals:{packetsLost:35},remoteAddress:'192.0.2.1',token:'jwt-secret'});
calls[0].emit('warning','high-jitter',{message:'jwt-secret',samples:[{ip:'192.0.2.1'}]});
calls[0].emit('warning-cleared','high-jitter');
calls[0].emit('warning','jwt-secret');
audio.stop();await tick();
assert.equal(reports.length,1);const report=reports[0];
assert.equal(report.url,`/api/sessions/${sid}/browser-quality`);
assert.equal(report.options.credentials,'same-origin');assert.equal(report.options.keepalive,true);
assert.deepEqual(report.options.headers,{'Content-Type':'application/json','X-Agent-Request':'1'});
assert.equal(report.body.final,true);assert.equal(report.body.codec,'opus');
assert.equal(report.body.samples[0].jitter,37);assert.equal(report.body.samples[0].packetsLostFraction,7);
assert.deepEqual(report.body.warnings.map(value=>[value.name,value.cleared]),[['high-jitter',false],['high-jitter',true]]);
assert.deepEqual(report.body.device,{browser:'chrome',platform:'mac',sdk_version:'2.18.5',
  audio_track_count:1,sample_rate:48000,channel_count:1,echo_cancellation:true,noise_suppression:true,auto_gain_control:false});
for(const secret of ['jwt-secret','one-time-nonce','private-device','private-group','Private microphone','192.0.2.1','123456']) {
  assert.equal(report.options.body.includes(secret),false,secret);
}
assert.equal(timers.size,0);assert.equal(calls[0].disconnects,1);
""")


def test_sdk_quality_reporting_is_bounded_and_failed_fetch_never_interrupts_call():
    run_sdk(r"""
const reports=[];window.fetch=async(url,options)=>{reports.push(JSON.parse(options.body));throw Error('network failed');};
const audio=await prepareSdk();await sdkAttach(audio);
for(let i=0;i<1000;i++) {
  calls[0].emit('sample',{jitter:i,rtt:Infinity,mos:NaN,packetsLostFraction:150,codecName:'pcmu'});
  calls[0].emit('warning','low-mos');
}
assert.equal(audio.quality.samples.length,15);assert.equal(audio.quality.warnings.length,16);
const timer=[...timers.values()].find(value=>value.ms===10000);assert.ok(timer);timer.callback();await tick();
assert.equal(reports.length,1);assert.equal(reports[0].samples.length,15);
assert.equal(reports[0].warnings.length,16);assert.equal(reports[0].codec,'pcmu');
assert.equal(reports[0].samples[0].rtt,undefined);assert.equal(reports[0].samples[0].mos,undefined);
assert.equal(reports[0].samples[0].packetsLostFraction,undefined);
assert.equal(audio.connected,true);assert.equal(audio.stopped,false);
audio.sendDigits('5');assert.deepEqual(calls[0].digits,['5']);
audio.stop();await tick();assert.equal(reports.length,2);assert.equal(reports[1].final,true);
""")


def test_sdk_final_quality_flush_waits_for_inflight_batch_and_navigation_flush_is_idempotent():
    run_sdk(r"""
const reports=[],pageEvents={};let finish;
window.addEventListener=(name,handler)=>pageEvents[name]=handler;
window.removeEventListener=(name,handler)=>{if(pageEvents[name]===handler)delete pageEvents[name];};
window.fetch=(url,options)=>{reports.push(JSON.parse(options.body));
  return reports.length===1?new Promise(resolve=>finish=resolve):Promise.resolve({ok:true});};
const audio=await prepareSdk();await sdkAttach(audio);
calls[0].emit('sample',{jitter:12,mos:4.2});audio.quality.flush();await tick();
assert.equal(reports.length,1);assert.equal(reports[0].final,false);
calls[0].emit('sample',{jitter:40,mos:2.9});pageEvents.pagehide();audio.stop();
assert.equal(reports.length,1,'In-flight reports do not spawn parallel fetches');
finish({ok:true});await tick();
assert.equal(reports.length,2);assert.equal(reports[1].final,true);
assert.equal(reports[1].sequence,2);assert.equal(reports[1].samples[0].jitter,40);
assert.equal(pageEvents.pagehide,undefined);assert.equal(timers.size,0);
audio.stop();await tick();assert.equal(reports.length,2);
""")
