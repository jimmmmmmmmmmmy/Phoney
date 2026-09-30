"""Browser call transport, playback, and device cleanup with no network or microphone."""

from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "public/dashboard-browser-audio.js"
WORKLET = ROOT / "public/dashboard-call-worklet.js"
HARNESS = r"""
const assert = require('node:assert/strict');
const window = {isSecureContext:true,location:{href:'https://phoney.example/',origin:'https://phoney.example'}};
const events = [], contexts = [], captures = [], sockets = [], played = [];
const track = {stopped:false,readyState:'live',listeners:{},stop(){this.stopped=true;events.push('track-stop');},
  addEventListener(name,listener){this.listeners[name]=listener;},
  removeEventListener(name,listener){if(this.listeners[name]===listener)delete this.listeners[name];},
  dispatch(name){this.listeners[name]?.();}};
const stream = {getTracks:()=>[track]};
let denied = false;
const navigator = {mediaDevices:{async getUserMedia(options) {
  events.push('microphone');
  assert.equal(options.video,false);
  assert.equal(options.audio.echoCancellation,true);
  if(denied)throw Object.assign(Error('Permission denied'),{name:'NotAllowedError'});
  return stream;
}}};
class AudioContext {
  constructor() {this.currentTime=0;this.destination={};this.state='running';this.listeners={};contexts.push(this);
    this.audioWorklet={addModule:async path=>{assert.equal(path,'/assets/dashboard-call-worklet.js');events.push('worklet');}};}
  async resume() {events.push('resume');}
  async close() {this.closed=true;events.push('context-close');}
  addEventListener(name,listener){this.listeners[name]=listener;}
  removeEventListener(name,listener){if(this.listeners[name]===listener)delete this.listeners[name];}
  dispatch(name){this.listeners[name]?.();}
  createMediaStreamSource() {return {connect(){},disconnect(){this.disconnected=true;}};}
  createGain() {return {gain:{value:1},connect(){},disconnect(){}};}
  createBuffer(channels,length,sampleRate) {
    const data=new Float32Array(length);
    return {duration:length/sampleRate,length,sampleRate,getChannelData:()=>data};
  }
  createBufferSource() {
    const value={connect(){},disconnect(){},start(time){this.startTime=time;played.push(this);},stop(){this.stopped=true;}};
    return value;
  }
}
window.AudioContext=AudioContext;
class AudioWorkletNode {
  constructor(context,name) {assert.equal(name,'dashboard-call-capture');this.port={};captures.push(this);}
  connect(){}
  disconnect(){this.disconnected=true;}
}
class WebSocket {
  constructor(url){this.url=url;this.readyState=0;this.bufferedAmount=0;this.sent=[];sockets.push(this);}
  send(value){this.sent.push(JSON.parse(value));}
  close(){this.readyState=3;this.closed=true;}
  open(){this.readyState=1;this.onopen();}
  receive(value){this.onmessage({data:JSON.stringify(value)});}
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const sid='a'.repeat(32);
const credentials={url:`/browser-media/${sid}/`,token:'private-once-token'};
const attach=async audio=>{
  const pending=audio.attach(credentials,sid);await tick();
  const socket=sockets.at(-1);socket.open();socket.receive({event:'ready'});await pending;
  return socket;
};
"""


def run_audio(checks, *, before=""):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for browser audio tests")
    result = subprocess.run(
        [node, "-e", HARNESS + before + "\n" + SCRIPT.read_text()
         + "\n(async()=>{\n" + checks
         + "\n})().catch(error=>{console.error(error);process.exitCode=1;});"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_browser_handshake_audio_frames_playback_marks_and_cleanup():
    run_audio(r"""
const audio=await window.DashboardBrowserAudio.prepare();
assert.deepEqual(events,['resume','microphone','worklet']);
captures[0].port.onmessage({data:new Uint8Array(160).fill(255).buffer});
assert.equal(sockets.length,0,'Preparing never opens a socket or sends audio');
const socket=await attach(audio);
assert.equal(socket.url,`wss://phoney.example/browser-media/${sid}/`);
assert.equal(socket.url.includes(credentials.token),false,'Tokens must not enter URL logs');
assert.deepEqual(socket.sent,[{event:'start',token:credentials.token}]);
captures[0].port.onmessage({data:new Uint8Array(160).fill(255).buffer});
captures[0].port.onmessage({data:new Uint8Array(160).fill(255).buffer});
assert.equal(socket.sent[1].media.track,'inbound');
assert.equal(socket.sent[1].media.timestamp,'0');
assert.equal(socket.sent[2].media.timestamp,'20');
assert.equal(atob(socket.sent[1].media.payload).length,160);
socket.receive({event:'media',media:{payload:btoa(String.fromCharCode(...new Uint8Array(160).fill(255)))}});
assert.equal(played[0].buffer.sampleRate,8000);
assert.equal(played[0].buffer.length,160);
assert.ok(played[0].buffer.getChannelData(0).every(value=>value===0));
socket.receive({event:'mark',mark:{name:'speech-1'}});
assert.equal(socket.sent.filter(value=>value.event==='mark').length,0,'Mark must wait for actual playback');
contexts[0].currentTime=.04;played[0].onended();
assert.deepEqual(socket.sent.at(-1),{event:'mark',mark:{name:'speech-1'}});
socket.receive({event:'media',media:{payload:btoa('\xff'.repeat(160))}});
socket.receive({event:'mark',mark:{name:'speech-cleared'}});
socket.receive({event:'clear'});
assert.equal(played[1].stopped,true);
assert.deepEqual(socket.sent.at(-1),{event:'mark',mark:{name:'speech-cleared'}});
audio.stop();audio.stop();
assert.equal(track.stopped,true);assert.equal(contexts[0].closed,true);assert.equal(socket.closed,true);
assert.equal(captures[0].port.onmessage,null);
assert.equal(events.filter(value=>value==='track-stop').length,1,'Cleanup must be idempotent');
""")


def test_permission_denial_closes_context_without_opening_a_socket():
    run_audio(r"""
await assert.rejects(window.DashboardBrowserAudio.prepare(),/Allow microphone access/);
assert.equal(contexts[0].closed,true);
assert.equal(captures.length,0);assert.equal(sockets.length,0);
""", before="denied=true;\n")


@pytest.mark.parametrize("failure", ["microphone", "context"])
def test_device_interruption_during_worklet_loading_never_returns_call_ready_audio(failure):
    interrupt = "track.readyState='ended';" if failure == "microphone" else "this.state='suspended';"
    run_audio(r"""
window.AudioContext=class extends AudioContext {
  constructor(){super();this.audioWorklet.addModule=async()=>{
""" + interrupt + r"""
  };}
};
await assert.rejects(window.DashboardBrowserAudio.prepare(),/Browser audio could not start/);
assert.equal(track.stopped,true);assert.equal(contexts[0].closed,true);
assert.equal(sockets.length,0);
""")


def test_insecure_browser_never_requests_the_microphone():
    run_audio(r"""
await assert.rejects(window.DashboardBrowserAudio.prepare(),/needs HTTPS/);
assert.deepEqual(events,[]);
""", before="window.isSecureContext=false;\n")


def test_invalid_socket_destination_or_malformed_handshake_never_connects():
    run_audio(r"""
const audio=await window.DashboardBrowserAudio.prepare();
await assert.rejects(audio.attach({...credentials,url:'https://other.example'+credentials.url},sid),/Invalid browser audio/);
await assert.rejects(audio.attach({...credentials,url:`/browser-media/${'b'.repeat(32)}/`},sid),/Invalid browser audio/);
assert.equal(sockets.length,0);
const pending=audio.attach(credentials,sid);await tick();sockets[0].open();
sockets[0].onmessage({data:'bad json'});
await assert.rejects(pending,/invalid call audio/);
assert.equal(track.stopped,true);assert.equal(contexts[0].closed,true);assert.equal(sockets[0].closed,true);
""")


def test_speaker_and_transport_backlogs_are_bounded_and_disconnect_closes_devices():
    run_audio(r"""
const audio=await window.DashboardBrowserAudio.prepare(),socket=await attach(audio);
let disconnected='';audio.onDisconnect=message=>disconnected=message;
const packet={event:'media',media:{payload:btoa('\xff'.repeat(160))}};
for(let index=0;index<100;index++)socket.receive(packet);
assert.ok(audio.playUntil-contexts[0].currentTime<=1.01,'Playback must never accumulate many seconds of stale audio');
assert.ok(played.some(source=>source.stopped),'Overflow must drop old scheduled audio');
socket.bufferedAmount=64001;
captures[0].port.onmessage({data:new Uint8Array(160).buffer});
assert.match(disconnected,/connection stalled/);
assert.equal(socket.closed,true);assert.equal(track.stopped,true);assert.equal(contexts[0].closed,true);
""")


@pytest.mark.parametrize("failure", ["microphone", "context"])
def test_revoked_microphone_or_suspended_audio_context_ends_browser_transport(failure):
    trigger = "track.dispatch('ended');" if failure == "microphone" else "contexts[0].state='suspended';contexts[0].dispatch('statechange');"
    run_audio(r"""
const audio=await window.DashboardBrowserAudio.prepare(),socket=await attach(audio);
let disconnected='';audio.onDisconnect=message=>disconnected=message;
""" + trigger + r"""
assert.match(disconnected,/call is ending/);
assert.equal(socket.closed,true);assert.equal(track.stopped,true);assert.equal(contexts[0].closed,true);
assert.equal(track.listeners.ended,undefined);assert.equal(contexts[0].listeners.statechange,undefined);
""")


def test_stopping_during_handshake_settles_attachment_without_waiting_for_timeout():
    run_audio(r"""
const audio=await window.DashboardBrowserAudio.prepare();
const pending=audio.attach(credentials,sid);await tick();
audio.stop();
await assert.rejects(pending,/calling was cancelled/);
assert.equal(audio.cancelAttach,null);
assert.equal(track.stopped,true);assert.equal(contexts[0].closed,true);assert.equal(sockets[0].closed,true);
""")


@pytest.mark.parametrize("sample_rate", [44100, 48000])
def test_worklet_resamples_actual_device_rate_into_exact_twenty_ms_ulaw_frames(sample_rate):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for browser audio tests")
    prefix = f"""
const assert = require('node:assert/strict');
const sampleRate={sample_rate};
let Processor;
class AudioWorkletProcessor {{constructor(){{this.messages=[];this.port={{postMessage:value=>this.messages.push(new Uint8Array(value))}};}}}}
function registerProcessor(name,value){{assert.equal(name,'dashboard-call-capture');Processor=value;}}
"""
    result = subprocess.run(
        [node, "-e", prefix + WORKLET.read_text() + r"""
const processor=new Processor();
const samples=new Float32Array(sampleRate).fill(0);
for(let offset=0;offset<samples.length;offset+=128)processor.process([[samples.slice(offset,offset+128)]]);
assert.equal(processor.messages.length,50,'One second must produce 50 twenty-ms frames');
assert.ok(processor.messages.every(frame=>frame.length===160));
assert.ok(processor.messages.every(frame=>frame.every(value=>value===255)),'PCM silence must encode as standard μ-law silence');
assert.equal(processor.encode(1),128);
assert.equal(processor.encode(-1),0);
assert.equal(processor.process([]),true);
"""], capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
