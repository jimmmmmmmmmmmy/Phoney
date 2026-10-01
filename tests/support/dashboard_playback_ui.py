"""Shared fixtures and fakes for focused integration checks."""

from html.parser import HTMLParser


import json


from pathlib import Path


import re


import shutil


import subprocess


import pytest


HTML = Path(__file__).resolve().parents[2] / "dashboard.html"


class AudioMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.players = []
        self.ids = []
        self.details = {}
        self.attributes = {}
        self.tags = {}

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == "audio":
            self.players.append(attributes)
        if "id" in attributes:
            self.ids.append(attributes["id"])
            self.attributes[attributes["id"]] = attributes
            self.tags[attributes["id"]] = tag
        if tag == "details":
            self.details[attributes["id"]] = attributes


HARNESS = r'''
const assert = require('node:assert/strict');
const elements = new Map(), handlers = new Map(), documentHandlers = new Map();
class Element {
 constructor(tag='div') {Object.assign(this,{tag,dataset:{},attributes:{},children:[],textContent:'',hidden:false,
  checked:true,open:false,scrollTop:0,scrollHeight:100,clientHeight:100,events:{},paused:true,ended:false,currentTime:0,duration:120,loads:0,pauses:0,plays:0,rect:{top:10,bottom:50}});
  const classes=new Set();this.classList={toggle(name,on){if(on)classes.add(name);else classes.delete(name);},contains(name){return classes.has(name);}};}
 setAttribute(name,value) {this.attributes[name]=value;}
 getAttribute(name) {return this.attributes[name]??null;}
 removeAttribute(name) {delete this.attributes[name];if(name==='href')delete this.href;}
 get src() {return this.attributes.src || '';}
 getBoundingClientRect() {return this.rect;}
 append(...children) {this.children.push(...children);}
 replaceChildren(...children) {this.children=children.flatMap(child=>child.tag==='fragment'?child.children:[child]);}
 addEventListener(name,fn) {this.events[name]=fn;}
 focus() {document.activeElement=this;}
 pause() {this.pauses++;this.paused=true;}
 load() {this.loads++;this.currentTime=0;this.paused=true;this.ended=false;}
 play() {this.plays++;this.paused=false;this.events.play?.();}
}
const document = {activeElement:null,getElementById(id) {assert.ok(markupIds.has(id),'Markup is missing #'+id);if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},
 createElement(tag) {return new Element(tag);},createDocumentFragment() {return new Element('fragment');},
 addEventListener(name,fn) {documentHandlers.set(name,fn);}};
const historyEntries=[];
const location={hash:'',pathname:'/dashboard',search:''};
const history={state:null,pushState(state,unused,url){this.state=state;location.hash=url.includes('#')?'#'+url.split('#')[1]:'';historyEntries.push({state,url});},
 replaceState(state,unused,url){this.state=state;location.hash=url.includes('#')?'#'+url.split('#')[1]:'';historyEntries[historyEntries.length-1]={state,url};}};
const window = {location,history,addEventListener(name,fn) {handlers.set(name,fn);}};
function setTimeout() {return 1;}
function clearTimeout() {}
function fetch() {throw new Error('UI test must never make a network request');}
const SID='CA'+'a'.repeat(32), OTHER='CA'+'b'.repeat(32);
function recording(sid=SID) {return {call_sid:sid,started_at:'2026-09-26T12:00:00Z',finished_at:'2026-09-26T12:02:00Z',
 status:'completed',duration_seconds:120,url:`/api/recordings/${sid}/audio?track=combined`,
 tracks:{inbound:{duration_seconds:120,url:`/api/recordings/${sid}/audio?track=inbound`},
 outbound:{duration_seconds:118,url:`/api/recordings/${sid}/audio?track=outbound`}}};}
function session(sid=SID) {return {call_sid:sid,started_at:'2026-09-26T12:00:00Z',ended_at:'2026-09-26T12:02:00Z',
 status:'completed',tracks:{},segments:[{id:'one',track:'inbound',start_ms:0,end_ms:1000,text:'hello',confidence:.9}]};}
function snapshot(sessions=[],records=[]) {return {enabled:true,provider:'deepgram',model:'nova-3',sessions,
 selected_call_sid:sessions[0]?.call_sid || null,voicemail:{enabled:true,storage_error:'',voicemails:[]},
 recordings:{enabled:true,storage_error:'',recordings:records}};}
function openCall(sid=SID) {const paused=state.paused;state.paused=true;chooseCall(sid);state.paused=paused;}
'''


def run_browser_logic(tmp_path, assertions):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for the DOM/audio regression check")
    html = HTML.read_text()
    parsed = AudioMarkup()
    parsed.feed(html)
    script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    assert script.rstrip().endswith("poll();")
    # Keep the actual UI code and event handlers; suppress only its initial network poll.
    script = script.rsplit("    poll();", 1)[0]
    path = tmp_path / "playback-check.js"
    path.write_text("const markupIds = new Set(" + json.dumps(parsed.ids) + ");\n" + HARNESS + script + "\n" + assertions)
    result = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
