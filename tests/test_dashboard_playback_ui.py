"""Focused product and boundary checks; test helpers live in support."""

from support.dashboard_playback_ui import (run_browser_logic)


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
 assert.equal($('page-title').textContent,page[0].toUpperCase()+page.slice(1));
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
