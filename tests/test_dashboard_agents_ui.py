"""Agent list and shared New/Edit modal behavior; all API requests are mocked offline."""

from pathlib import Path
import shutil
import subprocess

import pytest

from test_dashboard_toolbar_ui import HARNESS

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "public/dashboard-agents.js"
SETUP = r"""
const draft = {id:'agent-12345678',name:'Admissions',prompt:'Ask what they need.',createdAt:'2026-09-26T20:00:00Z'};
workspaceSnapshot.agents=[clone(draft)];
const requests=[];
const voice={id:'voice-12345678',name:'Owner',voiceId:'elevenOwner',ready:true,requiresVerification:false};
const published={...draft,voiceProfileId:voice.id,voiceId:voice.voiceId,slot:1,revision:1};
let registry={authenticated:true,demoMode:true,enabled:true,manualEnabled:false,agents:[published],voices:[voice]};
const reply=(value,status=200)=>({ok:status>=200&&status<300,status,json:async()=>clone(value)});
let handler=async(path,options)=>{
 if(path==='/api/agents/config')return reply(registry);
 if(path.startsWith('/api/agents/')&&options.method==='PUT'){
   const saved={id:decodeURIComponent(path.slice('/api/agents/'.length)),revision:2,...JSON.parse(options.body)};
   registry.agents=[...registry.agents.filter(item=>item.id!==saved.id),saved];
   return reply(saved);
 }
 throw Error('Unexpected request: '+path);
};
async function fetch(path,options){requests.push({path,...options});return handler(path,options);}
const named=textValue=>$('agents-view').all().find(element=>element.tagName==='BUTTON'&&element.textContent===textValue);
const change=(id,value)=>{$(id).value=value;$(id).dispatch('input');$(id).dispatch('change');};
const edits=()=>$('agents-view').all().filter(element=>element.tagName==='BUTTON'&&element.textContent==='Edit');
const openEdit=()=>edits()[0].click();
"""


def run_agents(checks, before="", *, with_toolbar=False):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for dashboard behavior tests")
    toolbar = (ROOT / "public/dashboard-toolbar.js").read_text() if with_toolbar else ""
    result = subprocess.run(
        [node, "-e", HARNESS + SETUP + before + "\n" + toolbar + "\n" + SCRIPT.read_text()
         + "\n(async()=>{await tick();\n" + checks
         + "\n})().catch(error=>{console.error(error);process.exitCode=1;});"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_agents_page_contains_only_list_new_button_and_edit_actions():
    run_agents(r"""
const page = $('agents-view');
assert.match(text(page), /Admissions/);assert.match(text(page), /Owner/);
assert.match(text(page), /#1/);assert.match(text(page), /Ask what they need/);
assert.equal(edits().length,1);
assert.equal(page.all().filter(item=>item.tagName==='BUTTON').length,2);
assert.equal(page.all().filter(item=>item.tagName==='H2').length,0,'The global page title already identifies Agents');
assert.equal(page.all().some(item=>['INPUT','TEXTAREA','SELECT','FORM'].includes(item.tagName)),false);
assert.doesNotMatch(text(page),/Owner controls|Unlock|Lock controls|Voices|clone|Manual call|Publish|Save draft/);
assert.equal(requests.length,1);assert.equal(requests[0].path,'/api/agents/config');
assert.equal($('create-agent-dialog').open,false);
""")


def test_edit_modal_has_all_fields_and_saves_one_configuration_request():
    run_agents(r"""
openEdit();
assert.equal($('create-agent-dialog').open,true);
assert.equal($('create-agent-title').textContent,'Edit agent');
assert.equal(document.activeElement,$('agent-name'));
assert.equal($('agent-name').value,'Admissions');
assert.equal($('agent-outbound-prompt').value,'Ask what they need.');
assert.equal($('agent-edit-voice').value,voice.id);
assert.equal($('agent-edit-slot').value,'1');
change('agent-name','Leasing agent');change('agent-outbound-prompt','Ask about availability.');
change('agent-edit-slot','2');await submit();
const sent=requests.find(item=>item.method==='PUT');
assert.equal(sent.path,'/api/agents/'+draft.id);
assert.deepEqual(JSON.parse(sent.body),{name:'Leasing agent',prompt:'Ask about availability.',voiceProfileId:voice.id,slot:2});
assert.equal(sent.credentials,'same-origin');assert.equal(sent.cache,'no-store');
assert.equal(sent.headers['X-Agent-Request'],'1');
assert.equal(sent.headers.Authorization,undefined);
assert.equal(writes,0,'A single registry save must not require a workspace draft write');
assert.equal($('create-agent-dialog').open,false);
assert.match(text($('agents-view')),/Leasing agent/);assert.match(text($('agents-view')),/#2/);
assert.equal(edits().length,1,'Registry records replace legacy drafts with the same ID');
assert.equal(document.activeElement,edits()[0]);
assert.equal(requests.length,2);
""")


def test_new_agent_defaults_to_owner_voice_and_both_entrypoints_share_dialog():
    run_agents(r"""
$('create-button').click();menuItems()[1].click();
const shared=$('create-agent-dialog');
assert.equal(shared.open,true);assert.equal($('create-menu').hidden,true);
assert.equal($('create-agent-title').textContent,'New agent');
assert.equal($('agent-edit-voice').value,voice.id,'Prefer Owner even when another ready voice is first');
assert.equal($('agent-edit-slot').value,'');
$('create-agent-close').click();
assert.equal(document.activeElement,$('create-button'));
named('New agent').click();assert.equal($('create-agent-dialog'),shared);
change('agent-name','Voice Clone');change('agent-outbound-prompt','Tries to hang the call up asap');
change('agent-edit-slot','2');await submit();
assert.equal(shared.open,false);assert.equal(edits().length,2);
const sent=requests.find(item=>item.method==='PUT');
assert.match(sent.path,/^\/api\/agents\/agent-/);
assert.deepEqual(JSON.parse(sent.body),{name:'Voice Clone',prompt:'Tries to hang the call up asap',voiceProfileId:voice.id,slot:2});
assert.equal(document.body.all().filter(item=>item.tagName==='DIALOG').length,1);
const savedEdit=edits().find(item=>item.getAttribute('aria-label')==='Edit Voice Clone');
shared.dispatch('close');
assert.equal(document.activeElement,savedEdit,'A queued native close event must retain focus on the saved agent');
""", before="registry.voices=[{...voice,id:'voice-other123',name:'Other voice'},voice];", with_toolbar=True)


def test_slot_choices_disable_other_assignments_but_keep_current_slot():
    run_agents(r"""
openEdit();
const choices=$('agent-edit-slot').children;
assert.equal(choices.length,10);
assert.equal(choices.find(item=>item.value==='1').disabled,false);
assert.equal(choices.find(item=>item.value==='9').disabled,true);
assert.match(choices.find(item=>item.value==='9').textContent,/Other/);
change('agent-edit-slot','9');await submit();
assert.equal(requests.length,1);assert.match(text($('create-agent-dialog')),/already assigned/);
assert.equal(document.activeElement,$('agent-edit-slot'));
""", before="registry.agents.push({...published,id:'agent-other123',name:'Other',slot:9});")


def test_invalid_name_prompt_and_unavailable_voice_fail_before_mutation():
    run_agents(r"""
openEdit();change('agent-name','  ');await submit();
assert.match(text($('create-agent-dialog')),/Enter an agent name/);
assert.equal(document.activeElement,$('agent-name'));
change('agent-name','x'.repeat(81));await submit();
assert.match(text($('create-agent-dialog')),/80 characters/);
change('agent-name','Valid');change('agent-outbound-prompt','');await submit();
assert.match(text($('create-agent-dialog')),/Add a prompt/);
change('agent-outbound-prompt','x'.repeat(8001));await submit();
assert.match(text($('create-agent-dialog')),/8,000 characters/);
change('agent-outbound-prompt','Prompt');change('agent-edit-voice',voice.id);await submit();
assert.match(text($('create-agent-dialog')),/Choose an available voice/);
assert.equal($('agent-edit-voice').children[1].disabled,true);
assert.equal(requests.length,1);
""", before="registry.voices=[{...voice,ready:false,requiresVerification:true}];")


def test_cancel_and_escape_restore_focus_without_save():
    run_agents(r"""
const trigger=edits()[0];trigger.focus();trigger.click();
$('agent-cancel').click();assert.equal($('create-agent-dialog').open,false);
assert.equal(document.activeElement,trigger);
named('New agent').focus();named('New agent').click();
const cancel=$('create-agent-dialog').dispatch('cancel');
assert.notEqual(cancel.defaultPrevented,true,'Native dialog Escape remains enabled');
$('create-agent-dialog').close();
assert.equal(document.activeElement,named('New agent'));
assert.equal(requests.length,1);
""")


def test_failed_save_preserves_form_and_retries_same_identity():
    run_agents(r"""
named('New agent').click();
change('agent-name','Retain my input');change('agent-outbound-prompt','Keep this prompt.');
let failed=true;const original=handler;
handler=(path,options)=>options.method==='PUT'&&failed?reply({detail:'Save unavailable.'},503):original(path,options);
await submit();
assert.equal($('create-agent-dialog').open,true);
assert.equal($('agent-name').value,'Retain my input');
assert.equal($('agent-outbound-prompt').value,'Keep this prompt.');
assert.equal($('agent-name').disabled,false);
assert.equal(document.activeElement,$('agent-form-error'));
assert.match(text($('create-agent-dialog')),/Save unavailable/);
assert.equal(edits().length,1);
change('agent-outbound-prompt','Revised prompt.');failed=false;await submit();
const puts=requests.filter(item=>item.method==='PUT');assert.equal(puts.length,2);
assert.equal(puts[0].path,puts[1].path);
assert.equal(JSON.parse(puts[1].body).prompt,'Revised prompt.');
assert.equal($('create-agent-dialog').open,false);assert.equal(edits().length,2);
""")


def test_pending_save_blocks_duplicate_submits_and_dialog_dismissal():
    run_agents(r"""
let complete;const gate=new Promise(resolve=>{complete=resolve;});
const original=handler;handler=async(path,options)=>{if(options.method==='PUT')await gate;return original(path,options);};
openEdit();change('agent-name','Updated');await submit();
const form=$('create-agent-dialog').children[0];
assert.equal(form.getAttribute('aria-busy'),'true');
for(const id of ['agent-name','agent-outbound-prompt','agent-edit-voice','agent-edit-slot','create-agent-close','agent-cancel','agent-save'])assert.equal($(id).disabled,true);
await submit();window.DashboardAgents.openCreateAgent();
assert.equal($('agent-name').value,'Updated');
$('create-agent-close').click();$('agent-cancel').click();
assert.equal($('create-agent-dialog').open,true);
assert.equal($('create-agent-dialog').dispatch('cancel').defaultPrevented,true);
assert.equal(requests.filter(item=>item.method==='PUT').length,1);
complete();await tick();assert.equal(form.getAttribute('aria-busy'),'false');
assert.equal($('create-agent-dialog').open,false);
assert.match(text($('agents-view')),/Updated/);
""")


def test_conflicting_shortcut_refreshes_choices_without_losing_input():
    run_agents(r"""
openEdit();change('agent-name','Preserved');change('agent-edit-slot','2');
const original=handler;
handler=(path,options)=>{
 if(options.method==='PUT'){
   registry.agents.push({...published,id:'agent-concurrent',name:'Other user',slot:2});
   return reply({detail:'Shortcut is now in use.'},409);
 }
 return original(path,options);
};
await submit();
assert.equal($('create-agent-dialog').open,true);
assert.equal($('agent-name').value,'Preserved');
assert.equal($('agent-edit-slot').children.find(item=>item.value==='2').disabled,true);
assert.match(text($('create-agent-dialog')),/Shortcut is now in use/);
assert.equal(requests.filter(item=>item.path==='/api/agents/config').length,2);
""")


def test_registry_is_canonical_over_workspace_drafts_and_markup_stays_literal():
    run_agents(r"""
assert.match(text($('agents-view')),/<img src=x onerror=alert\(1\)>/);
assert.match(text($('agents-view')),/<script>literal<\/script>/);
assert.doesNotMatch(text($('agents-view')),/Ask what they need/);
assert.equal($('agents-view').all().some(item=>['SCRIPT','IMG'].includes(item.tagName)),false);
workspaceSnapshot.agents.push({id:'agent-legacy123',name:'Legacy draft',prompt:'Kept for editing.'});publishWorkspace();
assert.equal(edits().length,2);assert.match(text($('agents-view')),/Legacy draft/);
assert.match(text($('agents-view')),/<img src=x onerror=alert\(1\)>/);
""", before="registry.agents=[{...published,name:'<img src=x onerror=alert(1)>',prompt:'<script>literal</script>'}];")


def test_configuration_unavailable_is_visible_without_owner_controls():
    run_agents(r"""
assert.match(text($('agents-view')),/Connection unavailable/);
assert.match(text($('agents-view')),/Admissions/,'Legacy agents remain visible');
assert.doesNotMatch(text($('agents-view')),/Owner controls|Unlock|Lock controls/);
openEdit();await tick();
assert.equal($('create-agent-dialog').open,true);
assert.equal($('agent-name').value,'Admissions');
assert.match(text($('create-agent-dialog')),/Connection unavailable/);
await submit();
assert.equal(requests.filter(item=>item.method==='PUT').length,0);
assert.match(text($('create-agent-dialog')),/Agent editing is unavailable/);
""", before="handler=async()=>reply({detail:'Connection unavailable.'},503);")


def test_configuration_load_does_not_erase_name_or_prompt_being_typed():
    run_agents(r"""
named('New agent').click();change('agent-name','Typed while loading');change('agent-outbound-prompt','Keep this text.');
assert.equal($('agent-save').disabled,true);
release();await tick();
assert.equal($('agent-name').value,'Typed while loading');
assert.equal($('agent-outbound-prompt').value,'Keep this text.');
assert.equal($('agent-edit-voice').value,voice.id);
assert.equal($('agent-save').disabled,false);
await submit();assert.match(text($('agents-view')),/Typed while loading/);
""", before="let release;const gate=new Promise(resolve=>{release=resolve;});const original=handler;handler=async(path,options)=>{if(path==='/api/agents/config')await gate;return original(path,options);};")


def test_delayed_configuration_replaces_untouched_legacy_fields_with_canonical_values():
    run_agents(r"""
openEdit();
assert.equal($('agent-name').value,'Admissions');
release();await tick();
assert.equal($('agent-name').value,'Published name');
assert.equal($('agent-outbound-prompt').value,'Latest published instructions.');
await submit();
const payload=JSON.parse(requests.find(item=>item.method==='PUT').body);
assert.equal(payload.name,'Published name');assert.equal(payload.prompt,'Latest published instructions.');
""", before="registry.agents=[{...published,name:'Published name',prompt:'Latest published instructions.'}];let release;const gate=new Promise(resolve=>{release=resolve;});const original=handler;handler=async(path,options)=>{if(path==='/api/agents/config')await gate;return original(path,options);};")


def test_delayed_configuration_preserves_typed_edit_and_updates_only_untouched_field():
    run_agents(r"""
openEdit();change('agent-outbound-prompt','My new instructions.');
release();await tick();
assert.equal($('agent-name').value,'Published name');
assert.equal($('agent-outbound-prompt').value,'My new instructions.');
await submit();
const payload=JSON.parse(requests.find(item=>item.method==='PUT').body);
assert.equal(payload.name,'Published name');assert.equal(payload.prompt,'My new instructions.');
""", before="registry.agents=[{...published,name:'Published name',prompt:'Latest published instructions.'}];let release;const gate=new Promise(resolve=>{release=resolve;});const original=handler;handler=async(path,options)=>{if(path==='/api/agents/config')await gate;return original(path,options);};")


def test_server_authorizes_saving_without_a_client_owner_unlock_flow():
    run_agents(r"""
openEdit();await submit();
assert.equal(requests.filter(item=>item.method==='PUT').length,1);
assert.equal(requests.some(item=>item.path==='/api/agents/session'),false);
assert.equal($('create-agent-dialog').open,false);
""", before="registry.authenticated=false;")


def test_agent_without_shortcut_can_be_saved_without_voice():
    run_agents(r"""
openEdit();change('agent-edit-slot','');change('agent-edit-voice','');await submit();
const sent=requests.find(item=>item.method==='PUT');
assert.equal(JSON.parse(sent.body).slot,null);assert.equal(JSON.parse(sent.body).voiceProfileId,null);
assert.match(text($('agents-view')),/No shortcut/);assert.match(text($('agents-view')),/No voice selected/);
""", before="registry.voices=[];")


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
