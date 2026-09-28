"""Network changes recover reads without reloading the user's current work."""

import pytest

from test_dashboard_playback_ui import run_browser_logic


TIMERS = r"""
const timers = new Map(); let timerId = 0;
setTimeout = (callback, delay) => {const id = ++timerId; timers.set(id, {callback, delay}); return id;};
clearTimeout = id => timers.delete(id);
const tick = () => new Promise(resolve => setImmediate(resolve));
function fireTimer(delay) {
 const entry = [...timers].find(([, timer]) => timer.delay === delay);
 assert.ok(entry, `Expected a ${delay}ms timer`);
 timers.delete(entry[0]); return entry[1].callback();
}
"""


@pytest.mark.parametrize("stalled_stage", ["headers", "body"])
def test_network_timeout_preserves_draft_playback_and_retries(tmp_path, stalled_stage):
    run_browser_logic(tmp_path, TIMERS + f"const stalledStage={stalled_stage!r};\n" + r"""
(async () => {
state.snapshot=snapshot([session()],[recording()]);render();openCall();
const audio=$('call-audio');audio.play();audio.currentTime=31;
showPage('contacts');
const draft=new Element('input');draft.value='Unsaved contact';$('contacts-view').append(draft);draft.focus();
const route=location.hash, loads=audio.loads, previous=state.snapshot;
let requests=0, signal;
fetch=async(url,options)=>{
 requests++;signal=options.signal;
 const wait=()=>new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError'))));
 return stalledStage==='headers' ? wait() : {ok:true,json:wait};
};
const pending=poll();await tick();
fireTimer(10000);await pending;
assert.equal(signal.aborted,true);assert.equal(state.polling,false);
assert.equal(state.snapshot,previous);assert.equal($('connection').attributes.title,'Reconnecting');
assert.match($('banner').textContent,/Showing the last update/);
assert.equal(timers.size,1);
fetch=async()=>{requests++;return {ok:true,json:async()=>snapshot([session()],[recording()])};};
await fireTimer(1000);
assert.equal(requests,2);assert.equal($('connection').attributes.title,'Connected');assert.equal($('banner').hidden,true);
assert.equal(state.page,'contacts');assert.equal(location.hash,route);assert.equal(state.selected,SID);
assert.equal(draft.value,'Unsaved contact');assert.equal(document.activeElement,draft);
assert.equal(audio.loads,loads);assert.equal(audio.currentTime,31);assert.equal(audio.paused,false);
assert.equal(timers.size,1,'Exactly one future poll remains scheduled');
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_online_focus_and_visible_events_recover_without_overlapping_polls(tmp_path):
    run_browser_logic(tmp_path, TIMERS + r"""
(async () => {
let requests=0, active=0, maximum=0;
fetch=async(url,options)=>{
 requests++;active++;maximum=Math.max(maximum,active);
 if(requests===2) {
  try {await new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError'))));}
  finally {active--;}
 }
 active--;return {ok:true,json:async()=>snapshot([session()])};
};
await poll();assert.equal(requests,1);assert.equal(timers.size,1);
document.hidden=true;documentHandlers.get('visibilitychange')();handlers.get('online')();handlers.get('focus')();
assert.equal(requests,1,'Background events do not start extra requests');
document.hidden=false;handlers.get('online')();
assert.equal(requests,2,'Returning online skips the normal polling delay');
handlers.get('focus')();documentHandlers.get('visibilitychange')();handlers.get('online')();
await tick();
assert.equal(requests,2);assert.equal(timers.size,1,'Recovery events coalesce into one pending refresh');
await fireTimer(0);
assert.equal(requests,3);assert.equal(maximum,1);assert.equal(timers.size,1);
assert.equal($('connection').attributes.title,'Connected');
// Each recovery event also refreshes an idle page immediately.
handlers.get('focus')();await tick();assert.equal(requests,4);
documentHandlers.get('visibilitychange')();await tick();assert.equal(requests,5);
assert.equal(maximum,1);assert.equal(timers.size,1);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_page_cache_restore_resumes_but_hidden_page_does_not_restart_polling(tmp_path):
    run_browser_logic(tmp_path, TIMERS + r"""
(async () => {
let requests=0;
fetch=async()=>{requests++;return {ok:true,json:async()=>snapshot([session()])};};
await poll();handlers.get('pagehide')();
assert.equal(timers.size,0);
handlers.get('online')();handlers.get('focus')();documentHandlers.get('visibilitychange')();
await tick();assert.equal(requests,1);
handlers.get('pageshow')({persisted:true});await tick();
assert.equal(requests,2);assert.equal(timers.size,1);assert.equal(state.paused,false);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
