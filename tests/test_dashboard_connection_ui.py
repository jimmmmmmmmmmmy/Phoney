"""Focused product and boundary checks; test helpers live in support."""

import pytest

from support.dashboard_connection_ui import TIMERS
from support.dashboard_playback_ui import run_browser_logic


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
