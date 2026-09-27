"""Voice verdict presentation through the existing dashboard polling/selection flow."""

import pytest

from test_dashboard_playback_ui import AudioMarkup, HTML, run_browser_logic


DETECTION = r"""
function detection(overrides={}) {return {call_sid:SID,provider:'modulate',status:'complete',
 label:'synthetic',confidence:.97,reason:'confident_synthetic',submitted_audio_ms:25000,
 coverage_limited:false,...overrides};}
function withDetection(result, calls=[session()]) {
 const value=snapshot(calls,[recording()]);
 value.detection={enabled:true,storage_error:'',calls:result?[result]:[]};
 return value;
}
"""


def test_voice_analysis_is_an_accessible_hidden_section_near_the_summary():
    parsed = AudioMarkup()
    parsed.feed(HTML.read_text())
    assert parsed.tags["voice-analysis"] == "section"
    assert "hidden" in parsed.attributes["voice-analysis"]
    assert parsed.attributes["voice-analysis"]["aria-labelledby"] == "voice-analysis-heading"
    assert parsed.attributes["voice-analysis-status"]["role"] == "status"
    assert parsed.attributes["voice-analysis-status"]["aria-atomic"] == "true"
    assert parsed.ids.index("summary-panel") < parsed.ids.index("voice-analysis") < parsed.ids.index("messages")


def test_polling_updates_verdict_without_replacing_transcript_or_reloading_audio(tmp_path):
    run_browser_logic(tmp_path, DETECTION + r"""
state.snapshot=withDetection(detection({status:'analyzing',label:'unknown',confidence:null}));render();openCall();
assert.equal($('voice-analysis').hidden,false);
assert.equal($('voice-analysis-status').textContent,'Analyzing caller audio…');
assert.equal($('voice-analysis-meta').textContent,'Modulate · 25s analyzed');
assert.equal($('voice-analysis-note').hidden,true);
const firstRow=$('messages').children[0], audio=$('call-audio'), loads=audio.loads;
audio.play();audio.currentTime=18;
state.snapshot=withDetection(detection());render();
assert.equal($('voice-analysis-status').textContent,'Synthetic speech detected');
assert.equal($('voice-analysis-status').dataset.tone,'synthetic');
assert.equal($('voice-analysis-meta').textContent,'Modulate · 25s analyzed · Provider verdict confidence: 97%');
assert.equal($('messages').children[0],firstRow);
assert.equal(audio.loads,loads);assert.equal(audio.currentTime,18);assert.equal(audio.paused,false);
state.snapshot=withDetection(detection({label:'non-synthetic',reason:'confident_non_synthetic',confidence:.995}));render();
assert.equal($('voice-analysis-status').textContent,'Non-synthetic speech detected');
assert.equal($('voice-analysis-status').dataset.tone,'neutral');
assert.equal($('voice-analysis-meta').textContent,'Modulate · 25s analyzed · Provider verdict confidence: 100%');
""")


def test_changing_call_clears_previous_result_before_next_poll(tmp_path):
    run_browser_logic(tmp_path, DETECTION + r"""
state.snapshot=withDetection(detection({coverage_limited:true}),[session(),session(OTHER)]);render();openCall();
assert.equal($('voice-analysis').hidden,false);
openCall(OTHER);
assert.equal($('voice-analysis').hidden,true);
for (const id of ['voice-analysis-status','voice-analysis-meta','voice-analysis-note']) assert.equal($(id).textContent,'');
assert.equal($('voice-analysis-note').hidden,true);
openCall();assert.equal($('voice-analysis').hidden,false);
state.snapshot=withDetection(null);render();
assert.equal($('voice-analysis').hidden,true);
assert.equal($('voice-analysis-status').textContent,'');
""")


@pytest.mark.parametrize("source", ["recording", "call_details", "voicemail"])
def test_detection_joins_calls_without_a_transcript(tmp_path, source):
    run_browser_logic(tmp_path, DETECTION + r"""
state.snapshot=snapshot([]);
""" + {
        "recording": "state.snapshot.recordings.recordings=[recording()];",
        "call_details": "state.snapshot.call_details={calls:[{call_sid:SID,ended_at:'2026-09-26T12:02:00Z'}]};",
        "voicemail": "state.snapshot.voicemail.voicemails=[{call_sid:SID,recording_status:'completed'}];",
    }[source] + r"""
state.snapshot.detection={calls:[detection()]};render();openCall();
assert.equal(state.selected,SID);
assert.equal($('voice-analysis').hidden,false);
assert.equal($('voice-analysis-status').textContent,'Synthetic speech detected');
assert.equal($('messages').hidden,true);
""")


def test_session_detection_fallback_and_limited_coverage_are_visible(tmp_path):
    run_browser_logic(tmp_path, DETECTION + r"""
const call=session();call.detection=detection({submitted_audio_ms:120000,coverage_limited:true});
state.snapshot=snapshot([call]);render();openCall();
assert.equal($('voice-analysis-meta').textContent,'Modulate · 2m 0s analyzed · Provider verdict confidence: 97%');
assert.equal($('voice-analysis-note').hidden,false);
assert.equal($('voice-analysis-note').textContent,'Limited coverage: only part of this call was analyzed.');
state.snapshot.detection={calls:[detection({status:'unknown',reason:'provider_timeout',confidence:null})]};render();
assert.equal($('voice-analysis-status').textContent,'Inconclusive');
assert.equal($('voice-analysis-note').textContent,'The voice analysis service timed out.');
assert.equal($('voice-analysis-status').dataset.tone,'neutral');
""")


def test_unknown_verdict_uses_safe_reasons_and_never_shows_confidence(tmp_path):
    run_browser_logic(tmp_path, DETECTION + r"""
for (const [reason,expected] of [
 ['insufficient_evidence','Not enough usable speech for a reliable result.'],
 ['below_confidence_threshold','Provider confidence was too low for a verdict.'],
 ['provider_timeout','The voice analysis service timed out.'],
 ['provider_transport_failed','The voice analysis service could not be reached.'],
 ['<img src=x onerror=alert(1)>','Voice analysis could not be completed.'],
 ['__proto__','Voice analysis could not be completed.'],
 [{toString:null},'Voice analysis could not be completed.']
]) {
 state.snapshot=withDetection(detection({status:'unknown',label:'synthetic',reason,confidence:.99}));render();openCall();
 assert.equal($('voice-analysis-status').textContent,'Inconclusive');
 assert.equal($('voice-analysis-meta').textContent,'Modulate · 25s analyzed');
 assert.equal($('voice-analysis-note').textContent,expected);
}
""")


def test_invalid_payloads_never_become_valid_verdicts_or_html(tmp_path):
    run_browser_logic(tmp_path, DETECTION + r"""
for (const id of ['voice-analysis-status','voice-analysis-meta','voice-analysis-note']) {
 Object.defineProperty($(id),'innerHTML',{set(){throw new Error('Analysis must only render text');}});
}
for (const value of [null,[],{}, {provider:'<img src=x onerror=alert(1)>'}]) {
 const call=session();call.detection=value;state.snapshot=snapshot([call]);render();openCall();
 assert.equal($('voice-analysis').hidden,true);
}
for (const confidence of [null,'0.98',-1,2,Infinity,NaN]) {
 state.snapshot=withDetection(detection({confidence,submitted_audio_ms:-100}));render();openCall();
 assert.equal($('voice-analysis-meta').textContent,'Modulate');
}
for (const overrides of [{status:'<img>',label:'synthetic'}, {status:'complete',label:'human'},
 {status:'complete',label:'<script>alert(1)</script>'}]) {
 state.snapshot=withDetection(detection(overrides));render();
 assert.equal($('voice-analysis-status').textContent,'Inconclusive');
 assert.equal($('voice-analysis-meta').textContent,'Modulate · 25s analyzed');
}
""")


def test_storage_failure_is_visible_without_results_and_clears_after_recovery(tmp_path):
    run_browser_logic(tmp_path, DETECTION + r"""
state.snapshot=withDetection(null);
state.snapshot.detection.storage_error='<img src=x onerror=alert(1)>';
render();openCall();
assert.equal($('detection-warning').hidden,false);
assert.equal($('detection-warning').textContent,'Voice analysis storage is unavailable. Results may be missing or unsaved.');
assert.equal($('voice-analysis').hidden,true);
state.snapshot=withDetection(detection());render();
assert.equal($('detection-warning').hidden,true);
assert.equal($('detection-warning').textContent,'');
assert.equal($('voice-analysis').hidden,false);
""")
