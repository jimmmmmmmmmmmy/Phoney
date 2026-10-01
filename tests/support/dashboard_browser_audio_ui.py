"""Shared fixtures and fakes for focused integration checks."""

from pathlib import Path


import shutil


import subprocess


import pytest


ROOT = Path(__file__).resolve().parents[2]


SCRIPT = ROOT / "public/dashboard-browser-audio.js"


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
