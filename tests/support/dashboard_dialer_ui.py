"""Shared fixtures and fakes for focused integration checks."""

from pathlib import Path


import shutil


import subprocess


import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "public/dashboard-dialer.js"


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
