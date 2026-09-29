"""Saved-call pagination, independent archived selection, and caller history UI."""

from test_dashboard_playback_ui import run_browser_logic
from test_dashboard_crm_ui import run_crm


def test_load_older_calls_keeps_playback_scroll_metadata_and_loaded_rows_during_poll(tmp_path):
    run_browser_logic(tmp_path, r'''
(async()=>{
const older='CA'+'c'.repeat(32), phone='+16562520233';
const first=snapshot([session()],[recording()]);
first.history={has_more:true,next_cursor:'page-one',total:25,duration_seconds:3000};
first.call_details={calls:[{call_sid:SID,caller_number:phone,duration_seconds:120}]};
state.snapshot=first;render();openCall();
const audio=$('call-audio');audio.play();audio.currentTime=41;backToCalls();
$('collection-scroller').scrollTop=170;
const loads=audio.loads,pauses=audio.pauses;
const page=snapshot([{...session(older),started_at:'2026-09-24T12:00:00Z'}],[recording(older)]);
page.call_details={calls:[{call_sid:older,caller_number:phone,duration_seconds:90,brief_summary:{text:'Older conversation'}}]};
page.history={has_more:false,next_cursor:null,total:25,duration_seconds:3000};
let requests=0;
fetch=async(url,options)=>{requests++;assert.match(url,/cursor=page-one/);assert.equal(options.credentials,'same-origin');return {ok:true,json:async()=>page};};
$('history-load-more').focus();await loadOlderCalls();
assert.equal(requests,1);assert.equal(state.sessions.length,2);
assert.equal($('collection-scroller').scrollTop,170);
assert.equal(audio.currentTime,41);assert.equal(audio.loads,loads);assert.equal(audio.pauses,pauses);assert.equal(audio.paused,false);
assert.equal($('history-controls').hidden,true);
assert.equal(document.activeElement.dataset.callSid,older);
assert.equal(state.sessions.find(item=>item.call_sid===older).call_detail.caller_number,phone);
state.snapshot=snapshot([session()],[recording()]);state.snapshot.history=first.history;render();
assert.equal(state.sessions.length,2,'First-page polls must not discard an explicitly loaded older page');
assert.equal($('history-controls').hidden,true,'Polling must not reset the completed pagination cursor');
openCall(older);assert.equal(state.selected,older);assert.match($('call-summary').textContent,/No summary/);
const detailed=snapshot([{...session(older),segments:[{...session().segments[0],text:'Archived transcript.'}]}],[recording(older)]);
detailed.call_details=page.call_details;detailed.history=first.history;
state.snapshot=detailed;render();
assert.equal($('messages').children[0].children[1].children[1].textContent,'Archived transcript.');
assert.equal($('export-txt').getAttribute('aria-disabled'),'false');
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


def test_history_failure_is_retryable_and_pending_clicks_do_not_duplicate_requests(tmp_path):
    run_browser_logic(tmp_path, r'''
(async()=>{
state.snapshot=snapshot([session()]);state.snapshot.history={has_more:true,next_cursor:'older'};render();
let release,requests=0;
fetch=async()=>{requests++;await new Promise(resolve=>{release=resolve;});return {ok:false};};
const pending=loadOlderCalls();await Promise.resolve();
assert.equal($('history-load-more').disabled,true);
await loadOlderCalls();assert.equal(requests,1);
release();await pending;
assert.match($('history-status').textContent,/could not be loaded/);
assert.equal($('history-load-more').disabled,false);
assert.equal(state.sessions.length,1);
fetch=async()=>({ok:true,json:async()=>({...snapshot([session(OTHER)]),history:{has_more:false,next_cursor:null}})});
await loadOlderCalls();assert.equal(state.sessions.length,2);assert.equal($('history-status').textContent,'');
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


def test_archived_call_can_open_by_id_without_being_in_recent_list(tmp_path):
    run_browser_logic(tmp_path, r'''
(async()=>{
state.snapshot=snapshot([session()]);render();
let requested='';
fetch=async url=>{requested=url;return {ok:true,json:async()=>snapshot([session(OTHER)])};};
window.DashboardCalls.openCall(OTHER);
assert.equal(state.selected,OTHER);assert.equal(state.detail,true);assert.equal(state.page,'calls');
assert.match($('empty-title').textContent,/Loading call/);
await new Promise(resolve=>setImmediate(resolve));
assert.match(requested,new RegExp('call_sid='+OTHER));
assert.equal(state.selectedSession.call_sid,OTHER);
assert.equal($('messages').children.length,1);
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


def test_caller_pagination_and_selected_call_queries_are_encoded_independently(tmp_path):
    run_browser_logic(tmp_path, r'''
(async()=>{
state.callerFilter='+16562520233';state.selected=SID;
let requested='';
fetch=async url=>{requested=url;return {ok:true,json:async()=>({...snapshot([session()]),history:{has_more:true,next_cursor:'next'}})};};
await poll();
const query=new URL('https://local'+requested).searchParams;
assert.equal(query.get('caller'),'+16562520233');assert.equal(query.get('call_sid'),SID);
await window.DashboardCalls.fetchHistory({caller:'+16562520233',cursor:'opaque+cursor/='});
const historyQuery=new URL('https://local'+requested).searchParams;
assert.equal(historyQuery.get('caller'),'+16562520233');assert.equal(historyQuery.get('cursor'),'opaque+cursor/=');
assert.equal(historyQuery.has('call_sid'),false);
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


def test_contact_history_loads_caller_pages_and_uses_complete_server_metrics():
    run_crm(r'''
const call=(sid,started,duration)=>({call_sid:sid,status:'completed',ended_at:started,started_at:started,
 call_detail:{caller_number:'+1 (656) 252-0233',started_at:started,duration_seconds:duration,
 summary:{text:'Caller’s archived request was confirmed with Caller.',source:'gemini'}}});
const first='CA'+'a'.repeat(32),older='CA'+'b'.repeat(32);
const requests=[],saved=[];
window.DashboardCalls={fetchHistory:async options=>{
 requests.push(options);
 const session=call(options.cursor?older:first,options.cursor?'2026-08-01T12:00:00Z':'2026-09-01T12:00:00Z',60);saved.push(session);
 return {sessions:[session],
 history:{has_more:!options.cursor,next_cursor:options.cursor?null:'page-two',total:22,duration_seconds:660,last_contact_at:'2026-09-01T12:00:00Z'}};
}};
navigate('#contacts/'+CONTACT.id);await new Promise(resolve=>setImmediate(resolve));
assert.deepEqual(requests,[{caller:CONTACT.phone,cursor:''}]);
assert.match(text(contactRoot),/Conversations 22/);assert.match(text(contactRoot),/Talk time 11m 0s/);
assert.match(text(contactRoot),/1 conversation loaded of 22/);
assert.ok($('crm-load-history'));
$('crm-load-history').focus();$('crm-load-history').click();await new Promise(resolve=>setImmediate(resolve));
assert.deepEqual(requests[1],{caller:CONTACT.phone,cursor:'page-two'});
assert.equal(contactRoot.all().filter(item=>hasClass(item,'crm-call')).length,2);
assert.equal($('crm-load-history'),null);assert.equal(document.activeElement,$('crm-profile-title'));
window.DashboardCRM.setSessions([]);window.DashboardCRM.render();
assert.equal(contactRoot.all().filter(item=>hasClass(item,'crm-call')).length,2,'Current session polling must retain caller archive pages');
const summaries=()=>contactRoot.all().filter(item=>hasClass(item,'crm-call-summary'));
assert.deepEqual(summaries().map(item=>item.textContent),Array(2).fill('Avery Chen’s archived request was confirmed with Avery Chen.'));
storeContacts([{...CONTACT,firstName:'$& <img src=x>',lastName:'Renamed'}]);changeStorage();
assert.deepEqual(summaries().map(item=>item.textContent),Array(2).fill('$& <img src=x> Renamed’s archived request was confirmed with $& <img src=x> Renamed.'));
assert.ok(summaries().every(item=>item.children.length===0));
assert.equal(requests.length,2,'Renaming must render cached history without fetching or regenerating summaries');
assert.ok(saved.every(item=>item.call_detail.summary.text==='Caller’s archived request was confirmed with Caller.'));
let selected;
window.DashboardCalls.openCall=id=>{selected=id;};
const link=contactRoot.all().find(item=>item.tagName==='A'&&item.href==='#calls/recent/'+older);
link.dispatch('click',{button:0});assert.equal(selected,older);
''', before='storeContacts([CONTACT]);')


def test_contact_history_failure_preserves_existing_calls_and_retry_cursor():
    run_crm(r'''
let fail=true,attempts=0;
window.DashboardCalls={fetchHistory:async()=>{attempts++;if(fail)throw Error('Call history could not be loaded. Try again.');
 return {sessions:[],history:{has_more:false,next_cursor:null,total:0,duration_seconds:0}};}};
navigate('#contacts/'+CONTACT.id);await new Promise(resolve=>setImmediate(resolve));
assert.match(text(contactRoot),/Call history could not be loaded/);
assert.equal(attempts,1,'Rendering an error must not automatically retry in a loop');
fail=false;$('crm-load-history').click();await new Promise(resolve=>setImmediate(resolve));
assert.equal(attempts,2);assert.doesNotMatch(text(contactRoot),/could not be loaded/);
assert.match(text(contactRoot),/Conversations 0/);
''', before='storeContacts([CONTACT]);')


def test_delayed_history_page_does_not_change_route_or_steal_focus(tmp_path):
    run_browser_logic(tmp_path, r'''
(async()=>{
state.snapshot=snapshot([session()]);state.snapshot.history={has_more:true,next_cursor:'older'};render();
let release;fetch=async()=>({ok:true,json:()=>new Promise(resolve=>{release=resolve;})});
const pending=loadOlderCalls();await Promise.resolve();await Promise.resolve();
showPage('contacts');const focused=document.activeElement;
release({...snapshot([session(OTHER)]),history:{has_more:false,next_cursor:null}});await pending;
assert.equal(state.page,'contacts');assert.equal(window.location.hash,'#contacts');
assert.equal(document.activeElement,focused);assert.equal(state.sessions.length,2);
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


def test_missing_archived_selection_does_not_fall_back_to_recent_call(tmp_path):
    run_browser_logic(tmp_path, r'''
(async()=>{
state.snapshot=snapshot([session()]);render();
fetch=async()=>({ok:true,json:async()=>({...snapshot([session()]),selected_call_sid:null,selected_call_missing:true})});
window.DashboardCalls.openCall(OTHER);await new Promise(resolve=>setImmediate(resolve));
assert.equal(state.selected,OTHER);assert.equal(state.selectedSession,null);
assert.equal($('messages').children.length,0);
assert.match($('empty-title').textContent,/not available in saved history/);
assert.equal($('export-txt').getAttribute('aria-disabled'),'true');
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


def test_recording_only_archived_stub_gains_transcript_and_export_when_selected(tmp_path):
    run_browser_logic(tmp_path, r'''
(async()=>{
state.snapshot=snapshot([session()]);state.snapshot.history={has_more:true,next_cursor:'older'};render();
fetch=async()=>({ok:true,json:async()=>({...snapshot([],[recording(OTHER)]),history:{has_more:false,next_cursor:null}})});
await loadOlderCalls();openCall(OTHER);
assert.equal($('export-txt').getAttribute('aria-disabled'),'true');
state.snapshot=snapshot([session(OTHER)],[recording(OTHER)]);render();
assert.equal(state.selectedSession.transcript_unavailable,false);
assert.equal($('messages').children.length,1);
assert.equal($('export-txt').getAttribute('aria-disabled'),'false');
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


def test_contact_profile_refreshes_totals_when_returning_from_another_page():
    run_crm(r'''
let total=1,requests=0;
window.DashboardCalls={fetchHistory:async()=>{requests++;return {sessions:[],
 history:{has_more:false,next_cursor:null,total,duration_seconds:0}};}};
navigate('#contacts/'+CONTACT.id);await new Promise(resolve=>setImmediate(resolve));
assert.equal(requests,1);assert.match(text(contactRoot),/Conversations 1/);
navigate('#calls/recent');total=2;navigate('#contacts/'+CONTACT.id);
await new Promise(resolve=>setImmediate(resolve));
assert.equal(requests,2);assert.match(text(contactRoot),/Conversations 2/);
''', before='storeContacts([CONTACT]);')


def test_partial_history_storage_failure_is_visible_without_a_dead_load_button(tmp_path):
    run_browser_logic(tmp_path, r'''
state.snapshot=snapshot([session()]);state.snapshot.history={has_more:false,next_cursor:null,complete:false};render();
assert.equal($('history-controls').hidden,false);assert.equal($('history-load-more').hidden,true);
assert.match($('history-status').textContent,/History may be incomplete/);
''')


def test_contact_history_does_not_present_incomplete_totals_as_lifetime_metrics():
    run_crm(r'''
window.DashboardCalls={fetchHistory:async()=>({sessions:[],
 history:{has_more:false,next_cursor:null,total:5,duration_seconds:600,complete:false}})};
navigate('#contacts/'+CONTACT.id);await new Promise(resolve=>setImmediate(resolve));
assert.match(text(contactRoot),/Conversations loaded 0/);
assert.match(text(contactRoot),/Talk time loaded 0s/);
assert.match(text(contactRoot),/History may be incomplete/);
assert.equal($('crm-load-history'),null);
''', before='storeContacts([CONTACT]);')


def test_contact_list_uses_archive_totals_without_loading_every_page():
    run_crm(r'''
window.DashboardCRM.setHistoryMetrics({[CONTACT.phone]:{total:32,duration_seconds:1800,last_contact_at:'2026-08-02T12:00:00Z'}},{complete:true,caller:''});
navigate('#contacts');
const row=rows().find(row=>text(row).includes('Avery Chen'));
assert.equal(row.children[4].textContent,'32');assert.match(row.children[5].textContent,/Aug 2/);
let release;
window.DashboardCalls={fetchHistory:()=>new Promise(resolve=>{release=resolve;})};
navigate('#contacts/'+CONTACT.id);
assert.match(text(contactRoot),/Conversations 32/);assert.match(text(contactRoot),/Talk time 30m 0s/);
release({sessions:[],history:{has_more:false,next_cursor:null,total:32,duration_seconds:1800,last_contact_at:'2026-08-02T12:00:00Z'}});
await new Promise(resolve=>setImmediate(resolve));
''', before='storeContacts([CONTACT]);')


def test_caller_filtered_metrics_do_not_erase_other_contacts():
    run_crm(r'''
window.DashboardCRM.setHistoryMetrics({[CONTACT.phone]:{total:32,duration_seconds:1800,last_contact_at:'2026-08-02T12:00:00Z'}},{complete:true,caller:''});
window.DashboardCRM.setHistoryMetrics({'+19415550101':{total:4,duration_seconds:30,last_contact_at:'2026-09-27T12:00:00Z'}},{complete:true,caller:'+19415550101'});
navigate('#contacts');
const row=rows().find(row=>text(row).includes('Avery Chen'));
assert.equal(row.children[4].textContent,'32');
const demo=rows().find(row=>text(row).includes('Alex Morgan'));
assert.equal(demo.children[4].textContent,'5','The existing fictional demo conversation stays distinct from real call totals');
''', before='storeContacts([CONTACT]);')


def test_dashboard_forwards_fresh_complete_caller_metrics_to_contacts(tmp_path):
    run_browser_logic(tmp_path, r'''
const received=[];
window.DashboardCRM={setSessions(){},render(){},setHistoryMetrics:(metrics,options)=>received.push({metrics,options})};
state.snapshot=snapshot([session()]);
state.snapshot.history={has_more:false,next_cursor:null,complete:true,caller_metrics:{'+16562520233':{total:12,duration_seconds:100,last_contact_at:'2026-09-26T12:00:00Z'}}};
render();renderNavigation();render();
assert.equal(received.length,1,'Repeated rendering of the same response must not replace newer profile aggregates');
assert.equal(received[0].metrics['+16562520233'].total,12);
assert.deepEqual(received[0].options,{complete:true,caller:''});
''')


def test_new_calls_arriving_during_pagination_do_not_create_a_gap_at_page_boundary(tmp_path):
    run_browser_logic(tmp_path, r'''
(async()=>{
const older='CA'+'c'.repeat(32);
state.snapshot=snapshot([session()]);state.snapshot.history={has_more:true,next_cursor:'before-original'};render();
let release;fetch=async()=>({ok:true,json:()=>new Promise(resolve=>{release=resolve;})});
const pending=loadOlderCalls();await Promise.resolve();await Promise.resolve();
// A new first page replaces the previous boundary while the older request runs.
state.snapshot=snapshot([session(OTHER)]);state.snapshot.history={has_more:true,next_cursor:'before-new'};render();
release({...snapshot([session(older)]),history:{has_more:false,next_cursor:null}});await pending;
assert.deepEqual(new Set(state.sessions.map(item=>item.call_sid)),new Set([SID,OTHER,older]));
})().catch(error=>{console.error(error);process.exitCode=1;});
''')
