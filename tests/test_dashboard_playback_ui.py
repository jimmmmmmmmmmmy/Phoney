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
        self.details = {}

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == "audio":
            self.players.append(attributes)
        if "id" in attributes:
            self.ids.append(attributes["id"])
        if tag == "details":
            self.details[attributes["id"]] = attributes


def test_one_native_player_is_accessible_and_never_autoplays_or_preloads_audio():
    parsed = AudioMarkup()
    parsed.feed(HTML.read_text())
    assert len(parsed.players) == 1
    player = parsed.players[0]
    assert player["id"] == "call-audio" and player["preload"] == "none"
    assert "controls" in player and "autoplay" not in player and "src" not in player
    assert player["aria-label"] and player["aria-describedby"] == "audio-status"
    assert len(parsed.ids) == len(set(parsed.ids))
    assert {"audio-track", "audio-download", "audio-status", "recording-warning"} <= set(parsed.ids)
    assert "open" in parsed.details["recent-calls"]
    assert "open" not in parsed.details["voicemail-section"]


HARNESS = r'''
const assert = require('node:assert/strict');
const elements = new Map(), handlers = new Map();
class Element {
 constructor(tag='div') {Object.assign(this,{tag,dataset:{},attributes:{},children:[],textContent:'',hidden:false,
  checked:true,open:false,scrollTop:0,scrollHeight:100,events:{},paused:true,ended:false,currentTime:0,duration:120,loads:0,pauses:0,plays:0,rect:{top:10,bottom:50}});
  const classes=new Set();this.classList={toggle(name,on){if(on)classes.add(name);else classes.delete(name);},contains(name){return classes.has(name);}};}
 setAttribute(name,value) {this.attributes[name]=value;}
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
assert.equal($('connection').attributes['aria-label'],'Connected');assert.equal($('call-list').children.length,2);
const audio=$('call-audio');audio.play();audio.currentTime=17;
const loads=audio.loads,pauses=audio.pauses;
await poll();
assert.equal(requests[1].url,`/api/transcripts?call_sid=${SID}`);
assert.equal(audio.loads,loads);assert.equal(audio.pauses,pauses);assert.equal(audio.currentTime,17);
assert.equal($('connection').attributes['aria-label'],'Connected');
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


def test_polling_preserves_playback_until_call_or_direction_changes(tmp_path):
    run_browser_logic(tmp_path, r'''
state.snapshot=snapshot([session()],[recording()]);render();
const audio=$('call-audio');
assert.equal(audio.src,`/api/recordings/${SID}/audio?track=combined`);
assert.equal(audio.loads,1);assert.equal(audio.pauses,1);assert.equal(audio.plays,0);
audio.play();audio.currentTime=17;
assert.equal(state.playbackPinned,SID);
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
assert.equal(state.selected,SID);assert.equal($('call-list').children.length,1);
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


def test_line_highlights_follow_overlaps_seek_direction_and_each_track_duration(tmp_path):
    run_browser_logic(tmp_path, r'''
const call=session();call.tracks={inbound:{interim:'Unfinished text'}};
call.segments=[
 {id:'input',track:'inbound',start_ms:0,end_ms:3000,text:'caller'},
 {id:'playback',track:'outbound',start_ms:1000,end_ms:4000,text:'overlap'},
 {id:'zero',track:'inbound',start_ms:2000,end_ms:2000,text:'zero length'},
 {id:'later',track:'inbound',start_ms:4000,end_ms:6000,text:'later'}];
const saved=recording();saved.tracks.inbound.duration_seconds=5;saved.tracks.outbound.duration_seconds=2.5;
state.snapshot=snapshot([call],[saved]);render();
const audio=$('call-audio');
const highlighted=()=>state.transcriptRows.filter(item=>item.row.classList.contains('playing-line')).map(item=>item.row.dataset.segmentId);
assert.deepEqual(highlighted(),[]); // Merely loading a recording must not start highlighting.
audio.play();audio.currentTime=1.5;audio.events.timeupdate();assert.deepEqual(highlighted(),['input','playback']);
assert.ok(!$('messages').children.at(-1).classList.contains('playing-line')); // Interim text has no timing.
audio.currentTime=2;audio.events.seeking();assert.deepEqual(highlighted(),['input','playback']);
audio.currentTime=2.5;audio.events.seeked();assert.deepEqual(highlighted(),['input']); // The shorter track ends here.
audio.currentTime=3;audio.events.timeupdate();assert.deepEqual(highlighted(),[]); // End time is exclusive.
audio.currentTime=4.5;audio.events.timeupdate();assert.deepEqual(highlighted(),['later']);
audio.currentTime=5;audio.events.timeupdate();assert.deepEqual(highlighted(),[]); // No highlight beyond the saved audio.
$('audio-track').value='outbound';$('audio-track').events.change();assert.deepEqual(highlighted(),[]);
audio.currentTime=1.5;audio.events.seeking();assert.deepEqual(highlighted(),['playback']);
audio.paused=true;audio.events.pause();assert.deepEqual(highlighted(),['playback']);
$('audio-track').value='combined';$('audio-track').events.change();assert.deepEqual(highlighted(),[]);
audio.play();audio.currentTime=1.5;audio.events.timeupdate();assert.deepEqual(highlighted(),['input','playback']);
audio.ended=true;audio.events.ended();assert.deepEqual(highlighted(),[]);render();assert.deepEqual(highlighted(),[]);
audio.ended=false;audio.currentTime=1.5;audio.events.seeked();assert.deepEqual(highlighted(),['input','playback']);
''')


def test_highlights_survive_polled_text_updates_and_clear_on_another_call(tmp_path):
    run_browser_logic(tmp_path, r'''
(async()=>{
const call=session();call.segments=[{id:'one',track:'inbound',start_ms:0,end_ms:3000,text:'first wording'}];
state.snapshot=snapshot([call,session(OTHER)],[recording(),recording(OTHER)]);render();
const audio=$('call-audio');audio.play();audio.currentTime=1.5;audio.events.timeupdate();
const original=state.transcriptRows[0].row,loads=audio.loads,pauses=audio.pauses;
fetch=async()=>({ok:true,json:async()=>snapshot([{...call,segments:[{...call.segments[0],text:'corrected wording'}]},session(OTHER)],[recording(),recording(OTHER)])});
await poll();
assert.equal(audio.loads,loads);assert.equal(audio.pauses,pauses);assert.equal(audio.currentTime,1.5);
assert.notEqual(state.transcriptRows[0].row,original);assert.equal(state.transcriptRows[0].row.classList.contains('playing-line'),true);
const previousRows=state.transcriptRows.map(item=>item.row);
state.paused=true;chooseCall(OTHER);
assert.equal(state.selected,OTHER);assert.equal(audio.currentTime,0);assert.equal(state.highlightEnabled,false);
assert.ok(previousRows.every(row=>!row.classList.contains('playing-line')));
assert.ok(state.transcriptRows.every(item=>!item.row.classList.contains('playing-line')));
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


def test_live_text_and_playback_follow_automatically_without_a_toggle(tmp_path):
    run_browser_logic(tmp_path, r'''
const call=session();call.segments[0].end_ms=3000;
state.snapshot=snapshot([call],[recording()]);render();
const audio=$('call-audio'),viewport=$('transcript'),row=state.transcriptRows[0].row;
viewport.rect={top:100,bottom:300};row.rect={top:150,bottom:210};viewport.scrollTop=40;
audio.play();audio.currentTime=1.5;audio.events.timeupdate();assert.equal(viewport.scrollTop,40);
row.rect={top:350,bottom:410};audio.events.timeupdate();assert.equal(viewport.scrollTop,158);
row.rect={top:150,bottom:210};audio.events.timeupdate();assert.equal(viewport.scrollTop,158);
audio.ended=true;audio.events.ended();
call.segments.push({id:'new',track:'outbound',start_ms:4000,end_ms:6000,text:'new text'});
viewport.scrollHeight=250;render();assert.equal(viewport.scrollTop,250);
''')


def test_live_updates_keep_paused_history_and_respect_another_manual_selection(tmp_path):
    run_browser_logic(tmp_path, r'''
const THIRD='CA'+'c'.repeat(32),FOURTH='CA'+'d'.repeat(32);
state.snapshot=snapshot([session(),session(THIRD)],[recording(),recording(THIRD)]);render();
const audio=$('call-audio');audio.play();audio.currentTime=17;audio.paused=true;audio.events.pause();
state.snapshot.sessions.unshift({...session(OTHER),started_at:'2026-09-26T13:00:00Z',ended_at:null,status:'live'});
render();assert.equal(state.selected,SID);assert.equal(state.pendingLive,OTHER);assert.equal(audio.currentTime,17);
state.paused=true;chooseCall(THIRD); // Explicit manual selection wins over the queued live call.
assert.equal(state.selected,THIRD);assert.equal(state.pendingLive,null);render();assert.equal(state.selected,THIRD);
audio.play();audio.paused=true;audio.events.pause();
state.snapshot.sessions.unshift({...session(FOURTH),started_at:'2026-09-26T14:00:00Z',ended_at:null,status:'live'});
render();assert.equal(state.selected,THIRD);assert.equal(state.pendingLive,FOURTH);
audio.ended=true;audio.events.ended();render();assert.equal(state.selected,FOURTH);
''')


def test_collapsible_sections_preserve_manual_choices_across_updates(tmp_path):
    run_browser_logic(tmp_path, r'''
state.snapshot=snapshot([session()],[recording()]);$('recent-calls').open=true;render();
assert.equal($('voicemail-section').open,false);
$('recent-calls').open=false;$('voicemail-section').open=true;$('voicemail-section').events.toggle();
render();assert.equal($('recent-calls').open,false);assert.equal($('voicemail-section').open,true);
$('voicemail-section').open=false;$('voicemail-section').events.toggle();
state.snapshot.voicemail.voicemails=[{call_sid:SID,recording_status:'processing',started_at:'2026-09-26T12:00:00Z'}];
render();assert.equal($('voicemail-section').open,false);assert.equal($('voicemail-list').children.length,1);
const visible=node=>node.textContent+' '+node.children.map(visible).join(' ');
assert.ok(!visible($('call-list')).includes(SID.slice(-12)));
assert.ok(!visible($('voicemail-list')).includes(SID.slice(-12)));
assert.ok(!$('session-detail').textContent.includes(SID));
''')


def test_caller_metadata_and_saved_summary_render_safely_without_restarting_playback(tmp_path):
    run_browser_logic(tmp_path, r'''
const saved=recording(),call=session();call.segments[0].end_ms=3000;
state.snapshot=snapshot([call,session(OTHER)],[saved,recording(OTHER)]);
const details={call_sid:SID,caller_number:'+14155550111',started_at:'2026-09-26T13:00:00Z',ended_at:'2026-09-26T13:03:00Z',
 duration_seconds:180,summary:{text:'<img src=x onerror=alert(1)> A saved agent summary.',source:'agent',created_at:'2026-09-26T13:04:00Z'}};
state.snapshot.call_details={enabled:true,storage_error:'',calls:[details]};render();
const button=$('call-list').children.find(row=>row.dataset.callSid===SID);
assert.equal(button.children[0].textContent,'+14155550111');
assert.equal(button.children[1].textContent,clockTime(details.started_at)+' · 03:00');
assert.equal($('call-summary').textContent,details.summary.text);assert.equal($('call-summary').children.length,0);
assert.equal($('call-summary').dataset.empty,'false');
const audio=$('call-audio');audio.play();audio.currentTime=1.5;audio.events.timeupdate();
const loads=audio.loads,pauses=audio.pauses;
details.summary.text='An updated saved summary.';details.duration_seconds=181;render();
assert.equal($('call-summary').textContent,'An updated saved summary.');
assert.equal(audio.loads,loads);assert.equal(audio.pauses,pauses);assert.equal(audio.currentTime,1.5);
assert.equal(state.transcriptRows[0].row.classList.contains('playing-line'),true);
state.paused=true;chooseCall(OTHER);
assert.equal($('call-summary').textContent,'No summary yet.');assert.equal($('call-summary').dataset.empty,'true');
''')


def test_call_list_uses_honest_fallbacks_when_metadata_is_missing(tmp_path):
    run_browser_logic(tmp_path, r'''
const call=session(),saved=recording();state.snapshot=snapshot([call],[saved]);render();
let button=$('call-list').children[0];
assert.equal(button.children[0].textContent,'Unknown caller');
assert.equal(button.children[1].textContent,clockTime(call.started_at)+' · 02:00');
assert.equal($('call-summary').textContent,'No summary yet.');
saved.duration_seconds=undefined;call.ended_at='2026-09-26T12:01:00Z';render();
assert.equal($('call-list').children[0].children[1].textContent,clockTime(call.started_at)+' · 01:00');
call.ended_at=null;render();assert.ok($('call-list').children[0].children[1].textContent.endsWith('Duration unavailable'));
state.snapshot.call_details={enabled:true,storage_error:'',calls:[{call_sid:SID,caller_number:'',duration_seconds:0,summary:null}]};render();
assert.equal($('call-list').children[0].children[0].textContent,'Unknown caller');
assert.ok($('call-list').children[0].children[1].textContent.endsWith('00:00')); // Zero is known, not missing.
''')


def test_metadata_only_calls_keep_their_saved_summary_selectable(tmp_path):
    run_browser_logic(tmp_path, r'''
state.snapshot=snapshot();state.snapshot.enabled=false;
state.snapshot.call_details={enabled:true,storage_error:'',calls:[{call_sid:SID,caller_number:'+14155550222',
 started_at:'2026-09-26T12:00:00Z',ended_at:'2026-09-26T12:01:00Z',duration_seconds:60,
 summary:{text:'Saved summary without live transcription.',source:'agent',created_at:'2026-09-26T12:02:00Z'}}]};render();
assert.equal(state.selected,SID);assert.equal($('call-list').children.length,1);
assert.equal($('call-list').children[0].children[0].textContent,'+14155550222');
assert.equal($('call-summary').textContent,'Saved summary without live transcription.');
assert.equal($('export-json').attributes['aria-disabled'],'true');assert.equal($('call-audio').hidden,true);
assert.equal(trackNames.outbound,'New College');assert.equal(audioTracks.outbound,'New College');
''')
