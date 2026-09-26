"""Browser-side recording controls, using a DOM/audio fake with no network or playback."""

from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess

import pytest


HTML = Path(__file__).resolve().parents[1] / "dashboard.html"


class AudioMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.players = []
        self.ids = []

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == "audio":
            self.players.append(attributes)
        if "id" in attributes:
            self.ids.append(attributes["id"])


def test_one_native_player_is_accessible_and_never_autoplays_or_preloads_audio():
    parsed = AudioMarkup()
    parsed.feed(HTML.read_text())
    assert len(parsed.players) == 1
    player = parsed.players[0]
    assert player["id"] == "call-audio" and player["preload"] == "none"
    assert "controls" in player and "autoplay" not in player and "src" not in player
    assert player["aria-label"] and player["aria-describedby"] == "audio-status audio-direction-note"
    assert len(parsed.ids) == len(set(parsed.ids))
    assert {"audio-track", "audio-download", "audio-status", "recording-warning"} <= set(parsed.ids)


HARNESS = r'''
const assert = require('node:assert/strict');
const elements = new Map(), handlers = new Map();
class Element {
 constructor(tag='div') {Object.assign(this,{tag,dataset:{},attributes:{},children:[],textContent:'',hidden:false,
  checked:true,scrollTop:0,scrollHeight:100,events:{},paused:true,ended:false,currentTime:0,loads:0,pauses:0,plays:0});}
 setAttribute(name,value) {this.attributes[name]=value;}
 removeAttribute(name) {delete this.attributes[name];if(name==='href')delete this.href;}
 get src() {return this.attributes.src || '';}
 append(...children) {this.children.push(...children);}
 replaceChildren(...children) {this.children=children.flatMap(child=>child.tag==='fragment'?child.children:[child]);}
 addEventListener(name,fn) {this.events[name]=fn;}
 focus() {document.activeElement=this;}
 pause() {this.pauses++;this.paused=true;}
 load() {this.loads++;this.currentTime=0;this.paused=true;}
 play() {this.plays++;this.paused=false;this.events.play?.();}
}
const document = {activeElement:null,getElementById(id) {if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},
 createElement(tag) {return new Element(tag);},createDocumentFragment() {return new Element('fragment');}};
const window = {addEventListener(name,fn) {handlers.set(name,fn);}};
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
'''


def run_browser_logic(tmp_path, assertions):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for the DOM/audio regression check")
    html = HTML.read_text()
    script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    assert script.rstrip().endswith("poll();")
    # Keep the actual UI code and event handlers; suppress only its initial network poll.
    script = script.rsplit("    poll();", 1)[0]
    path = tmp_path / "playback-check.js"
    path.write_text(HARNESS + script + "\n" + assertions)
    result = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_snapshot_fetch_bypasses_ngrok_html_warning_and_preserves_audio(tmp_path):
    run_browser_logic(tmp_path, r'''
(async()=>{
const requests=[];
fetch=async(url,options)=>{
 requests.push({url,options});
 // A browser request without ngrok's bypass receives an HTML page with HTTP 200.
 const bypass=options.headers?.['ngrok-skip-browser-warning']==='1';
 return {ok:true,json:async()=>{
  if(!bypass)throw new SyntaxError('Unexpected HTML browser warning');
  return snapshot([session(),session(OTHER)],[recording(),recording(OTHER)]);
 }};
};
await poll();
assert.equal(requests.length,1);assert.equal(requests[0].url,'/api/transcripts');
assert.equal(requests[0].options.credentials,'same-origin');
assert.equal(requests[0].options.headers.Accept,'application/json');
assert.equal(requests[0].options.headers['ngrok-skip-browser-warning'],'1');
assert.equal(requests[0].options.headers.Authorization,undefined);
assert.equal($('connection-text').textContent,'Connected');assert.equal($('session-count').textContent,'2');
const audio=$('call-audio');audio.play();audio.currentTime=17;
const loads=audio.loads,pauses=audio.pauses;
await poll();
assert.equal(requests[1].url,`/api/transcripts?call_sid=${SID}`);
assert.equal(audio.loads,loads);assert.equal(audio.pauses,pauses);assert.equal(audio.currentTime,17);
assert.equal($('connection-text').textContent,'Connected');
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


def test_polling_preserves_playback_until_call_or_direction_changes(tmp_path):
    run_browser_logic(tmp_path, r'''
state.snapshot=snapshot([session()],[recording()]);render();
const audio=$('call-audio');
assert.equal(audio.src,`/api/recordings/${SID}/audio?track=combined`);
assert.equal(audio.loads,1);assert.equal(audio.pauses,1);assert.equal(audio.plays,0);
audio.play();audio.currentTime=17;
assert.equal(state.followLive,false);
for(let i=0;i<5;i++){state.snapshot.sessions[0].segments[0].text='update '+i;render();}
assert.equal($('call-audio'),audio);assert.equal(audio.loads,1);assert.equal(audio.pauses,1);
assert.equal(audio.currentTime,17);assert.equal(audio.paused,false);assert.ok($('audio-status').textContent.startsWith('Playing'));
state.snapshot.sessions.unshift({...session(OTHER),started_at:'2026-09-26T13:00:00Z',ended_at:null,status:'live'});
state.snapshot.recordings.recordings.push(recording(OTHER));render();
assert.equal(state.selected,SID);assert.equal(audio.loads,1);
state.selected=OTHER;render();
assert.equal(audio.src,`/api/recordings/${OTHER}/audio?track=combined`);assert.equal(audio.loads,2);assert.equal(audio.paused,true);
$('audio-track').value='inbound';$('audio-track').events.change();
assert.equal(audio.src,`/api/recordings/${OTHER}/audio?track=inbound`);assert.equal(audio.loads,3);
render();assert.equal(audio.loads,3);assert.equal(audio.plays,1);
$('audio-track').value='__proto__';$('audio-track').events.change();assert.equal(audio.loads,3);
assert.equal($('audio-download').href,audio.src);assert.equal($('audio-download').download,`${OTHER}-inbound.wav`);
''')


def test_recordings_without_transcripts_are_selectable_and_urls_stay_local(tmp_path):
    run_browser_logic(tmp_path, r'''
const entry=recording();entry.url='https://attacker.invalid/collect';entry.tracks.inbound.url='javascript:alert(1)';
state.snapshot=snapshot([],[entry]);state.snapshot.enabled=false;render();
assert.equal(state.selected,SID);assert.equal($('session-count').textContent,'1');
assert.equal($('call-list').children.length,1);assert.equal($('sidebar-empty').hidden,true);
assert.equal($('transcript-title').textContent,'Call recording');assert.equal($('empty-title').textContent,'No transcript for this recording.');
assert.equal($('export-json').attributes['aria-disabled'],'true');assert.ok(!$('export-json').href);
assert.equal($('call-audio').src,`/api/recordings/${SID}/audio?track=combined`);
assert.equal($('audio-download').href,$('call-audio').src);
$('audio-track').value='inbound';$('audio-track').events.change();
assert.equal($('call-audio').src,`/api/recordings/${SID}/audio?track=inbound`);
state.snapshot.recordings.recordings=[{...entry,call_sid:'../secret'}];render();
assert.equal(state.selected,null);assert.equal($('call-audio').src,'');assert.equal($('call-audio').hidden,true);
assert.equal($('audio-download').attributes['aria-disabled'],'true');assert.ok(!$('audio-download').href);
''')


def test_finalization_missing_tracks_and_playback_errors_remain_clear(tmp_path):
    run_browser_logic(tmp_path, r'''
state.snapshot=snapshot([{...session(),ended_at:null,status:'live'}]);render();
assert.equal($('call-audio').hidden,true);assert.ok($('audio-status').textContent.includes('not finalized'));
const entry=recording();entry.status='partial';entry.url=null;entry.tracks.outbound.url=null;
state.snapshot.recordings.recordings=[entry];render();
assert.equal($('audio-track').value,'inbound');assert.equal($('audio-option-combined').disabled,true);
assert.equal($('audio-option-outbound').disabled,true);assert.equal($('audio-option-inbound').disabled,false);
assert.ok($('audio-status').textContent.includes('Partial recording'));
const loads=$('call-audio').loads;$('call-audio').events.error();render();
assert.ok($('audio-status').textContent.includes('could not be loaded'));assert.equal($('call-audio').loads,loads);
entry.tracks.inbound.url=null;entry.status='failed';render();
assert.equal($('call-audio').hidden,true);assert.ok($('audio-status').textContent.includes('could not be finalized'));
state.snapshot.recordings.storage_error='/private/path';render();
assert.equal($('recording-warning').hidden,false);assert.ok(!$('recording-warning').textContent.includes('/private/path'));
assert.equal(voicemailLabel({recording_status:'processing'}),'Unconfirmed recording');
assert.ok(voicemailDescriptions.processing.includes('has not been confirmed'));
''')
