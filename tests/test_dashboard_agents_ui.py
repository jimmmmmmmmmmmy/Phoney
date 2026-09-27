"""Agents management behavior; provider and call requests are mocked offline."""

from pathlib import Path
import shutil
import subprocess

import pytest

from test_dashboard_toolbar_ui import HARNESS

SCRIPT = Path(__file__).resolve().parents[1] / "public/dashboard-agents.js"
SETUP = r"""
const draft = {id:'agent-12345678',name:'Admissions',prompt:'Ask what they need.',createdAt:'2026-09-26T20:00:00Z'};
workspaceSnapshot.agents=[clone(draft)];
const requests=[];
let registry={authenticated:false,enabled:true,manualEnabled:true,agents:[],voices:[],capabilities:{voiceCatalog:true,voiceCloning:true}};
const voice={id:'voice-12345678',name:'Owner voice',voiceId:'elevenOwner',ready:true,requiresVerification:false};
const published={...draft,voiceProfileId:voice.id,voiceId:voice.voiceId,slot:1,revision:1};
const reply=(value,status=200)=>({ok:status>=200&&status<300,status,json:async()=>clone(value)});
let handler=async(path,options)=>{
 if(path==='/api/agents/config')return reply(registry);
 if(path==='/api/agents/session'&&options.method==='POST'){
   registry={...registry,authenticated:true,voices:[voice]};return reply(registry);
 }
 if(path==='/api/agents/session'&&options.method==='DELETE')return reply({authenticated:false});
 if(path==='/api/agents/voices/refresh')return reply({voices:[voice]});
 if(path==='/api/agents/voices/clone')return reply(voice);
 if(path==='/api/agents/'+draft.id)return reply({...published,...JSON.parse(options.body)});
 if(path==='/api/operator/sessions')return reply({voice_ready:true,sessions:[{id:'session-1',to:'+16565550123',mode:'human'}]});
 if(path.includes('/takeover'))return reply({session_id:'session-1',mode:'announcing',slot:'1'});
 if(path.endsWith('/mode'))return reply({session_id:'session-1',mode:'human',changed:true});
 throw Error('Unexpected request: '+path);
};
async function fetch(path,options){requests.push({path,...options});return handler(path,options);}
window.confirm=()=>true;
let createOpened=0;window.DashboardToolbar={openCreateAgent:()=>createOpened++};
const named=textValue=>$('agents-view').all().find(element=>element.tagName==='BUTTON'&&element.textContent===textValue);
const change=(id,value)=>{$(id).value=value;$(id).dispatch('input');$(id).dispatch('change');};
const unlock=async()=>{$('agent-owner-code').value='once-only-code';$('agent-owner-code').parentNode.dispatch('submit');await tick();};
"""


def run_agents(checks, before=""):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for dashboard behavior tests")
    result = subprocess.run(
        [node, "-e", HARNESS + SETUP + before + "\n" + SCRIPT.read_text()
         + "\n(async()=>{await tick();\n" + checks
         + "\n})().catch(error=>{console.error(error);process.exitCode=1;});"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_public_drafts_remain_editable_without_enabling_call_controls():
    run_agents(r"""
assert.equal($('agent-edit-name').value,'Admissions');
assert.equal($('agent-publish').disabled,true);
assert.equal($('agent-edit-voice').disabled,true);
assert.equal($('agent-live-call'),null);
change('agent-edit-name','Admissions updated');
change('agent-edit-prompt','<script>literal prompt</script>');
named('Save draft').click();await tick();
assert.equal(workspaceSnapshot.agents[0].name,'Admissions updated');
assert.equal(workspaceSnapshot.agents[0].prompt,'<script>literal prompt</script>');
assert.equal(requests.length,1);assert.equal(requests[0].path,'/api/agents/config');
assert.match(text($('agents-view')),/Published call settings are unchanged/);
assert.equal($('agents-view').all().some(item=>item.tagName==='SCRIPT'),false);
named('New agent').click();assert.equal(createOpened,1);
""")


def test_unlock_uses_same_origin_cookie_and_never_keeps_code_in_the_form():
    run_agents(r"""
await unlock();
const sent=requests.find(item=>item.path==='/api/agents/session');
assert.equal(sent.credentials,'same-origin');assert.equal(sent.cache,'no-store');
assert.equal(sent.headers['X-Agent-Request'],'1');
assert.equal(sent.headers.Authorization,undefined);
assert.deepEqual(JSON.parse(sent.body),{code:'once-only-code'});
assert.equal($('agent-owner-code'),null);
assert.equal($('agent-publish').disabled,false);
assert.equal($('agent-edit-voice').children[1].value,voice.id);
assert.equal(requests.filter(item=>item.path.includes('/voices/')).length,0);
assert.equal(requests.filter(item=>item.path.includes('/sessions')).length,0);
named('Lock controls').click();await tick();
assert.equal($('agent-publish').disabled,true);
assert.equal($('agent-owner-code').value,'');
assert.equal($('agent-live-call'),null);
""")


def test_publish_takes_exact_owner_edited_snapshot_and_blocks_occupied_slots():
    run_agents(r"""
assert.equal($('agent-edit-slot').children.find(item=>item.value==='9').disabled,true);
change('agent-edit-name','Leasing agent');change('agent-edit-prompt','Ask about availability.');
change('agent-edit-voice',voice.id);change('agent-edit-slot','2');
$('agent-publish').click();await tick();
const sent=requests.find(item=>item.path==='/api/agents/'+draft.id);
assert.deepEqual(JSON.parse(sent.body),{name:'Leasing agent',prompt:'Ask about availability.',voiceProfileId:voice.id,slot:2});
assert.equal(workspaceSnapshot.agents[0].name,'Leasing agent');
assert.match(text($('agents-view')),/Published for #2/);
assert.equal(requests.filter(item=>item.path.includes('/takeover')).length,0);
""", before="registry={...registry,authenticated:true,voices:[voice],agents:[{...published,id:'agent-other123',name:'Other',slot:9}]};")


def test_publish_rejects_unverified_voice_and_empty_prompt_before_mutation():
    run_agents(r"""
change('agent-edit-voice',voice.id);change('agent-edit-slot','1');
$('agent-publish').click();await tick();
assert.match(text($('agents-view')),/Choose a ready voice/);
assert.equal(requests.length,1);assert.equal(writes,0);
change('agent-edit-prompt','');$('agent-publish').click();await tick();
assert.match(text($('agents-view')),/Add a prompt/);assert.equal(requests.length,1);
""", before="registry={...registry,authenticated:true,voices:[{...voice,ready:false,requiresVerification:true}]};")


def test_call_takeover_only_follows_manual_refresh_and_explicit_action():
    run_agents(r"""
assert.equal(requests.length,1);assert.equal($('agent-takeover').disabled,true);
named('Refresh calls').click();await tick();
assert.equal($('agent-live-call').value,'session-1');
change('agent-live-slot','1');$('agent-takeover').click();await tick();
const takeover=requests.find(item=>item.path==='/api/sessions/session-1/takeover');
assert.deepEqual(JSON.parse(takeover.body),{slot:'1'});
assert.equal(takeover.headers['X-Agent-Request'],'1');
$('agent-release').click();await tick();
const release=requests.find(item=>item.path==='/api/sessions/session-1/mode');
assert.deepEqual(JSON.parse(release.body),{mode:'human'});
assert.match(text($('agents-view')),/Human control restored/);
""", before="registry={...registry,authenticated:true,voices:[voice],agents:[published]};")


def test_failed_draft_save_preserves_unsaved_input_and_does_not_publish():
    run_agents(r"""
change('agent-edit-name','Still editing');change('agent-edit-prompt','Retain this text.');
failWrites=true;named('Save draft').click();await tick();
assert.equal($('agent-edit-name').value,'Still editing');
assert.equal($('agent-edit-prompt').value,'Retain this text.');
assert.match(text($('agents-view')),/Workspace storage is unavailable/);
assert.equal(workspaceSnapshot.agents[0].name,'Admissions');assert.equal(requests.length,1);
""")


def test_clone_requires_consent_and_checks_upload_limits_before_request():
    run_agents(r"""
const form=$('agent-clone-name').parentNode.parentNode;
$('agent-clone-name').value='My clone';
$('agent-clone-files').files=[{size:10,name:'sample.wav'}];
$('agent-clone-consent').checked=false;form.dispatch('submit');await tick();
assert.equal(requests.length,1);
$('agent-clone-consent').checked=true;
$('agent-clone-files').files=[{size:21*1024*1024,name:'large.wav'}];
form.dispatch('submit');await tick();
assert.equal(requests.length,1);assert.match(text($('agents-view')),/16 MB combined/);
""", before="registry={...registry,authenticated:true,voices:[voice]};")


def test_pending_publish_prevents_double_request_and_preserves_current_input():
    run_agents(r"""
let complete;const gate=new Promise(resolve=>{complete=resolve;});
const originalHandler=handler;
handler=async(path,options)=>{if(options.method==='PUT')await gate;return originalHandler(path,options);};
change('agent-edit-voice',voice.id);change('agent-edit-slot','1');
$('agent-publish').click();await tick();
assert.equal($('agent-publish').disabled,true);assert.equal($('agent-edit-name').disabled,true);
$('agent-publish').click();await tick();
assert.equal(requests.filter(item=>item.method==='PUT').length,1);
complete();await tick();assert.equal($('agent-publish').disabled,false);
""", before="registry={...registry,authenticated:true,voices:[voice]};")


def test_generated_transcript_identifies_agent_without_transcription_confidence(tmp_path):
    from test_dashboard_playback_ui import run_browser_logic

    run_browser_logic(tmp_path, r"""
const call=session();
call.segments=[{...call.segments[0],source:'agent',speaker:'Leasing assistant',track:'outbound',
  delivery:'played',confidence:1,text:'The available time is Tuesday.'}];
state.snapshot=snapshot([call]);render();openCall();
const meta=()=>$('messages').children[0].children[1].children[0];
assert.equal(meta().children[0].textContent,'Leasing assistant · Agent');
assert.equal(meta().children.some(element=>element.className==='confidence'),false);
call.segments[0].delivery='interrupted';render();
assert.equal(meta().children[1].textContent,'Interrupted');
assert.equal(meta().children.some(element=>element.className==='confidence'),false);
call.segments=[{...session().segments[0],track:'outbound',confidence:.95}];render();
assert.equal(meta().children[0].textContent,trackNames.outbound);
assert.equal(meta().children[1].textContent,'95% confidence');
""")


def test_disabled_manual_flag_blocks_takeover_but_keeps_emergency_return():
    run_agents(r"""
named('Refresh calls').click();await tick();
assert.equal($('agent-takeover').disabled,true);
assert.equal($('agent-release').disabled,false);
assert.match(text($('agents-view')),/Manual takeover is not enabled yet/);
$('agent-takeover').click();await tick();
assert.equal(requests.filter(item=>item.path.includes('/takeover')).length,0);
""", before="registry={...registry,authenticated:true,manualEnabled:false,voices:[voice],agents:[published]};")
