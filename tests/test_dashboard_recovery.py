"""Same-origin analysis retries plus the dashboard's failure/recovery controls."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard import register_dashboard
from tests.test_dashboard import Manager, SETTINGS, SID
from test_dashboard_playback_ui import run_browser_logic


class Analysis:
    def __init__(self):
        self.retries = []

    def status(self, sid):
        return {"call_sid": sid, "state": "failed", "retryable": True, "retry_token": "a" * 64}

    async def retry(self, sid, token):
        self.retries.append((sid, token))
        return {"accepted": token == "a" * 64, "reason": "conflict", "status": {
            **self.status(sid), "state": "queued", "retryable": False}}


def test_recovery_routes_validate_origin_and_surface_latest_status():
    app = FastAPI()
    service = app.state.detection_backfill = Analysis()
    register_dashboard(app, SETTINGS, Manager())
    with TestClient(app, base_url=SETTINGS.public_base_url) as client:
        status = client.get(f"/api/detection/{SID}")
        assert status.json()["state"] == "failed"
        assert status.headers["cache-control"] == "no-store"
        snapshot = client.get("/api/transcripts", params={"call_sid": SID}).json()
        assert snapshot["detection_status"]["call_sid"] == SID
        path = f"/api/detection/{SID}/retry"
        assert client.post(path, json={"retry_token": "a" * 64}).status_code == 403
        headers = {"Origin": SETTINGS.public_base_url, "X-Workspace-Request": "1"}
        assert client.post(path, json={"retry_token": "b" * 64}, headers=headers).status_code == 409
        assert client.post(path, json={"retry_token": "a" * 64}, headers=headers).status_code == 202
        assert client.post(path, json={"retry_token": "a" * 64, "call": "other"}, headers=headers).status_code == 400
        assert service.retries == [(SID, "b" * 64), (SID, "a" * 64)]
        assert client.get("/api/detection/not-a-call").status_code == 404


def test_retry_keeps_transcript_and_audio_and_rejects_duplicate_clicks(tmp_path):
    run_browser_logic(tmp_path, r'''
(async()=>{
state.snapshot=snapshot([session()],[recording()]);
state.snapshot.detection_status={call_sid:SID,state:'failed',retryable:true,retry_token:'a'.repeat(64)};
render();openCall();state.paused=true;
assert.equal($('analysis-recovery').hidden,false);assert.equal($('analysis-status').textContent,'Audio analysis failed');
const audio=$('call-audio');audio.play();audio.currentTime=17;
const loads=audio.loads,row=$('messages').children[0];
let release,requests=0;
fetch=async(url,options)=>{requests++;assert.equal(url,`/api/detection/${SID}/retry`);
 assert.equal(options.headers['X-Workspace-Request'],'1');
 assert.deepEqual(JSON.parse(options.body),{retry_token:'a'.repeat(64)});
 await new Promise(resolve=>release=resolve);
 return {ok:true,status:202,json:async()=>({accepted:true,status:{call_sid:SID,state:'queued',retryable:false}})};
};
const pending=retryAnalysis();await Promise.resolve();await retryAnalysis();
assert.equal(requests,1);assert.equal($('analysis-retry').disabled,true);
release();await pending;
assert.equal($('analysis-status').textContent,'Audio analysis queued');
assert.equal($('analysis-retry').hidden,true);assert.equal($('messages').children[0],row);
assert.equal(audio.loads,loads);assert.equal(audio.currentTime,17);assert.equal(audio.paused,false);
showPage('contacts');assert.equal($('analysis-recovery').hidden,true);
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


def test_ai_voicemail_inbox_and_fallback_message_playback_use_correct_timeline(tmp_path):
    run_browser_logic(tmp_path, r'''
const value=snapshot([session()],[recording()]);
value.voicemail={enabled:true,voicemails:[{call_sid:SID,mode:'voicemail_ai',recording_status:'completed'}]};
value.history={has_more:false,next_cursor:null,counts:{recent:30,voicemail:1},collection:'voicemail'};
state.snapshot=value;state.snapshotCollection='voicemail';state.collection='voicemail';render();openCall();
assert.equal($('voicemail-list').children.length,1);assert.equal($('call-list').children.length,0);
assert.equal($('recent-count').textContent,'30');assert.equal($('voicemail-count').textContent,'1');
assert.match($('voicemail-detail').textContent,/Voicemail audio saved/);
assert.doesNotMatch($('voicemail-detail').textContent,/Twilio/);
value.voicemail.voicemails[0]={call_sid:SID,mode:'voicemail_fallback',recording_status:'completed',recording_sid:'RE'+'c'.repeat(32)};
render();const audio=$('call-audio');
assert.equal(audio.src,`/api/voicemails/${SID}/audio`);
assert.match(audio.attributes['aria-label'],/voicemail message/);
assert.match($('audio-status').textContent,/Recorded message/);
audio.play();audio.currentTime=1.5;audio.events.timeupdate();
assert.ok(state.transcriptRows.every(item=>!item.row.classList.contains('playing-line')));
''')


def test_voicemail_pagination_does_not_reuse_recent_call_cursor(tmp_path):
    run_browser_logic(tmp_path, r'''
(async()=>{
state.snapshot=snapshot([session()]);state.snapshot.history={has_more:true,next_cursor:'recent-cursor',collection:'recent'};render();
showCollection('voicemail');assert.equal(historyState().next_cursor,null);
fetch=async url=>{assert.equal(new URL('https://local'+url).searchParams.get('collection'),'voicemail');
return {ok:true,json:async()=>({...snapshot([]),history:{has_more:true,next_cursor:'vm-cursor',collection:'voicemail'}})};};
await poll();assert.equal(historyState().next_cursor,'vm-cursor');
showCollection('recent');assert.equal(historyState().next_cursor,'recent-cursor');
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


def test_failed_ai_readback_keeps_original_audio_and_additional_recording(tmp_path):
    run_browser_logic(tmp_path, r'''
const call=session();
call.segments=[
 {id:'greeting',track:'outbound',source:'agent',delivery:'played',start_ms:0,end_ms:1000,text:'Please leave a message.'},
 {id:'message',track:'inbound',start_ms:1000,end_ms:3000,text:'This is Alex. Please call tomorrow.'}
];
const value=snapshot([call],[recording()]);
value.voicemail={enabled:true,voicemails:[{call_sid:SID,mode:'voicemail_fallback',recording_status:'completed',
 recording_sid:'RE'+'c'.repeat(32),duration_seconds:18}]};
state.snapshot=value;render();openCall();
const audio=$('call-audio'),extra=$('voicemail-extra-audio');
assert.equal(audio.src,`/api/recordings/${SID}/audio?track=combined`);
assert.equal(extra.hidden,false);assert.equal(extra.href,`/api/voicemails/${SID}/audio`);
assert.equal(extra.download,`${SID}-additional-message.wav`);
assert.match($('audio-status').textContent,/Original conversation/);
audio.play();audio.currentTime=1.5;audio.events.timeupdate();
assert.ok(state.transcriptRows.some(item=>item.row.classList.contains('playing-line')));
// Polling must not switch the caller away from the original timeline.
const loads=audio.loads;render();assert.equal(audio.loads,loads);assert.equal(audio.currentTime,1.5);
state.snapshot=snapshot([session(OTHER)],[recording(OTHER)]);render();openCall(OTHER);
assert.equal(extra.hidden,true);assert.equal(extra.href,undefined);
''')


def test_failed_ai_readback_without_local_audio_still_plays_native_recording(tmp_path):
    run_browser_logic(tmp_path, r'''
const call=session();call.segments.push({id:'agent',track:'outbound',source:'agent',text:'Please leave a message.'});
const value=snapshot([call],[]);
value.voicemail={enabled:true,voicemails:[{call_sid:SID,mode:'voicemail_fallback',recording_status:'completed',
 recording_sid:'RE'+'c'.repeat(32)}]};
state.snapshot=value;render();openCall();
assert.equal($('call-audio').src,`/api/voicemails/${SID}/audio`);
assert.equal($('voicemail-extra-audio').hidden,true);
''')
