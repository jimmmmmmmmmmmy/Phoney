"""Positive caller alerts through the real dashboard polling and navigation flow."""

from html.parser import HTMLParser

import pytest

from test_dashboard_playback_ui import AudioMarkup, HTML, run_browser_logic


DETECTION = r"""
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
assert.equal($('caller-ai-badge').textContent,'AI Caller');
assert.equal($('caller-ai-badge').dataset.tone,'ai');
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
 showPage('calls');assert.equal($('caller-ai-badge').textContent,'AI Caller');
}
showCollection('recent');assertBadgeCleared();
openCall();assert.equal($('caller-ai-badge').textContent,'AI Caller');
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
assert.equal($('caller-ai-badge').textContent,'AI Caller');
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
assert.equal(badge.textContent,'AI Caller');
assert.equal(badge.attributes.title,`AI Caller. ${description}. Provisional result from partial call analysis.`);
assert.equal(badge.attributes['aria-label'],badge.attributes.title);
state.snapshot=withDetection(detection({analysis:analysis({source})}));render();
assert.equal(badge.attributes.title,`AI Caller. ${description}. ${source==='live'
 ? 'Provisional result; recording analysis pending.' : 'Completed analysis.'}`);
assert.equal(badge.attributes['aria-label'],badge.attributes.title);
""")


def test_completed_recording_potential_alert_does_not_inherit_unknown_status_as_partial(tmp_path):
    run_browser_logic(tmp_path, DETECTION + r"""
state.snapshot=withDetection(detection({status:'unknown',label:'unknown',confidence:null,
 analysis:analysis({version:2,alert:'potential_ai',source:'recording',complete:true})}));render();openCall();
const badge=$('caller-ai-badge');
assert.equal(badge.hidden,false);
assert.equal(badge.textContent,'Potentially AI');
assert.equal(badge.attributes.title,'Potentially AI. Recorded caller audio. Completed analysis.');
assert.equal(badge.attributes['aria-label'],badge.attributes.title);
""")


def test_fully_covered_completed_live_result_remains_provisional_until_recording_analysis(tmp_path):
    run_browser_logic(tmp_path, DETECTION + r"""
state.snapshot=withDetection(detection({status:'complete',coverage_limited:false,
 analysis:analysis({version:2,source:'live',complete:true})}));render();openCall();
const badge=$('caller-ai-badge');
assert.equal(badge.hidden,false);
assert.equal(badge.textContent,'AI Caller');
assert.equal(badge.attributes.title,'AI Caller. Live caller audio. Provisional result; recording analysis pending.');
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
 {analysis:analysis({version:3})}, {analysis:analysis({version:'2'})},
 {analysis:analysis({complete:'true'})}, {analysis:analysis({source:'<img>'})}]) {
 state.snapshot=withDetection(detection(overrides));render();openCall();assertBadgeCleared();
}
""")


@pytest.mark.parametrize("version", [1, 2])
def test_positive_labels_use_backend_alert_without_exposing_numbers(tmp_path, version):
    run_browser_logic(tmp_path, DETECTION + f"const version={version};\n" + r"""
// Contradictory counters prove that the browser does not reclassify the alert
// from confidence, speech durations, or the provider's overlapping windows.
for (const [alert,label,tone] of [['ai_caller','AI Caller','ai'],['potential_ai','Potentially AI','potential']]) {
 state.snapshot=withDetection(detection({label:'unknown',confidence:.999,
  analysis:analysis({version,alert,synthetic_share:0,synthetic_ms:1,non_synthetic_ms:999999})}));render();openCall();
 const badge=$('caller-ai-badge');
 assert.equal(badge.hidden,false);assert.equal(badge.textContent,label);
 assert.equal(badge.dataset.tone,tone);
 assert.ok(!/[0-9%]/.test(badge.textContent+badge.attributes.title+badge.attributes['aria-label']));
}
""")


@pytest.mark.parametrize("status", ["analyzing", "unknown"])
def test_partial_positive_alert_has_provisional_accessibility_only(tmp_path, status):
    run_browser_logic(tmp_path, DETECTION + f"const providerStatus={status!r};\n" + r"""
state.snapshot=withDetection(detection({status:providerStatus,label:'unknown',confidence:null,
 coverage_limited:true,analysis:analysis({alert:'potential_ai',complete:false})}));render();openCall();
const badge=$('caller-ai-badge');
assert.equal(badge.textContent,'Potentially AI');
assert.equal(badge.dataset.tone,'potential');
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
assert.equal($('caller-ai-badge').textContent,'AI Caller');
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
