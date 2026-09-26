"""Browser-side recording controls, using a DOM/audio fake with no network or playback."""

from html.parser import HTMLParser
import json
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


def test_one_native_player_is_accessible_and_never_autoplays_or_preloads_audio():
    parsed = AudioMarkup()
    parsed.feed(HTML.read_text())
    assert len(parsed.players) == 1
    player = parsed.players[0]
    assert player["id"] == "call-audio" and player["preload"] == "none"
    assert "controls" in player and "autoplay" not in player and "src" not in player
    assert player["aria-label"] and player["aria-describedby"] == "audio-status"
    assert len(parsed.ids) == len(set(parsed.ids))
    assert {"audio-panel", "audio-download", "audio-status", "recording-warning"} <= set(parsed.ids)
    assert not {"audio-track", "audio-option-combined", "audio-option-inbound", "audio-option-outbound",
                "session-badge", "session-detail", "audio-heading"} & set(parsed.ids)
    assert parsed.ids.index("transcript") < parsed.ids.index("transcript-title") < parsed.ids.index("summary-panel") < parsed.ids.index("messages")
    assert parsed.attributes["audio-download"]["aria-label"] == "Download WAV"
    for control in ("audio-close", "audio-reopen"):
        assert parsed.tags[control] == "button"
        assert parsed.attributes[control]["type"] == "button"
        assert parsed.attributes[control]["aria-controls"] == "audio-panel"
    assert parsed.attributes["audio-close"]["aria-label"]
    assert "hidden" in parsed.attributes["audio-reopen"]
    assert not {"recent-calls", "voicemail-section"} & set(parsed.ids)


def test_playback_dock_has_its_own_viewport_row_outside_scrolling_content():
    html = HTML.read_text()
    css = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    dock = re.search(r"\.audio-panel\s*\{([^}]+)\}", css).group(1)
    body = re.search(r"body\s*\{([^}]+)\}", css).group(1)
    assert "display:grid" in body
    assert "grid-template-columns:" in body
    assert "grid-template-rows:" in body and "minmax(0,1fr)" in body
    assert "height:100dvh" in body
    assert "position:fixed" not in dock
    assert "safe-area-inset-bottom" in dock
    assert html.index("</main>") < html.index('id="audio-panel"')
    # The player consumes its own row, so the main scroll area needs no
    # compensating bottom padding and cannot hide content behind the player.
    main_rules = re.findall(r"(?<![\w-])main\s*\{([^}]+)\}", css)
    assert all("safe-area-inset-bottom" not in rule and "126px" not in rule for rule in main_rules)
    assert re.search(r"@media\s*\(max-width:\s*\d+px\)\s*\{\s*\.audio-panel\s*\{"
                     r"[^}]*safe-area-inset-bottom", css)


def test_navigation_and_unimplemented_search_are_accessible_and_honest():
    parsed = AudioMarkup()
    parsed.feed(HTML.read_text())
    for page in ("calls", "contacts", "agents"):
        control = parsed.attributes[f"nav-{page}"]
        assert parsed.tags[f"nav-{page}"] == "button"
        assert control.get("type") == "button"
        assert control.get("aria-current") == ("page" if page == "calls" else None)
    assert "hidden" not in parsed.attributes["dashboard-view"]
    assert "hidden" in parsed.attributes["contacts-view"]
    assert "hidden" in parsed.attributes["agents-view"]
    search = parsed.attributes["global-search"]
    assert parsed.tags["global-search"] == "input"
    assert search["type"] == "search" and "disabled" in search
    assert search["aria-label"] == "Search (coming soon)" and search["title"]


def test_calls_categories_have_keyboard_accessible_tab_and_panel_relationships():
    parsed = AudioMarkup()
    parsed.feed(HTML.read_text())
    assert parsed.attributes["calls-tabs"]["role"] == "tablist"
    for collection in ("recent", "voicemail"):
        tab = parsed.attributes[f"tab-{collection}"]
        panel = parsed.attributes[f"{collection}-panel"]
        assert parsed.tags[f"tab-{collection}"] == "button" and tab["role"] == "tab"
        assert tab["aria-controls"] == f"{collection}-panel"
        assert panel["role"] == "tabpanel" and panel["aria-labelledby"] == f"tab-{collection}"
    assert parsed.attributes["tab-recent"]["aria-selected"] == "true"
    assert parsed.attributes["tab-voicemail"]["aria-selected"] == "false"
    assert "hidden" in parsed.attributes["calls-detail"]


HARNESS = r'''
const assert = require('node:assert/strict');
const elements = new Map(), handlers = new Map();
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
 createElement(tag) {return new Element(tag);},createDocumentFragment() {return new Element('fragment');}};
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


def test_navigation_preserves_call_selection_and_ongoing_playback_without_requests(tmp_path):
    run_browser_logic(tmp_path, r'''
assert.equal(state.page,'calls');
state.snapshot=snapshot([session(),session(OTHER)],[recording(),recording(OTHER)]);render();openCall();
const audio=$('call-audio');audio.play();audio.currentTime=17;
const selected=state.selected,src=audio.src,loads=audio.loads,pauses=audio.pauses,plays=audio.plays;
const viewIds={calls:'dashboard-view',contacts:'contacts-view',agents:'agents-view'};
for(const page of ['contacts','agents','calls']){
 $('nav-'+page).events.click();
 assert.equal(state.page,page);
 assert.equal($('page-title').textContent,page==='calls'?'Call details':page[0].toUpperCase()+page.slice(1));
 for(const name of Object.keys(viewIds)){
  assert.equal($(viewIds[name]).hidden,name!==page);
  assert.equal($('nav-'+name).attributes['aria-current'],name===page?'page':undefined);
 }
 assert.equal(state.selected,selected);
 assert.equal(audio.src,src);assert.equal(audio.currentTime,17);assert.equal(audio.paused,false);
 assert.equal(audio.loads,loads);assert.equal(audio.pauses,pauses);assert.equal(audio.plays,plays);
 assert.equal($('audio-panel').hidden,false);
}
// The harness rejects every fetch: changing pages cannot dial, transfer, or call an agent API.
''')


def test_internal_team_links_preserve_the_player_and_return_to_the_selected_call(tmp_path):
    run_browser_logic(tmp_path, r'''
state.snapshot=snapshot([session(),session(OTHER)],[recording(),recording(OTHER)]);render();openCall();
const audio=$('call-audio');audio.play();audio.currentTime=37;
const src=audio.src,loads=audio.loads,pauses=audio.pauses,plays=audio.plays;
for(const id of ['team-brand','team-brand-mobile','team-footer']){
 let prevented=false;
 $(id).events.click({preventDefault(){prevented=true;}});
 assert.equal(prevented,true);assert.equal(state.page,'team');assert.equal(location.hash,'#team');
 assert.equal($('team-view').hidden,false);assert.equal($('dashboard-view').hidden,true);
 assert.equal($('page-title').textContent,'New College');
 assert.equal($('call-audio'),audio);assert.equal(audio.src,src);assert.equal(audio.currentTime,37);
 assert.equal(audio.paused,false);assert.equal($('audio-panel').hidden,false);
 assert.equal(audio.loads,loads);assert.equal(audio.pauses,pauses);assert.equal(audio.plays,plays);
 $('nav-calls').events.click();
 assert.equal(state.page,'calls');assert.equal(state.detail,true);assert.equal(state.selected,SID);
 assert.equal($('team-view').hidden,true);assert.equal($('calls-detail').hidden,false);
 assert.equal($('call-audio'),audio);assert.equal(audio.src,src);assert.equal(audio.currentTime,37);
 assert.equal(audio.paused,false);assert.equal(audio.loads,loads);assert.equal(audio.pauses,pauses);
 assert.equal(audio.plays,plays);
}
''')


def test_opening_calls_starts_with_a_collection_and_no_selected_audio(tmp_path):
    run_browser_logic(tmp_path, r'''
assert.equal(state.detail,false);assert.equal(state.selected,null);
state.snapshot=snapshot([session(),session(OTHER)],[recording(),recording(OTHER)]);render();
assert.equal(state.collection,'recent');assert.equal(state.detail,false);assert.equal(state.selected,null);
assert.equal($('calls-detail').hidden,true);assert.equal($('recent-panel').hidden,false);
assert.equal($('audio-panel').hidden,true);assert.equal($('call-audio').src,'');
assert.equal($('call-audio').loads,0);assert.equal($('call-audio').plays,0);
openCall();
assert.equal(state.detail,true);assert.equal(state.selected,SID);assert.equal($('calls-detail').hidden,false);
assert.equal($('call-audio').src,`/api/recordings/${SID}/audio?track=combined`);
assert.equal($('call-audio').paused,true);assert.equal($('call-audio').plays,0);
''')


def test_closing_player_pauses_and_stays_closed_through_polling_and_navigation(tmp_path):
    run_browser_logic(tmp_path, r'''
(async()=>{
const call=session();call.segments[0].end_ms=3000;
state.snapshot=snapshot([call,session(OTHER)],[recording(),recording(OTHER)]);render();openCall();
const audio=$('call-audio');audio.play();audio.currentTime=1.5;audio.events.timeupdate();
assert.equal(state.transcriptRows[0].row.classList.contains('playing-line'),true);
const src=audio.src,loads=audio.loads,plays=audio.plays;
$('audio-close').events.click();
assert.equal($('audio-panel').hidden,true);assert.equal($('audio-reopen').hidden,false);
assert.equal(audio.paused,true);assert.equal(audio.currentTime,1.5);assert.equal(audio.src,src);
assert.equal(document.activeElement,$('audio-reopen'));assert.equal(state.selected,SID);
assert.ok(state.transcriptRows.every(item=>!item.row.classList.contains('playing-line')));
fetch=async()=>({ok:true,json:async()=>snapshot([{...call,segments:[{...call.segments[0],text:'updated transcript'}]},session(OTHER)],[recording(),recording(OTHER)])});
await poll();
for(const page of ['contacts','agents','team','calls']) {
 showPage(page);assert.equal($('audio-panel').hidden,true);assert.equal(audio.paused,true);
}
backToCalls();showCollection('voicemail');showCollection('recent');
state.paused=true;location.hash=`#calls/recent/${SID}`;handlers.get('popstate')();
assert.equal(state.selected,SID);assert.equal(state.detail,true);assert.equal($('audio-panel').hidden,true);
audio.events.seeked();audio.events.timeupdate();
assert.ok(state.transcriptRows.every(item=>!item.row.classList.contains('playing-line')));
assert.equal(audio.currentTime,1.5);assert.equal(audio.src,src);assert.equal(audio.loads,loads);assert.equal(audio.plays,plays);
$('audio-reopen').events.click();
assert.equal($('audio-panel').hidden,false);assert.equal($('audio-reopen').hidden,true);
assert.equal(audio.paused,true);assert.equal(audio.currentTime,1.5);assert.equal(audio.src,src);
assert.equal(audio.loads,loads);assert.equal(audio.plays,plays);assert.equal(document.activeElement,$('audio-close'));
assert.ok(state.transcriptRows.every(item=>!item.row.classList.contains('playing-line')));
audio.play();audio.events.timeupdate();
assert.equal(state.transcriptRows[0].row.classList.contains('playing-line'),true);
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


def test_explicit_call_selection_reopens_closed_player_without_autoplay(tmp_path):
    run_browser_logic(tmp_path, r'''
state.snapshot=snapshot([session(),session(OTHER)],[recording(),recording(OTHER)]);render();openCall();
const audio=$('call-audio');audio.play();audio.currentTime=17;
const src=audio.src,loads=audio.loads,plays=audio.plays;
showPage('team');$('audio-close').events.click();
assert.equal(document.activeElement,$('page-title'));assert.equal(state.page,'team');
// A delayed native play event cannot start audio after the panel was dismissed.
audio.paused=false;audio.events.play();assert.equal(audio.paused,true);
showPage('calls');backToCalls();state.paused=true;
$('call-list').children.find(row=>row.dataset.callSid===SID).events.click();
assert.equal($('audio-panel').hidden,false);assert.equal($('audio-reopen').hidden,true);
assert.equal(state.detail,true);assert.equal(state.selected,SID);
assert.equal(audio.paused,true);assert.equal(audio.currentTime,17);assert.equal(audio.src,src);
assert.equal(audio.loads,loads);assert.equal(audio.plays,plays);
$('audio-close').events.click();openCall(OTHER);
assert.equal($('audio-panel').hidden,false);assert.equal($('audio-reopen').hidden,true);
assert.equal(state.selected,OTHER);assert.equal(audio.currentTime,0);assert.equal(audio.paused,true);
assert.equal(audio.src,`/api/recordings/${OTHER}/audio?track=combined`);assert.equal(audio.plays,plays);
''')


def test_player_status_and_errors_can_be_closed_before_recording_is_ready(tmp_path):
    run_browser_logic(tmp_path, r'''
const call={...session(),ended_at:null,status:'live'};
state.snapshot=snapshot([call]);render();openCall();
assert.equal($('call-audio').hidden,true);assert.equal($('audio-status').hidden,false);
$('audio-close').events.click();
assert.equal($('audio-panel').hidden,true);assert.equal($('audio-reopen').hidden,false);
state.snapshot=snapshot([session()],[recording()]);render();
assert.equal($('audio-panel').hidden,true);assert.equal($('call-audio').paused,true);
$('audio-reopen').events.click();
$('call-audio').events.error();
assert.equal($('audio-status').hidden,false);assert.ok($('audio-status').textContent.includes('could not be loaded'));
$('audio-close').events.click();render();
assert.equal($('audio-panel').hidden,true);assert.equal($('call-audio').paused,true);
$('call-audio').events.pause();
$('audio-reopen').events.click();
assert.equal($('audio-panel').hidden,false);assert.equal($('call-audio').plays,0);
assert.equal($('audio-status').hidden,false);assert.ok($('audio-status').textContent.includes('could not be loaded'));
''')


def test_background_polling_keeps_placeholder_page_selected_and_audio_running(tmp_path):
    run_browser_logic(tmp_path, r'''
(async()=>{
state.snapshot=snapshot([session()],[recording()]);render();openCall();
const audio=$('call-audio');audio.play();audio.currentTime=17;
showPage('agents');
const requests=[],loads=audio.loads,pauses=audio.pauses;
fetch=async(url,options)=>{
 requests.push({url,options});
 return {ok:true,json:async()=>snapshot([session()],[recording()])};
};
await poll();
assert.equal(requests.length,1);assert.equal(requests[0].url,`/api/transcripts?call_sid=${SID}`);
assert.equal(state.page,'agents');assert.equal($('agents-view').hidden,false);
assert.equal($('dashboard-view').hidden,true);assert.equal($('contacts-view').hidden,true);
assert.equal($('nav-agents').attributes['aria-current'],'page');
assert.equal(state.selected,SID);assert.equal(audio.currentTime,17);assert.equal(audio.paused,false);
assert.equal(audio.loads,loads);assert.equal(audio.pauses,pauses);
showPage('calls');assert.equal($('call-list').children.length,1);
assert.equal($('transcript-title').textContent,'Call transcript');
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


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
assert.equal(state.selected,null);openCall();
const audio=$('call-audio');audio.play();audio.currentTime=17;
const loads=audio.loads,pauses=audio.pauses;
await poll();
assert.equal(requests[1].url,`/api/transcripts?call_sid=${SID}`);
assert.equal(audio.loads,loads);assert.equal(audio.pauses,pauses);assert.equal(audio.currentTime,17);
assert.equal($('connection').attributes['aria-label'],'Connected');
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


def test_polling_preserves_combined_playback_until_selected_call_changes(tmp_path):
    run_browser_logic(tmp_path, r'''
state.snapshot=snapshot([session()],[recording()]);render();openCall();
const audio=$('call-audio');
assert.equal(audio.src,`/api/recordings/${SID}/audio?track=combined`);
assert.equal(audio.loads,1);assert.equal(audio.pauses,1);assert.equal(audio.plays,0);
audio.play();audio.currentTime=17;
for(let i=0;i<5;i++){state.snapshot.sessions[0].segments[0].text='update '+i;render();}
assert.equal($('call-audio'),audio);assert.equal(audio.loads,1);assert.equal(audio.pauses,1);
assert.equal(audio.currentTime,17);assert.equal(audio.paused,false);assert.equal($('audio-status').hidden,true);
assert.equal($('audio-status').textContent,'');
state.snapshot.sessions.unshift({...session(OTHER),started_at:'2026-09-26T13:00:00Z',ended_at:null,status:'live'});
state.snapshot.recordings.recordings.push(recording(OTHER));render();
assert.equal(state.selected,SID);assert.equal(audio.loads,1);
openCall(OTHER);
assert.equal(audio.src,`/api/recordings/${OTHER}/audio?track=combined`);assert.equal(audio.loads,2);assert.equal(audio.paused,true);
render();assert.equal(audio.loads,2);assert.equal(audio.plays,1);
assert.equal($('audio-download').href,audio.src);assert.equal($('audio-download').download,`${OTHER}-combined.wav`);
''')


def test_recordings_without_transcripts_are_selectable_and_urls_stay_local(tmp_path):
    run_browser_logic(tmp_path, r'''
const entry=recording();entry.url='https://attacker.invalid/collect';entry.tracks.inbound.url='javascript:alert(1)';
state.snapshot=snapshot([],[entry]);state.snapshot.enabled=false;render();openCall();
assert.equal(state.selected,SID);assert.equal($('call-list').children.length,1);
assert.equal($('call-list').children.length,1);assert.equal($('sidebar-empty').hidden,true);
assert.equal($('transcript-title').textContent,'Call transcript');assert.equal($('empty-title').textContent,'No transcript for this recording.');
assert.equal($('export-json').attributes['aria-disabled'],'true');assert.ok(!$('export-json').href);
assert.equal($('call-audio').src,`/api/recordings/${SID}/audio?track=combined`);
assert.equal($('audio-download').href,$('call-audio').src);
const src=$('call-audio').src,loads=$('call-audio').loads;
state.snapshot.recordings.recordings=[{...entry,call_sid:'../secret'}];render();openCall('../secret');
assert.equal(state.sessions.length,0);assert.equal($('call-list').children.length,0);
// Falling outside the latest history window retains the authorized selected recording;
// an invalid SID can neither replace it nor become a media/download URL.
assert.equal(state.selected,SID);assert.equal($('call-audio').src,src);assert.equal($('call-audio').loads,loads);
assert.equal($('audio-download').href,src);
''')


def test_combined_audio_availability_and_playback_errors_remain_clear(tmp_path):
    run_browser_logic(tmp_path, r'''
state.snapshot=snapshot();render();assert.equal($('audio-panel').hidden,true);
state.snapshot=snapshot([{...session(),ended_at:null,status:'live'}]);render();openCall();
assert.equal($('audio-panel').hidden,false);assert.equal($('call-audio').hidden,true);
assert.ok($('audio-status').textContent.includes('not finalized'));assert.equal($('audio-status').hidden,false);
const entry=recording();entry.status='partial';entry.url=null;entry.tracks.outbound.url=null;
state.snapshot.recordings.recordings=[entry];render();
// The remaining individual track must not silently become the conversation player.
assert.equal($('call-audio').hidden,true);assert.equal($('call-audio').src,'');
assert.equal($('audio-download').attributes['aria-disabled'],'true');
entry.url=`/api/recordings/${SID}/audio?track=combined`;render();
assert.equal($('call-audio').src,entry.url);assert.equal($('call-audio').hidden,false);
assert.equal($('audio-status').hidden,false);assert.ok($('audio-status').textContent.includes('incomplete'));
entry.status='completed';render();
$('call-audio').events.canplay();assert.equal($('audio-status').hidden,true);assert.equal($('audio-status').textContent,'');
const loads=$('call-audio').loads;$('call-audio').events.error();render();
assert.ok($('audio-status').textContent.includes('could not be loaded'));assert.equal($('call-audio').loads,loads);
assert.equal($('audio-status').hidden,false);
entry.url=null;entry.tracks.inbound.url=null;entry.status='failed';render();
assert.equal($('call-audio').hidden,true);assert.ok($('audio-status').textContent.includes('could not be finalized'));
state.snapshot.recordings.storage_error='/private/path';render();
assert.equal($('recording-warning').hidden,false);assert.ok(!$('recording-warning').textContent.includes('/private/path'));
assert.equal(voicemailLabel({recording_status:'processing'}),'Unconfirmed recording');
assert.ok(voicemailDescriptions.processing.includes('has not been confirmed'));
''')


def test_combined_line_highlights_follow_overlap_seek_and_each_track_duration(tmp_path):
    run_browser_logic(tmp_path, r'''
const call=session();call.tracks={inbound:{interim:'Unfinished text'}};
call.segments=[
 {id:'input',track:'inbound',start_ms:0,end_ms:3000,text:'caller'},
 {id:'playback',track:'outbound',start_ms:1000,end_ms:4000,text:'overlap'},
 {id:'zero',track:'inbound',start_ms:2000,end_ms:2000,text:'zero length'},
 {id:'later',track:'inbound',start_ms:4000,end_ms:6000,text:'later'}];
const saved=recording();saved.tracks.inbound.duration_seconds=5;saved.tracks.outbound.duration_seconds=2.5;
state.snapshot=snapshot([call],[saved]);render();openCall();
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
audio.currentTime=1.5;audio.events.seeking();assert.deepEqual(highlighted(),['input','playback']);
audio.paused=true;audio.events.pause();assert.deepEqual(highlighted(),['input','playback']);
audio.play();audio.currentTime=1.5;audio.events.timeupdate();assert.deepEqual(highlighted(),['input','playback']);
audio.ended=true;audio.events.ended();assert.deepEqual(highlighted(),[]);render();assert.deepEqual(highlighted(),[]);
audio.ended=false;audio.currentTime=1.5;audio.events.seeked();assert.deepEqual(highlighted(),['input','playback']);
''')


def test_highlights_survive_polled_text_updates_and_clear_on_another_call(tmp_path):
    run_browser_logic(tmp_path, r'''
(async()=>{
const call=session();call.segments=[{id:'one',track:'inbound',start_ms:0,end_ms:3000,text:'first wording'}];
state.snapshot=snapshot([call,session(OTHER)],[recording(),recording(OTHER)]);render();openCall();
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


def test_playback_highlights_scroll_only_visible_detail_and_live_text_respects_reading_position(tmp_path):
    run_browser_logic(tmp_path, r'''
const call=session();call.segments[0].end_ms=3000;
state.snapshot=snapshot([call],[recording()]);render();openCall();
const audio=$('call-audio'),viewport=$('transcript'),row=state.transcriptRows[0].row;
viewport.rect={top:100,bottom:300};row.rect={top:150,bottom:210};viewport.scrollTop=40;
audio.play();audio.currentTime=1.5;audio.events.timeupdate();assert.equal(viewport.scrollTop,40);
row.rect={top:350,bottom:410};audio.events.timeupdate();assert.equal(viewport.scrollTop,158);
row.rect={top:150,bottom:210};audio.events.timeupdate();assert.equal(viewport.scrollTop,158);
backToCalls();row.rect={top:350,bottom:410};audio.events.timeupdate();assert.equal(viewport.scrollTop,158);
row.rect={top:150,bottom:210};openCall();showPage('agents');
row.rect={top:350,bottom:410};audio.events.timeupdate();assert.equal(viewport.scrollTop,158);
row.rect={top:150,bottom:210};
showPage('calls');
audio.ended=true;audio.events.ended();
call.status='live';call.ended_at=null;
viewport.clientHeight=100;viewport.scrollHeight=250;viewport.scrollTop=140;
call.segments.push({id:'new',track:'outbound',start_ms:4000,end_ms:6000,text:'new text'});
render();assert.equal(viewport.scrollTop,250);
viewport.scrollTop=0;viewport.scrollHeight=500;
call.segments.push({id:'later',track:'outbound',start_ms:7000,end_ms:8000,text:'more text'});
render();assert.equal(viewport.scrollTop,0);
''')


def test_live_updates_never_replace_selected_history_even_after_playback_ends(tmp_path):
    run_browser_logic(tmp_path, r'''
const THIRD='CA'+'c'.repeat(32),FOURTH='CA'+'d'.repeat(32);
state.snapshot=snapshot([session(),session(THIRD)],[recording(),recording(THIRD)]);render();openCall();
const audio=$('call-audio');audio.play();audio.currentTime=17;audio.paused=true;audio.events.pause();
state.snapshot.sessions.unshift({...session(OTHER),started_at:'2026-09-26T13:00:00Z',ended_at:null,status:'live'});
render();assert.equal(state.selected,SID);assert.equal(audio.currentTime,17);
assert.equal($('live-call-notice').hidden,false);
openCall(THIRD);render();assert.equal(state.selected,THIRD);
audio.play();audio.paused=true;audio.events.pause();
state.snapshot.sessions.unshift({...session(FOURTH),started_at:'2026-09-26T14:00:00Z',ended_at:null,status:'live'});
render();assert.equal(state.selected,THIRD);
audio.ended=true;audio.events.ended();render();assert.equal(state.selected,THIRD);
state.paused=true;$('live-call-open').events.click();assert.equal(state.selected,FOURTH);
assert.equal(state.detail,true);assert.equal($('live-call-notice').hidden,true);
''')


def test_recent_and_voicemail_are_deduplicated_disjoint_categories(tmp_path):
    run_browser_logic(tmp_path, r'''
state.snapshot=snapshot([session(),session(OTHER),session(OTHER)],[recording(),recording(OTHER)]);
const voicemail={call_sid:SID,recording_status:'completed',started_at:'2026-09-26T12:00:00Z',duration_seconds:12};
state.snapshot.voicemail.voicemails=[voicemail,{...voicemail}];
state.snapshot.call_details={enabled:true,calls:[{call_sid:SID,caller_number:'+14155550111',summary:{text:'Saved voicemail summary.'}}]};
render();
assert.equal(state.sessions.filter(call=>call.call_sid===SID).length,1);
assert.equal(state.sessions.filter(call=>call.call_sid===OTHER).length,1);
assert.equal($('call-list').children.length,1);assert.equal($('call-list').children[0].dataset.callSid,OTHER);
assert.equal($('voicemail-list').children.length,1);
assert.equal($('voicemail-list').children[0].dataset.callSid,SID);
showCollection('voicemail');assert.equal($('voicemail-panel').hidden,false);assert.equal($('recent-panel').hidden,true);
const visible=node=>node.textContent+' '+node.children.map(visible).join(' ');
assert.ok(!visible($('call-list')).includes(SID.slice(-12)));
assert.ok(!visible($('voicemail-list')).includes(SID.slice(-12)));
''')


def test_call_breadcrumbs_replace_stepper_and_clock_with_accessible_links():
    parsed = AudioMarkup()
    parsed.feed(HTML.read_text())
    assert parsed.tags["call-breadcrumbs"] == "nav"
    assert parsed.attributes["call-breadcrumbs"]["aria-label"] == "Call navigation"
    assert parsed.tags["breadcrumb-calls"] == "a"
    assert parsed.attributes["breadcrumb-calls"]["href"] == "#calls/recent"
    assert parsed.tags["breadcrumb-caller"] == "a"
    assert parsed.tags["breadcrumb-date"] == "span"
    assert parsed.attributes["breadcrumb-date"]["aria-current"] == "page"
    assert not {"detail-back", "detail-position", "detail-previous", "detail-next", "updated-at"} & set(parsed.ids)


def test_calls_breadcrumb_returns_to_unfiltered_list_without_resetting_audio(tmp_path):
    run_browser_logic(tmp_path, r'''
state.snapshot=snapshot([session(),session(OTHER)],[recording(),recording(OTHER)]);
state.snapshot.call_details={calls:[{call_sid:SID,caller_number:'+16562520233',started_at:'2026-09-26T16:46:00Z'}]};
render();$('collection-scroller').scrollTop=87;openCall();
assert.equal($('call-breadcrumbs').hidden,false);
assert.equal($('breadcrumb-caller').textContent,'+16562520233');
assert.equal($('breadcrumb-caller').getAttribute('href'),'#calls/recent?caller=%2B16562520233');
assert.equal($('breadcrumb-caller-label').hidden,true);
assert.equal($('breadcrumb-date').textContent,clockTime('2026-09-26T16:46:00Z',true));
const audio=$('call-audio');audio.play();audio.currentTime=19;
const src=audio.src,loads=audio.loads,pauses=audio.pauses;
let prevented=0;
$('breadcrumb-calls').events.click({preventDefault(){prevented++;}});
assert.equal(prevented,1);assert.equal(state.collection,'recent');assert.equal(state.detail,false);
assert.equal(state.callerFilter,'');assert.equal(location.hash,'#calls/recent');
assert.equal($('collection-scroller').scrollTop,87);assert.equal($('call-breadcrumbs').hidden,true);
assert.equal(audio.src,src);assert.equal(audio.currentTime,19);assert.equal(audio.paused,false);
assert.equal(audio.loads,loads);assert.equal(audio.pauses,pauses);
state.paused=true;$('audio-return').events.click();
assert.equal(state.detail,true);assert.equal(state.selected,SID);
assert.equal(audio.currentTime,19);assert.equal(audio.loads,loads);assert.equal(audio.pauses,pauses);
''')


def test_caller_breadcrumb_filters_both_collections_through_polling_and_history(tmp_path):
    run_browser_logic(tmp_path, r'''
const THIRD='CA'+'c'.repeat(32), FOURTH='CA'+'d'.repeat(32), phone='+16562520233';
state.snapshot=snapshot([session(),session(OTHER),session(THIRD)],[recording()]);
state.snapshot.call_details={calls:[{call_sid:SID,caller_number:phone},
 {call_sid:OTHER,caller_number:'+16562520234'}, {call_sid:THIRD,caller_number:'+1 (656) 252-0233'}]};
state.snapshot.voicemail.voicemails=[{call_sid:THIRD,recording_status:'completed'}];
render();openCall();
const audio=$('call-audio');audio.play();audio.currentTime=19;
const loads=audio.loads,pauses=audio.pauses;
$('breadcrumb-caller').events.click({preventDefault(){}});
assert.equal(location.hash,'#calls/recent?caller=%2B16562520233');
assert.equal(state.detail,false);assert.equal(state.selected,SID);assert.equal(state.callerFilter,phone);
assert.equal($('breadcrumb-caller').hidden,true);assert.equal($('breadcrumb-caller-label').textContent,phone);
assert.equal($('breadcrumb-caller-label').getAttribute('aria-current'),'page');
assert.equal($('breadcrumb-date-item').hidden,true);
assert.deepEqual($('call-list').children.map(row=>row.dataset.callSid),[SID]);
assert.deepEqual($('voicemail-list').children.map(row=>row.dataset.callSid),[THIRD]);
assert.equal($('recent-count').textContent,'1');assert.equal($('voicemail-count').textContent,'1');
state.snapshot.sessions.push(session(FOURTH));
state.snapshot.call_details.calls.push({call_sid:FOURTH,caller_number:phone});render();
assert.deepEqual($('call-list').children.map(row=>row.dataset.callSid),[SID,FOURTH]);
assert.equal($('recent-count').textContent,'2');
showCollection('voicemail');assert.equal(location.hash,'#calls/voicemail?caller=%2B16562520233');
assert.equal($('voicemail-panel').hidden,false);assert.equal(state.callerFilter,phone);
showCollection('recent');openCall();
assert.equal(location.hash,`#calls/recent/${SID}?caller=%2B16562520233`);
state.paused=true;location.hash='#calls/voicemail?caller=%2B16562520233';handlers.get('popstate')();
assert.equal(state.detail,false);assert.equal(state.collection,'voicemail');assert.equal(state.callerFilter,phone);
assert.equal($('voicemail-list').children.length,1);
location.hash='#calls/recent?caller=%2B16562520234';handlers.get('hashchange')();
assert.equal(state.callerFilter,'+16562520234');assert.deepEqual($('call-list').children.map(row=>row.dataset.callSid),[OTHER]);
location.hash=`#calls/recent/${SID}?caller=%2B16562520233`;handlers.get('popstate')();
assert.equal(state.detail,true);assert.equal(state.callerFilter,phone);
assert.equal(audio.currentTime,19);assert.equal(audio.paused,false);
assert.equal(audio.loads,loads);assert.equal(audio.pauses,pauses);
$('breadcrumb-calls').events.click({preventDefault(){}});
assert.equal(state.callerFilter,'');assert.equal($('call-list').children.length,3);
assert.equal(location.hash,'#calls/recent');
''')


def test_caller_navigation_handles_unknowns_native_links_and_external_call_selection(tmp_path):
    run_browser_logic(tmp_path, r'''
state.snapshot=snapshot([session(),session(OTHER)],[recording(),recording(OTHER)]);render();openCall();
assert.equal($('breadcrumb-caller').hidden,true);
assert.equal($('breadcrumb-caller').getAttribute('href'),null);
assert.equal($('breadcrumb-caller-label').textContent,'Unknown caller');
showCallerCalls();assert.equal(state.detail,true);assert.equal(state.callerFilter,'');
state.snapshot.call_details={calls:[{call_sid:SID,caller_number:'+16562520233'},
 {call_sid:OTHER,caller_number:'+16562520234'}]};render();
let prevented=0;
for(const id of ['breadcrumb-calls','breadcrumb-caller']){
 $(id).events.click({metaKey:true,preventDefault(){prevented++;}});
}
assert.equal(prevented,0);assert.equal(state.detail,true);assert.equal(state.callerFilter,'');
showCallerCalls();openCall(OTHER);
assert.equal(state.callerFilter,'');assert.equal(location.hash,`#calls/recent/${OTHER}`);
state.paused=true;
for(const caller of ['unknown','%E0%A4%A','16562520233','%2B16562520233suffix']){
 location.hash='#calls/recent?caller='+caller;handlers.get('hashchange')();
 assert.equal(state.callerFilter,'');assert.equal($('call-list').children.length,2);
}
location.hash='#calls/recent?caller=%2B16562529999';handlers.get('hashchange')();
assert.equal($('call-list').children.length,0);assert.equal($('sidebar-empty').hidden,false);
assert.equal($('sidebar-empty').textContent,'No recent calls from this number.');
''')


def test_return_to_call_reopens_retained_recording_after_it_leaves_recent_history(tmp_path):
    run_browser_logic(tmp_path, r'''
state.snapshot=snapshot([session(),session(OTHER)],[recording(),recording(OTHER)]);render();openCall();
const audio=$('call-audio');audio.play();audio.currentTime=17;
const src=audio.src,loads=audio.loads,pauses=audio.pauses;
backToCalls();
state.snapshot=snapshot([session(OTHER)],[recording(OTHER)]);render();
assert.equal(state.sessions.some(call=>call.call_sid===SID),false);
assert.equal($('call-list').children.length,1);assert.equal($('call-list').children[0].dataset.callSid,OTHER);
showPage('contacts');assert.equal($('audio-return').hidden,false);
state.paused=true;$('audio-return').events.click();
assert.equal(state.page,'calls');assert.equal(state.detail,true);assert.equal(state.selected,SID);
assert.equal($('calls-detail').hidden,false);assert.equal($('transcript-title').textContent,'Call transcript');
assert.equal($('session-storage-warning').hidden,false);
assert.ok($('session-storage-warning').textContent.includes('last update'));
assert.equal(state.transcriptRows[0].row.dataset.segmentId,'one');
assert.equal(audio.src,src);assert.equal(audio.currentTime,17);assert.equal(audio.paused,false);
assert.equal(audio.loads,loads);assert.equal(audio.pauses,pauses);
assert.equal($('audio-download').href,src);assert.equal($('audio-return').hidden,false);
''')


def test_first_live_detail_opens_at_latest_text_after_its_pane_is_visible(tmp_path):
    run_browser_logic(tmp_path, r'''
state.snapshot=snapshot([{...session(),ended_at:null,status:'live'}]);render();
const viewport=$('transcript');viewport.clientHeight=200;
// A hidden ancestor has no layout height in the browser. Model that behavior
// so opening a live call cannot mistake display:none for a short transcript.
Object.defineProperty(viewport,'scrollHeight',{get(){
 return $('dashboard-view').hidden||$('calls-detail').hidden?0:620;
}});
assert.equal($('calls-detail').hidden,true);assert.equal(viewport.scrollHeight,0);
openCall();
assert.equal($('calls-detail').hidden,false);assert.equal(viewport.scrollHeight,620);
assert.equal(viewport.scrollTop,620);assert.equal(state.selected,SID);
assert.equal($('messages').hidden,false);
''')


def test_new_live_call_scrolls_to_latest_text_when_selected_snapshot_finishes_loading(tmp_path):
    run_browser_logic(tmp_path, r'''
const live={...session(OTHER),ended_at:null,status:'live',segments:[]};
state.snapshot=snapshot([session(),live],[recording()]);render();openCall();
const viewport=$('transcript');viewport.clientHeight=200;viewport.scrollHeight=100;
let scrollTop=0;
// Browsers clamp a scroll assignment while the loading placeholder is shorter
// than the viewport; it cannot establish the eventual transcript's end position.
Object.defineProperty(viewport,'scrollTop',{
 get(){return scrollTop;},
 set(value){scrollTop=Math.max(0,Math.min(value,viewport.scrollHeight-viewport.clientHeight));}
});
openCall(OTHER);
assert.equal(state.snapshot.selected_call_sid,SID);assert.equal(state.selected,OTHER);
assert.equal($('empty-title').textContent,'Loading this conversation…');assert.equal(viewport.scrollTop,0);
live.segments=[{id:'latest',track:'inbound',start_ms:30000,end_ms:32000,text:'The newest live speech.'}];
state.snapshot=snapshot([live,session()],[recording()]);
viewport.scrollHeight=620;render();
assert.equal(state.snapshot.selected_call_sid,OTHER);assert.equal($('empty').hidden,true);
assert.equal(viewport.scrollTop,420); // The maximum visible scroll position.
assert.equal(state.transcriptRows[0].row.dataset.segmentId,'latest');
''')


def test_category_tabs_switch_with_keyboard_and_preserve_selected_audio(tmp_path):
    run_browser_logic(tmp_path, r'''
state.snapshot=snapshot([session()],[recording()]);render();openCall();
const audio=$('call-audio');audio.play();audio.currentTime=17;
const src=audio.src,loads=audio.loads,pauses=audio.pauses;
let prevented=0;
const key=(tab,value)=>$(tab).events.keydown({key:value,preventDefault(){prevented++;}});
key('tab-recent','ArrowRight');
assert.equal(state.collection,'voicemail');assert.equal(state.detail,false);
assert.equal($('tab-voicemail').attributes['aria-selected'],'true');
assert.equal($('tab-recent').attributes['aria-selected'],'false');
assert.equal(document.activeElement,$('tab-voicemail'));
assert.equal($('voicemail-panel').hidden,false);assert.equal($('recent-panel').hidden,true);
key('tab-voicemail','Home');assert.equal(state.collection,'recent');
key('tab-recent','End');assert.equal(state.collection,'voicemail');
key('tab-voicemail','ArrowLeft');assert.equal(state.collection,'recent');
assert.equal(prevented,4);
assert.equal(state.selected,SID);assert.equal(audio.src,src);assert.equal(audio.currentTime,17);
assert.equal(audio.paused,false);assert.equal(audio.loads,loads);assert.equal(audio.pauses,pauses);
''')


def test_browser_navigation_restores_routes_without_restart_or_auto_selection(tmp_path):
    run_browser_logic(tmp_path, r'''
state.snapshot=snapshot([session()],[recording()]);render();
showCollection('voicemail');assert.equal(location.hash,'#calls/voicemail');assert.equal(state.selected,null);
showCollection('recent');openCall();assert.equal(location.hash,`#calls/recent/${SID}`);
const audio=$('call-audio');audio.play();audio.currentTime=17;
const loads=audio.loads,pauses=audio.pauses;
showPage('contacts');assert.equal(location.hash,'#contacts');
state.paused=true;location.hash=`#calls/recent/${SID}`;handlers.get('popstate')();
assert.equal(state.page,'calls');assert.equal(state.detail,true);assert.equal(state.selected,SID);
location.hash='#calls/recent';handlers.get('hashchange')();
assert.equal(state.detail,false);assert.equal(state.collection,'recent');assert.equal(state.selected,SID);
assert.equal(audio.currentTime,17);assert.equal(audio.paused,false);
assert.equal(audio.loads,loads);assert.equal(audio.pauses,pauses);
''')


def test_caller_metadata_and_saved_summary_render_safely_without_restarting_playback(tmp_path):
    run_browser_logic(tmp_path, r'''
const saved=recording(),call=session();call.segments[0].end_ms=3000;
state.snapshot=snapshot([call,session(OTHER)],[saved,recording(OTHER)]);
const details={call_sid:SID,caller_number:'+14155550111',started_at:'2026-09-26T13:00:00Z',ended_at:'2026-09-26T13:03:00Z',
 duration_seconds:180,summary:{text:'<img src=x onerror=alert(1)> A saved agent summary.',source:'agent',created_at:'2026-09-26T13:04:00Z'}};
state.snapshot.call_details={enabled:true,storage_error:'',calls:[details]};render();openCall();
const button=$('call-list').children.find(row=>row.dataset.callSid===SID);
assert.equal(button.children[0].textContent,'+14155550111');
assert.equal(button.children[2].textContent,details.summary.text);
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
const call=session(),saved=recording();state.snapshot=snapshot([call],[saved]);render();openCall();
let button=$('call-list').children[0];
assert.equal(button.children[0].textContent,'Unknown caller');
assert.equal(button.children[1].children[0].textContent,clockTime(call.started_at,true));
assert.equal(button.children[1].children[1].textContent,'Duration · 02:00');
assert.equal($('call-summary').textContent,'No summary yet.');
saved.duration_seconds=undefined;call.ended_at='2026-09-26T12:01:00Z';render();
assert.equal($('call-list').children[0].children[1].children[1].textContent,'Duration · 01:00');
call.ended_at=null;render();assert.ok($('call-list').children[0].children[1].children[1].textContent.endsWith('Duration unavailable'));
state.snapshot.call_details={enabled:true,storage_error:'',calls:[{call_sid:SID,caller_number:'',duration_seconds:0,summary:null}]};render();
assert.equal($('call-list').children[0].children[0].textContent,'Unknown caller');
assert.ok($('call-list').children[0].children[1].children[1].textContent.endsWith('00:00')); // Zero is known, not missing.
''')


def test_metadata_only_calls_keep_their_saved_summary_selectable(tmp_path):
    run_browser_logic(tmp_path, r'''
state.snapshot=snapshot();state.snapshot.enabled=false;
state.snapshot.call_details={enabled:true,storage_error:'',calls:[{call_sid:SID,caller_number:'+14155550222',
 started_at:'2026-09-26T12:00:00Z',ended_at:'2026-09-26T12:01:00Z',duration_seconds:60,
 summary:{text:'Saved summary without live transcription.',source:'agent',created_at:'2026-09-26T12:02:00Z'}}]};render();openCall();
assert.equal(state.selected,SID);assert.equal($('call-list').children.length,1);
assert.equal($('call-list').children[0].children[0].textContent,'+14155550222');
assert.equal($('call-summary').textContent,'Saved summary without live transcription.');
assert.equal($('export-json').attributes['aria-disabled'],'true');assert.equal($('call-audio').hidden,true);
assert.equal(trackNames.outbound,'New College');
''')


def test_contact_changes_refresh_both_call_lists_without_disturbing_audio_or_focus(tmp_path):
    run_browser_logic(tmp_path, r'''
state.snapshot=snapshot([session(),session(OTHER)],[recording()]);
const phone='+14155550111';
state.snapshot.call_details={calls:[{call_sid:SID,caller_number:phone},{call_sid:OTHER,caller_number:phone}]};
state.snapshot.voicemail.voicemails=[{call_sid:OTHER,recording_status:'completed'}];
render();openCall();backToCalls();
const audio=$('call-audio');audio.play();audio.currentTime=19;
const loads=audio.loads,pauses=audio.pauses;
$('call-list').children[0].focus();
let contact={id:'local-test-contact',name:'<img src=x> Taylor Demo',phone}, synchronized;
window.DashboardCRM={findContactByPhone: number=>number===phone?contact:null,
 setSessions:sessions=>{synchronized=sessions;},render(){}};
handlers.get('dashboard-contacts-changed')();
for(const list of ['call-list','voicemail-list']){
 const row=$(list).children[0];
 assert.equal(row.children[0].textContent,contact.name);
 assert.equal(row.children[0].children.length,1);
 assert.equal(row.children[0].children[0].textContent,phone);
 assert.equal(row.children[1].className,'call-time');
 assert.equal(row.children[2].className,'call-summary-preview');
}
assert.equal(document.activeElement.dataset.callSid,SID);
assert.equal(synchronized,state.sessions);
contact={...contact,name:'Taylor Renamed'};render();
assert.equal($('call-list').children[0].children[0].textContent,'Taylor Renamed');
contact=null;handlers.get('dashboard-contacts-changed')();
assert.equal($('call-list').children[0].children[0].textContent,phone);
assert.equal($('call-list').children[0].children[0].children.length,0);
assert.equal(audio.currentTime,19);assert.equal(audio.paused,false);
assert.equal(audio.loads,loads);assert.equal(audio.pauses,pauses);
''')


def test_return_link_uses_recording_call_date_and_retains_it_outside_recent_history(tmp_path):
    run_browser_logic(tmp_path, r'''
state.snapshot=snapshot([session(),session(OTHER)],[recording(),recording(OTHER)]);
const startedAt='2026-09-26T16:46:00Z';
state.snapshot.call_details={calls:[{call_sid:SID,caller_number:'+19419930832',started_at:startedAt}]};
render();openCall();showPage('contacts');
const expected='Return to call · +19419930832 · '+clockTime(startedAt,true)+' →';
assert.equal($('audio-return').textContent,expected);
state.snapshot=snapshot([session(OTHER)],[recording(OTHER)]);render();
assert.equal($('audio-return').textContent,expected);
assert.equal(state.audioSession.call_sid,SID);
state.audioSession.call_detail.started_at='invalid';renderNavigation();
assert.equal($('audio-return').textContent,'Return to call · +19419930832 · Time unavailable →');
''')


@pytest.mark.parametrize("has_recording", [False, True])
def test_return_link_stays_available_for_selected_call_across_views_and_recording_states(tmp_path, has_recording):
    run_browser_logic(tmp_path, "const hasRecording=" + json.dumps(has_recording) + ";\n" + r'''
state.snapshot=snapshot([session(),session(OTHER)],hasRecording?[recording()]:[]);render();
assert.equal($('audio-return').hidden,true);assert.equal($('audio-panel').hidden,true);
state.snapshot.call_details={calls:[{call_sid:SID,caller_number:'+19419930832'}]};openCall();
const audio=$('call-audio');
if(hasRecording){audio.play();audio.currentTime=19;}
const src=audio.src,loads=audio.loads,pauses=audio.pauses,plays=audio.plays;
const expected='Return to call · +19419930832 · '+clockTime(callStartedAt(state.audioSession),true)+' →';
function assertReturnVisible(){
 assert.equal($('audio-panel').hidden,false);
 assert.equal($('audio-return').hidden,false);
 assert.equal($('audio-return').textContent,expected);
}
assertReturnVisible(); // The link remains visible on the active call, too.
assert.equal($('call-audio').hidden,!hasRecording);
backToCalls();assertReturnVisible();
showPage('contacts');assertReturnVisible();
state.snapshot=snapshot([session(OTHER)]);render();assertReturnVisible();
assert.equal(state.sessions.some(call=>call.call_sid===SID),false);
state.paused=true;$('audio-return').events.click();assertReturnVisible();
assert.equal(state.page,'calls');assert.equal(state.detail,true);assert.equal(state.selected,SID);
assert.equal(audio.src,src);assert.equal(audio.loads,loads);assert.equal(audio.pauses,pauses);assert.equal(audio.plays,plays);
assert.equal(audio.currentTime,hasRecording?19:0);assert.equal(audio.paused,!hasRecording);
$('audio-close').events.click();render();assert.equal($('audio-panel').hidden,true);
$('audio-reopen').events.click();assertReturnVisible();assert.equal(audio.paused,true);
assert.equal(audio.src,src);assert.equal(audio.loads,loads);assert.equal(audio.plays,plays);
''')


def test_caller_breadcrumb_follows_contact_changes_without_changing_filter_or_playback(tmp_path):
    run_browser_logic(tmp_path, r'''
const phone='+19412982923',formattedPhone='+1 (941) 298-2923';
state.snapshot=snapshot([session(),session(OTHER)],[recording()]);
state.snapshot.call_details={calls:[{call_sid:SID,caller_number:formattedPhone},
 {call_sid:OTHER,caller_number:'+19412982924'}]};render();openCall();
const audio=$('call-audio');audio.play();audio.currentTime=19;
const src=audio.src,loads=audio.loads,pauses=audio.pauses,plays=audio.plays;
const filterHref='#calls/recent?caller=%2B19412982923';
let contact={id:'local-breadcrumb-contact',name:'<img src=x> Taylor Demo',phone};
window.DashboardCRM={findContactByPhone:number=>number===phone?contact:null,render(){}};
handlers.get('dashboard-contacts-changed')();
const link=$('breadcrumb-caller'),label=$('breadcrumb-caller-label');
assert.equal(link.hidden,false);assert.equal(link.textContent,contact.name);assert.equal(link.children.length,0);
assert.equal(link.getAttribute('href'),filterHref);assert.equal(link.getAttribute('title'),phone);
assert.equal(link.getAttribute('aria-label'),'Calls from '+contact.name+' ('+phone+')');
link.events.click({preventDefault(){}});
assert.equal(location.hash,filterHref);assert.equal(state.callerFilter,phone);
assert.equal(link.hidden,true);assert.equal(label.hidden,false);
assert.equal(label.textContent,contact.name);assert.equal(label.getAttribute('title'),phone);
assert.equal(label.getAttribute('aria-current'),'page');
assert.deepEqual($('call-list').children.map(row=>row.dataset.callSid),[SID]);
contact={...contact,name:'Taylor Renamed'};handlers.get('dashboard-contacts-changed')();
assert.equal(label.textContent,'Taylor Renamed');assert.equal(label.getAttribute('title'),phone);
assert.equal(location.hash,filterHref);assert.equal(state.callerFilter,phone);
openCall();assert.equal(link.textContent,'Taylor Renamed');assert.equal(link.getAttribute('href'),filterHref);
contact=null;handlers.get('dashboard-contacts-changed')();
assert.equal(link.textContent,phone);assert.equal(link.getAttribute('href'),filterHref);
assert.equal(link.getAttribute('title'),null);assert.equal(link.getAttribute('aria-label'),'Calls from '+phone);
link.events.click({preventDefault(){}});
assert.equal(label.textContent,phone);assert.equal(label.getAttribute('title'),null);
assert.equal(audio.src,src);assert.equal(audio.currentTime,19);assert.equal(audio.paused,false);
assert.equal(audio.loads,loads);assert.equal(audio.pauses,pauses);assert.equal(audio.plays,plays);
''')
