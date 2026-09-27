"""Workspace browser adapter: canonical server data, migration and failed writes."""

from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "public/dashboard-workspace.js"
HARNESS = r"""
const assert=require('node:assert/strict');
const values=new Map(), calls=[];
const EMPTY={version:1,contacts:[],demoOverrides:[],agents:[]};
const CONTACT={id:'local-12345678',firstName:'Avery',lastName:'Chen',phone:'+16562520233',createdAt:'2026-09-26T17:00:00Z'};
const AGENT={id:'agent-12345678',name:'Admissions',prompt:'Hello',createdAt:'2026-09-26T17:00:00Z'};
const CONTACT_KEY='hacking-banyons.contacts.v1',AGENT_KEY='hacking-banyons.agent-drafts.v1';
const clone=value=>JSON.parse(JSON.stringify(value));
let server=clone(EMPTY), fail=false, blockedStorage=false;
const window={localStorage:{
 getItem(key){if(blockedStorage)throw new Error('blocked');return values.get(key)??null;},
 setItem(){throw new Error('Browser backups must never be changed');},
 removeItem(){throw new Error('Browser backups must never be removed');},
 clear(){throw new Error('Browser backups must never be removed');}
}};
const response=(data,status=200)=>({ok:status>=200&&status<300,status,async json(){return clone(data);}});
let handler=async(path,options)=>{
 if(fail)return response({},503);
 if(options.method==='GET')return response(server);
 if(path==='/api/workspace/import')return response(server);
 const value=JSON.parse(options.body),key=path.includes('/contacts/')?(value.id.startsWith('demo-')?'demoOverrides':'contacts'):'agents';
 server[key]=server[key].some(item=>item.id===value.id)?server[key].map(item=>item.id===value.id?value:item):[value,...server[key]];
 return response(value);
};
async function fetch(path,options){calls.push({path,...options});return handler(path,options);}
"""


def run_workspace(checks, before=""):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for workspace browser checks")
    result = subprocess.run(
        [node, "-e", HARNESS + before + "\n" + SCRIPT.read_text()
         + "\n(async()=>{const api=window.DashboardWorkspace;\n" + checks
         + "\n})().catch(error=>{console.error(error);process.exitCode=1;});"],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_initial_server_load_and_new_origin_keep_existing_records():
    run_workspace(r"""
const events=[];api.subscribe(state=>events.push(state));
await api.ready;
assert.deepEqual(api.getSnapshot(),server);
assert.equal(calls.length,1);assert.equal(calls[0].path,'/api/workspace');
assert.equal(calls[0].cache,'no-store');assert.equal(calls[0].credentials,'same-origin');
assert.equal(events.at(-1).loading,false);assert.equal(events.at(-1).error,'');
assert.equal(events.at(-1).snapshot.contacts[0].firstName,'Avery');
const detached=api.getSnapshot();detached.contacts[0].firstName='Changed';
assert.equal(api.getSnapshot().contacts[0].firstName,'Avery');
// A fresh browser origin has no legacy localStorage, but the same server data.
assert.equal(values.size,0);
await api.reload();assert.deepEqual(api.getSnapshot().agents,[AGENT]);
""", before="server={...clone(EMPTY),contacts:[CONTACT],agents:[AGENT]};")


def test_imports_all_legacy_records_and_preserves_backups_verbatim():
    run_workspace(r"""
await api.ready;
const sent=calls.find(call=>call.path==='/api/workspace/import');
assert.deepEqual(JSON.parse(sent.body),{contacts:[CONTACT],demoOverrides:[demo],agents:[legacyAgent]});
assert.equal(sent.method,'POST');assert.equal(sent.headers['Content-Type'],'application/json');
assert.equal(sent.headers['X-Workspace-Request'],'1');assert.equal(sent.headers.Origin,undefined);
assert.equal(values.get(CONTACT_KEY),contactBackup);assert.equal(values.get(AGENT_KEY),agentBackup);
assert.equal(api.getSnapshot().agents[0].id,'agent-server-stable-id');
await api.reload();
assert.equal(api.getSnapshot().contacts.length,1);assert.equal(api.getSnapshot().agents.length,1);
assert.equal(values.get(CONTACT_KEY),contactBackup);
""", before=r"""
const demo={...CONTACT,id:'demo-alex-morgan',phone:'+19415550101'};
const legacyAgent={name:'Admissions',prompt:'Hello'};
const contactBackup=JSON.stringify({version:1,contacts:[CONTACT],demoOverrides:[demo]});
const agentBackup=JSON.stringify([legacyAgent]);
values.set(CONTACT_KEY,contactBackup);values.set(AGENT_KEY,agentBackup);
handler=async(path,options)=>{
 if(path==='/api/workspace/import')server={...clone(EMPTY),contacts:[CONTACT],demoOverrides:[demo],agents:[{...legacyAgent,id:'agent-server-stable-id'}]};
 return response(server);
};
""")


def test_put_updates_canonical_snapshot_and_does_not_write_browser_storage():
    run_workspace(r"""
await api.ready;
const events=[];api.subscribe(value=>events.push(value));
const saved=await api.saveContact(CONTACT);
assert.deepEqual(saved,CONTACT);assert.deepEqual(api.getSnapshot().contacts,[CONTACT]);
assert.deepEqual(events.at(-1).snapshot.contacts,[CONTACT]);
await api.saveContact({...CONTACT,firstName:'Updated'});
assert.equal(api.getSnapshot().contacts.length,1);assert.equal(api.getSnapshot().contacts[0].firstName,'Updated');
await api.saveContact({...CONTACT,id:'demo-alex-morgan',phone:'+19415550101'});
await api.saveAgent(AGENT);
assert.equal(api.getSnapshot().demoOverrides.length,1);assert.deepEqual(api.getSnapshot().agents,[AGENT]);
for(const call of calls.filter(call=>call.method==='PUT')) {
 assert.equal(call.headers['Content-Type'],'application/json');assert.equal(call.headers['X-Workspace-Request'],'1');
}
assert.equal(values.size,0);
""")


def test_failed_write_never_publishes_unsaved_record_and_retry_recovers():
    run_workspace(r"""
await api.ready;
fail=true;
await assert.rejects(api.saveContact(CONTACT),/Workspace storage is unavailable/);
assert.equal(api.getSnapshot().contacts.length,0);
let latest;api.subscribe(value=>latest=value);assert.match(latest.error,/try again/);
fail=false;await api.saveContact(CONTACT);
assert.equal(api.getSnapshot().contacts.length,1);assert.equal(latest.error,'');
""")


def test_unavailable_initial_load_never_promotes_browser_backup_to_canonical():
    run_workspace(r"""
await assert.rejects(api.ready,/Workspace storage is unavailable/);
assert.equal(api.getSnapshot(),null);
await assert.rejects(api.saveContact(CONTACT),/Workspace storage is unavailable/);
assert.equal(calls.some(call=>call.method==='PUT'),false);
assert.equal(values.get(CONTACT_KEY),backup);
fail=false;await api.reload();assert.equal(api.getSnapshot().contacts.length,0);
""", before="fail=true;const backup=JSON.stringify({version:1,contacts:[CONTACT]});values.set(CONTACT_KEY,backup);")


def test_malformed_or_blocked_browser_storage_does_not_hide_server_records():
    run_workspace(r"""
await api.ready;
let state;api.subscribe(value=>state=value);
assert.deepEqual(api.getSnapshot().contacts,[CONTACT]);assert.match(state.importError,/backup is unchanged/);
assert.equal(values.get(CONTACT_KEY),'{broken');
blockedStorage=true;await api.reload();assert.deepEqual(api.getSnapshot().contacts,[CONTACT]);
await api.saveAgent(AGENT);assert.deepEqual(api.getSnapshot().agents,[AGENT]);
""", before="server={...clone(EMPTY),contacts:[CONTACT]};values.set(CONTACT_KEY,'{broken');")


def test_import_failure_keeps_server_canonical_and_backup_for_retry():
    run_workspace(r"""
await api.ready;
let state;api.subscribe(value=>state=value);
assert.equal(api.getSnapshot().contacts.length,0);assert.match(state.importError,/reload to retry/);
assert.equal(values.get(CONTACT_KEY),backup);
""", before=r"""
const backup=JSON.stringify({version:1,contacts:[CONTACT]});values.set(CONTACT_KEY,backup);
handler=async(path)=>path.endsWith('/import')?response({},503):response(EMPTY);
""")


def test_serial_requests_preserve_contact_and_agent_updates_with_slow_load():
    run_workspace(r"""
const pendingContact=api.saveContact(CONTACT),pendingAgent=api.saveAgent(AGENT);
await Promise.resolve();assert.equal(calls.length,1);
releaseLoad();await Promise.all([api.ready,pendingContact,pendingAgent]);
assert.deepEqual(calls.map(call=>call.method),['GET','PUT','PUT']);
assert.deepEqual(api.getSnapshot().contacts,[CONTACT]);assert.deepEqual(api.getSnapshot().agents,[AGENT]);
""", before=r"""
let releaseLoad;const loadGate=new Promise(resolve=>releaseLoad=resolve);const normalHandler=handler;
handler=async(path,options)=>{if(options.method==='GET')await loadGate;return normalHandler(path,options);};
""")
