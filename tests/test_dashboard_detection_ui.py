"""Positive caller alerts through the real dashboard polling and navigation flow."""

from html.parser import HTMLParser

import pytest

from test_dashboard_playback_ui import AudioMarkup, HTML, run_browser_logic


DETECTION = r"""
document.createElementNS = (_namespace,tag) => new Element(tag);
function analysis(overrides={}) {return {version:1,track:'inbound',source:'live',complete:true,
 alert:'ai_caller',synthetic_share:.8,analyzed_ms:25000,windows:[],...overrides};}
function detection(overrides={}) {return {call_sid:SID,provider:'modulate',status:'complete',
 label:'synthetic',confidence:.97,reason:'confident_synthetic',submitted_audio_ms:25000,
 coverage_limited:false,analysis:analysis(),...overrides};}
function withDetection(result, calls=[session()]) {
 const value=snapshot(calls,[recording()]);
 value.detection={enabled:true,storage_error:'',calls:result?[result]:[]};
 return value;
}
function assertBadgeCleared() {
 const badge=$('caller-ai-badge');
 assert.equal(badge.hidden,true);
 assert.equal(badge.textContent,'');
 assert.equal(badge.attributes.title,undefined);
 assert.equal(badge.attributes['aria-label'],undefined);
 assert.equal(badge.dataset.tone,undefined);
}
"""


def test_only_compact_badge_sits_beside_title_within_page_heading():
    class Parents(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack = []
            self.parents = {}

        def handle_starttag(self, tag, attributes):
            attributes = dict(attributes)
            if "id" in attributes:
                self.parents[attributes["id"]] = list(self.stack)
            if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
                self.stack.append((tag, attributes))

        def handle_endtag(self, tag):
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index][0] == tag:
                    del self.stack[index:]
                    break

    parsed, parents = AudioMarkup(), Parents()
    html = HTML.read_text()
    parsed.feed(html)
    parents.feed(html)
    assert parsed.tags["caller-ai-badge"] == "span"
    assert "hidden" in parsed.attributes["caller-ai-badge"]
    assert parsed.attributes["caller-ai-badge"]["role"] == "status"
    assert parsed.attributes["caller-ai-badge"]["aria-atomic"] == "true"
    assert parents.parents["caller-ai-badge"] == parents.parents["page-title"]
    assert parents.parents["caller-ai-badge"][-1][1]["class"] == "page-heading-title"
    assert any(attrs.get("class") == "page-heading" for _, attrs in parents.parents["caller-ai-badge"])
    assert parsed.ids.index("page-title") < parsed.ids.index("caller-ai-badge") < parsed.ids.index("call-exports")
    assert not {"voice-analysis", "voice-analysis-heading", "voice-analysis-status", "voice-analysis-meta", "voice-analysis-note"} & set(parsed.ids)
    assert "Caller analysis</" not in html


def test_polling_updates_badge_without_replacing_transcript_or_reloading_audio(tmp_path):
    run_browser_logic(tmp_path, DETECTION + r"""
state.snapshot=withDetection(detection({status:'analyzing',label:'unknown',confidence:null,
 analysis:analysis({alert:'inconclusive',complete:false})}));render();openCall();
assertBadgeCleared();
const firstRow=$('messages').children[0], audio=$('call-audio'), loads=audio.loads;
audio.play();audio.currentTime=18;
state.snapshot=withDetection(detection());render();
assert.equal($('caller-ai-badge').hidden,false);
assert.equal($('caller-ai-badge').textContent,'AI Detected');
assert.equal($('caller-ai-badge').dataset.tone,undefined);
assert.equal($('messages').children[0],firstRow);
assert.equal(audio.loads,loads);assert.equal(audio.currentTime,18);assert.equal(audio.paused,false);
state.snapshot=withDetection(detection({label:'non-synthetic',reason:'confident_non_synthetic',confidence:.995,
 analysis:analysis({alert:'none'})}));render();
assertBadgeCleared();
assert.equal($('messages').children[0],firstRow);
assert.equal(audio.loads,loads);assert.equal(audio.currentTime,18);assert.equal(audio.paused,false);
""")


def test_changing_call_clears_previous_result_before_next_poll(tmp_path):
    run_browser_logic(tmp_path, DETECTION + r"""
state.snapshot=withDetection(detection({coverage_limited:true}),[session(),session(OTHER)]);render();openCall();
assert.equal($('caller-ai-badge').hidden,false);
openCall(OTHER);assertBadgeCleared();
openCall();assert.equal($('caller-ai-badge').hidden,false);
state.snapshot=withDetection(null);render();assertBadgeCleared();
// A result attached to the wrong session must never label the selected caller.
const call=session();call.detection=detection({call_sid:OTHER});
state.snapshot=snapshot([call]);render();assertBadgeCleared();
""")


def test_badge_hides_on_other_routes_without_interrupting_audio(tmp_path):
    run_browser_logic(tmp_path, DETECTION + r"""
state.snapshot=withDetection(detection());render();
assertBadgeCleared(); // A background selection is not an open call.
openCall();
const audio=$('call-audio');audio.play();audio.currentTime=19;
const loads=audio.loads,src=audio.src;
for (const page of ['contacts','agents','team']) {
 showPage(page);assertBadgeCleared();
 assert.equal(audio.src,src);assert.equal(audio.loads,loads);
 assert.equal(audio.currentTime,19);assert.equal(audio.paused,false);
 showPage('calls');assert.equal($('caller-ai-badge').textContent,'AI Detected');
}
showCollection('recent');assertBadgeCleared();
openCall();assert.equal($('caller-ai-badge').textContent,'AI Detected');
showCollection('voicemail');assertBadgeCleared();
assert.equal(audio.src,src);assert.equal(audio.loads,loads);assert.equal(audio.paused,false);
state.selected=null;state.selectedSession=null;state.detail=true;renderNavigation();assertBadgeCleared();
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
assert.equal($('caller-ai-badge').hidden,false);
assert.equal($('caller-ai-badge').textContent,'AI Detected');
assert.equal($('messages').hidden,true);
""")


@pytest.mark.parametrize("source,description", [
    ("live", "Live caller audio"),
    ("recording", "Recorded caller audio"),
    ("combined", "Live and recorded caller audio"),
])
def test_source_and_limited_coverage_are_accessible_without_visible_copy(tmp_path, source, description):
    run_browser_logic(tmp_path, DETECTION + f"const source={source!r},description={description!r};\n" + r"""
const call=session();call.detection=detection({submitted_audio_ms:120000,coverage_limited:true,
 analysis:analysis({source,complete:false})});
state.snapshot=snapshot([call]);render();openCall();
const badge=$('caller-ai-badge');
assert.equal(badge.textContent,'AI Detected');
assert.equal(badge.attributes.title,`AI Detected. ${description}. Provisional result from partial call analysis.`);
assert.equal(badge.attributes['aria-label'],badge.attributes.title);
state.snapshot=withDetection(detection({analysis:analysis({source})}));render();
assert.equal(badge.attributes.title,`AI Detected. ${description}. ${source==='live'
 ? 'Provisional result; recording analysis pending.' : 'Completed analysis.'}`);
assert.equal(badge.attributes['aria-label'],badge.attributes.title);
""")


def test_completed_recording_potential_alert_does_not_inherit_unknown_status_as_partial(tmp_path):
    run_browser_logic(tmp_path, DETECTION + r"""
state.snapshot=withDetection(detection({status:'unknown',label:'unknown',confidence:null,
 analysis:analysis({version:2,alert:'potential_ai',source:'recording',complete:true})}));render();openCall();
const badge=$('caller-ai-badge');
assert.equal(badge.hidden,false);
assert.equal(badge.textContent,'AI Detected');
assert.equal(badge.attributes.title,'AI Detected. Recorded caller audio. Completed analysis.');
assert.equal(badge.attributes['aria-label'],badge.attributes.title);
""")


def test_fully_covered_completed_live_result_remains_provisional_until_recording_analysis(tmp_path):
    run_browser_logic(tmp_path, DETECTION + r"""
state.snapshot=withDetection(detection({status:'complete',coverage_limited:false,
 analysis:analysis({version:2,source:'live',complete:true})}));render();openCall();
const badge=$('caller-ai-badge');
assert.equal(badge.hidden,false);
assert.equal(badge.textContent,'AI Detected');
assert.equal(badge.attributes.title,'AI Detected. Live caller audio. Provisional result; recording analysis pending.');
assert.equal(badge.attributes['aria-label'],badge.attributes.title);
""")


def test_neutral_unknown_and_unfinished_results_hide_badge(tmp_path):
    run_browser_logic(tmp_path, DETECTION + r"""
for (const status of ['complete','unknown','analyzing']) {
 for (const alert of ['none','inconclusive']) {
  state.snapshot=withDetection(detection({status,label:'synthetic',reason:'provider_timeout',confidence:.99,
   analysis:analysis({alert,complete:status==='complete'})}));render();openCall();
  assertBadgeCleared();
 }
}
""")


def test_invalid_payloads_never_become_valid_badges_or_html(tmp_path):
    run_browser_logic(tmp_path, DETECTION + r"""
Object.defineProperty($('caller-ai-badge'),'innerHTML',{set(){throw new Error('Badge must only render text');}});
for (const value of [null,[],{}, {provider:'<img src=x onerror=alert(1)>'}]) {
 const call=session();call.detection=value;state.snapshot=snapshot([call]);render();openCall();assertBadgeCleared();
}
for (const overrides of [{status:'<img>',label:'synthetic'}, {analysis:null}, {analysis:[]},
 {analysis:analysis({track:'outbound'})}, {analysis:analysis({alert:'<script>alert(1)</script>'})},
 {analysis:analysis({version:4})}, {analysis:analysis({version:'2'})},
 {analysis:analysis({complete:'true'})}, {analysis:analysis({source:'<img>'})}]) {
 state.snapshot=withDetection(detection(overrides));render();openCall();assertBadgeCleared();
}
""")


@pytest.mark.parametrize("version", [1, 2, 3])
def test_positive_labels_use_backend_alert_without_exposing_numbers(tmp_path, version):
    run_browser_logic(tmp_path, DETECTION + f"const version={version};\n" + r"""
// Contradictory counters prove that the browser does not reclassify the alert
// from confidence, speech durations, or the provider's overlapping windows.
for (const alert of (version===3 ? ['ai_detected'] : ['ai_caller','potential_ai'])) {
 state.snapshot=withDetection(detection({label:'unknown',confidence:.999,
  analysis:analysis({version,alert,synthetic_share:0,synthetic_ms:1,non_synthetic_ms:999999})}));render();openCall();
 const badge=$('caller-ai-badge');
 assert.equal(badge.hidden,false);assert.equal(badge.textContent,'AI Detected');
 assert.equal(badge.dataset.tone,undefined);
 assert.ok(!/[0-9%]/.test(badge.textContent+badge.attributes.title+badge.attributes['aria-label']));
}
""")


@pytest.mark.parametrize("status", ["analyzing", "unknown"])
def test_partial_positive_alert_has_provisional_accessibility_only(tmp_path, status):
    run_browser_logic(tmp_path, DETECTION + f"const providerStatus={status!r};\n" + r"""
state.snapshot=withDetection(detection({status:providerStatus,label:'unknown',confidence:null,
 coverage_limited:true,analysis:analysis({alert:'potential_ai',complete:false})}));render();openCall();
const badge=$('caller-ai-badge');
assert.equal(badge.textContent,'AI Detected');
assert.equal(badge.dataset.tone,undefined);
assert.ok(badge.attributes.title.includes(providerStatus==='analyzing'
 ? 'Provisional analysis in progress.' : 'Provisional result from partial call analysis.'));
assert.equal(badge.attributes['aria-label'],badge.attributes.title);
""")


def test_legacy_verdict_does_not_claim_new_caller_thresholds(tmp_path):
    run_browser_logic(tmp_path, DETECTION + r"""
for (const label of ['synthetic','non-synthetic']) {
 const previous=detection({label,confidence:1});delete previous.analysis;
 state.snapshot=withDetection(previous);render();openCall();assertBadgeCleared();
}
""")


def test_transcription_confidence_is_explicitly_distinct_from_caller_analysis(tmp_path):
    run_browser_logic(tmp_path, DETECTION + r"""
state.snapshot=withDetection(detection());render();openCall();
const confidence=$('messages').children[0].children[1].children[0].children[1];
assert.equal(confidence.textContent,'90% confidence');
assert.equal(confidence.attributes['aria-label'],'Transcription confidence: 90%');
assert.equal(confidence.attributes.title,'Transcription confidence');
assert.equal($('caller-ai-badge').textContent,'AI Detected');
""")


def test_storage_failure_is_visible_without_results_and_clears_after_recovery(tmp_path):
    run_browser_logic(tmp_path, DETECTION + r"""
state.snapshot=withDetection(null);
state.snapshot.detection.storage_error='<img src=x onerror=alert(1)>';
render();openCall();
assert.equal($('detection-warning').hidden,false);
assert.equal($('detection-warning').textContent,'Voice analysis storage is unavailable. Results may be missing or unsaved.');
assertBadgeCleared();
state.snapshot=withDetection(detection());render();
assert.equal($('detection-warning').hidden,true);
assert.equal($('detection-warning').textContent,'');
assert.equal($('caller-ai-badge').hidden,false);
""")


TIMED_EVIDENCE = r"""
const STREAM='MZ'+'c'.repeat(32), OTHER_STREAM='MZ'+'d'.repeat(32);
function timedCall(segments,overrides={}) {
 return {...session(),stream_sid:STREAM,segments,...overrides};
}
function segment(overrides={}) {
 return {id:'caller',track:'inbound',start_ms:9230,end_ms:17520,text:'Caller paragraph',confidence:.99,...overrides};
}
function timedDetection(intervals=[{stream_id:STREAM,start_ms:16109,end_ms:20109}],overrides={}) {
 return detection({analysis:analysis({version:3,alert:'ai_detected',source:'recording',synthetic_intervals:intervals,...overrides})});
}
function marker(row) {return row.children[1].children[0].children.find(child=>child.className==='segment-ai-marker');}
function flagged(row) {return row.classList.contains('ai-detected');}
"""


def test_only_finalized_caller_audio_is_highlighted_with_accessible_robot(tmp_path):
    run_browser_logic(tmp_path, DETECTION + TIMED_EVIDENCE + r"""
const call=timedCall([
 segment(),
 segment({id:'college',track:'outbound',text:'College paragraph'}),
 segment({id:'weak-transcription',text:'Quiet caller',confidence:.01}),
 segment({id:'outside',start_ms:21000,end_ms:24000,confidence:1}),
 segment({id:'unknown-track',track:'unrecognized'})
],{tracks:{inbound:{interim:segment({text:'Still transcribing'})}}});
state.snapshot=withDetection(timedDetection(),[call]);render();openCall();
const rows=$('messages').children;
const caller=rows.find(row=>row.dataset.segmentId==='caller');
const college=rows.find(row=>row.dataset.segmentId==='college');
const quiet=rows.find(row=>row.dataset.segmentId==='weak-transcription');
assert.equal(flagged(caller),true);assert.equal(flagged(quiet),true);
assert.equal(flagged(college),false);assert.equal(marker(college),undefined);
assert.equal(flagged(rows.find(row=>row.dataset.segmentId==='outside')),false);
assert.equal(flagged(rows.find(row=>row.dataset.segmentId==='unknown-track')),false);
assert.equal(flagged(rows.at(-1)),false);assert.equal(marker(rows.at(-1)),undefined);
assert.equal(caller.children[1].children[1].textContent,'Caller paragraph');
const robot=marker(caller);
assert.equal(robot.hidden,false);assert.equal(robot.attributes.role,'img');
assert.equal(robot.attributes['aria-label'],'AI detected in audio overlapping this transcript segment.');
assert.equal(robot.attributes.title,robot.attributes['aria-label']);
assert.equal(robot.children[0].tag,'svg');assert.equal(robot.children[0].attributes['aria-hidden'],'true');
assert.equal(robot.children[0].children[0].tag,'rect');assert.equal(robot.children[0].children[1].tag,'path');
assert.equal(caller.children[1].children[0].children[1].attributes.title,'Transcription confidence');
assert.equal($('caller-ai-badge').textContent,'AI Detected');
""")


@pytest.mark.parametrize("start,end,expected", [
    (9000, 10000, False),
    (12000, 13000, False),
    (9999, 10001, True),
    (11999, 13000, True),
    (10000, 12000, True),
    (11000, 11000, False),
    (12000, 10000, False),
    (-1000, 12000, False),
    (10000.5, 12000, False),
])
def test_audio_overlap_is_half_open_and_requires_valid_segment_timing(tmp_path, start, end, expected):
    run_browser_logic(tmp_path, DETECTION + TIMED_EVIDENCE
        + f"const start={start},end={end},expected={'true' if expected else 'false'};\n" + r"""
state.snapshot=withDetection(timedDetection([{stream_id:STREAM,start_ms:10000,end_ms:12000}]),
 [timedCall([segment({start_ms:start,end_ms:end})])]);render();openCall();
assert.equal(flagged($('messages').children[0]),expected);
assert.equal(Boolean(marker($('messages').children[0])),expected);
""")


def test_evidence_polling_updates_existing_rows_and_coexists_with_playback(tmp_path):
    run_browser_logic(tmp_path, DETECTION + TIMED_EVIDENCE + r"""
const call=timedCall([segment()]);
state.snapshot=withDetection(timedDetection([],{alert:'none'}),[call]);render();openCall();
const row=$('messages').children[0], paragraph=row.children[1].children[1], audio=$('call-audio');
const viewport=$('transcript');viewport.scrollTop=123;
audio.play();audio.currentTime=10;updatePlaybackHighlight();
const loads=audio.loads,src=audio.src;
assert.equal(flagged(row),false);assert.equal(row.classList.contains('playing-line'),true);
state.snapshot=withDetection(timedDetection(),[call]);render();
assert.equal($('messages').children[0],row);assert.equal(row.children[1].children[1],paragraph);
assert.equal(flagged(row),true);assert.equal(row.classList.contains('playing-line'),true);
assert.equal(viewport.scrollTop,123);assert.equal(audio.loads,loads);assert.equal(audio.src,src);
assert.equal(audio.currentTime,10);assert.equal(audio.paused,false);
const robot=marker(row);const metaCount=row.children[1].children[0].children.length;
render();assert.equal(marker(row),robot);assert.equal(row.children[1].children[0].children.length,metaCount);
showPage('contacts');assert.equal(flagged(row),false);assert.equal(robot.hidden,true);
showPage('calls');assert.equal(flagged(row),true);assert.equal(robot.hidden,false);
state.snapshot=withDetection(timedDetection([],{alert:'none'}),[call]);render();
assert.equal($('messages').children[0],row);assert.equal(flagged(row),false);assert.equal(robot.hidden,true);
assert.equal(row.classList.contains('playing-line'),true);
assert.equal(viewport.scrollTop,123);assert.equal(audio.loads,loads);assert.equal(audio.paused,false);
state.snapshot=withDetection(timedDetection(),[call]);render();
assert.equal(marker(row),robot);assert.equal(robot.hidden,false);
state.snapshot=withDetection(null,[call]);render();assert.equal(flagged(row),false);assert.equal(robot.hidden,true);
""")


def test_missing_malformed_or_unaligned_evidence_never_highlights(tmp_path):
    run_browser_logic(tmp_path, DETECTION + TIMED_EVIDENCE + r"""
const call=timedCall([segment()]);
for (const intervals of [undefined,null,{},[],
 [{stream_id:STREAM,start_ms:'16109',end_ms:20109}],
 [{stream_id:STREAM,start_ms:NaN,end_ms:20109}],
 [{stream_id:STREAM,start_ms:16109,end_ms:Infinity}],
 [{stream_id:STREAM,start_ms:16109,end_ms:16109}],
 [{stream_id:STREAM,start_ms:-1,end_ms:20109}],
 [{stream_id:STREAM,start_ms:16109,end_ms:3600001}],
 [{start_ms:16109,end_ms:20109}],
 [{stream_id:'<img>',start_ms:16109,end_ms:20109}],
 [{stream_id:OTHER_STREAM,start_ms:16109,end_ms:20109}],
 [{stream_id:STREAM,start_ms:16109,end_ms:20109},null]
]) {
 state.snapshot=withDetection(timedDetection([],{synthetic_intervals:intervals}),[call]);render();openCall();
 assert.equal(flagged($('messages').children[0]),false);
 assert.ok(!marker($('messages').children[0]) || marker($('messages').children[0]).hidden);
}
for (const stream_sid of [undefined,null,'',STREAM+'x',OTHER_STREAM]) {
 state.snapshot=withDetection(timedDetection(),[timedCall([segment()],{stream_sid})]);render();openCall();
 assert.equal(flagged($('messages').children[0]),false);
}
for (const overrides of [{alert:'none'},{alert:'inconclusive'},{track:'outbound'},
 {version:1,alert:'ai_caller'},{version:2,alert:'potential_ai'}]) {
 state.snapshot=withDetection(timedDetection(undefined,overrides),[call]);render();openCall();
 assert.equal(flagged($('messages').children[0]),false);
}
""")


def test_call_switch_does_not_reuse_other_calls_evidence_or_marker(tmp_path):
    run_browser_logic(tmp_path, DETECTION + TIMED_EVIDENCE + r"""
const first=timedCall([segment()]);
const second=timedCall([segment()],{call_sid:OTHER,stream_sid:OTHER_STREAM});
state.snapshot=withDetection(timedDetection(),[first,second]);render();openCall();
const original=$('messages').children[0];assert.equal(flagged(original),true);
openCall(OTHER);
const other=$('messages').children[0];assert.notEqual(other,original);
assert.equal(flagged(other),false);assert.equal(marker(other),undefined);assertBadgeCleared();
assert.equal($('call-audio').src,'');assert.equal($('call-audio').paused,true);
openCall();assert.equal(flagged($('messages').children[0]),true);
// Stream identity changes alone must invalidate cached row highlighting.
state.snapshot=withDetection(timedDetection(),[timedCall([segment()],{stream_sid:OTHER_STREAM})]);render();
assert.equal(flagged($('messages').children[0]),false);
""")
