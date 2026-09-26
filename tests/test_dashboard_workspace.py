"""Dashboard workspace assets and navigation retain the live call's player."""

from html.parser import HTMLParser
import pytest

from test_dashboard import client_for
from test_dashboard_playback_ui import run_browser_logic


WORKSPACE_ASSETS = {
    "/assets/dashboard-crm.js": "javascript",
    "/assets/dashboard-crm.css": "text/css",
    "/assets/dashboard-toolbar.js": "javascript",
    "/assets/dashboard-toolbar.css": "text/css",
}


class WorkspaceAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.styles = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("src"):
            self.scripts.append(attrs["src"])
        if tag == "link" and attrs.get("rel") == "stylesheet":
            self.styles.append(attrs["href"])


def test_workspace_assets_load_under_the_dashboard_content_security_policy():
    with client_for() as client:
        dashboard = client.get("/dashboard")
        parsed = WorkspaceAssets()
        parsed.feed(dashboard.text)
        assert set(WORKSPACE_ASSETS) <= set(parsed.scripts + parsed.styles)
        policy = dict(part.strip().split(" ", 1)
                      for part in dashboard.headers["content-security-policy"].split(";")
                      if part.strip())
        for resource in ("script-src", "style-src"):
            assert "'self'" in policy[resource].split()
            assert "'unsafe-inline'" not in policy[resource]
            assert "'unsafe-eval'" not in policy[resource]
            assert "*" not in policy[resource]


@pytest.mark.parametrize("path,content_type", WORKSPACE_ASSETS.items())
def test_workspace_asset_get_and_head_have_safe_content_types(path, content_type):
    with client_for() as client:
        response = client.get(path)
        assert response.status_code == 200
        assert content_type in response.headers["content-type"]
        assert len(response.content) > 100
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        head = client.head(path)
        assert head.status_code == 200
        assert head.content == b""
        assert head.headers["content-length"] == response.headers["content-length"]


@pytest.mark.parametrize("path", (
    "/assets/dashboard.py", "/assets/.env", "/assets/not-a-dashboard-script.js",
))
def test_asset_route_does_not_expose_other_workspace_files(path):
    with client_for() as client:
        assert client.get(path).status_code == 404


def test_contact_navigation_and_polling_preserve_the_playing_call(tmp_path):
    run_browser_logic(tmp_path, r'''
state.snapshot=snapshot([session()],[recording()]);render();openCall();
const audio=$('call-audio');audio.play();audio.currentTime=39;
const source=audio.src, loads=audio.loads, pauses=audio.pauses;
const crmRenders=[];
window.DashboardCRM={
 setSessions(sessions){this.sessions=sessions;},
 render(){crmRenders.push({hash:window.location.hash,page:state.page});}
};
showPage('contacts');
assert.equal(window.location.hash,'#contacts');
assert.deepEqual(crmRenders.at(-1),{hash:'#contacts',page:'contacts'});
assert.equal($('contacts-view').hidden,false);
assert.equal($('dashboard-view').hidden,true);
assert.equal($('audio-panel').hidden,false);
assert.equal(audio.paused,false);
assert.equal(audio.currentTime,39);
assert.equal(audio.src,source);
assert.equal(audio.loads,loads);assert.equal(audio.pauses,pauses);

// The CRM owns its profile hash. A transcription poll cannot reset that route.
window.history.pushState(null,'','#contacts/demo-alex');
state.snapshot=snapshot([{...session(),segments:[]}],[recording()]);render();
assert.equal(window.location.hash,'#contacts/demo-alex');
assert.deepEqual(crmRenders.at(-1),{hash:'#contacts/demo-alex',page:'contacts'});
assert.equal(window.DashboardCRM.sessions[0].call_sid,SID);
assert.equal(audio.paused,false);assert.equal(audio.currentTime,39);
assert.equal(audio.src,source);assert.equal(audio.loads,loads);assert.equal(audio.pauses,pauses);
showPage('team');
assert.equal(audio.paused,false);assert.equal(audio.currentTime,39);
showPage('contacts');
assert.equal(window.location.hash,'#contacts');
assert.equal(audio.loads,loads);assert.equal(audio.pauses,pauses);
''')


def test_history_restores_a_contact_profile_without_replacing_audio(tmp_path):
    run_browser_logic(tmp_path, r'''
(async()=>{
 state.snapshot=snapshot([session()],[recording()]);render();openCall();
 const audio=$('call-audio');audio.play();audio.currentTime=23;
 const loads=audio.loads,pauses=audio.pauses,source=audio.src;
 const routes=[];
 window.DashboardCRM={setSessions(){},render(){routes.push(window.location.hash);}};
 // Simulate browser Back to a contact profile; fetch refreshes call metadata only.
 fetch=async()=>({ok:true,json:async()=>snapshot([session()],[recording()])});
 window.location.hash='#contacts/demo-jordan';
 handlers.get('popstate')();
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(state.page,'contacts');
 assert.equal($('contacts-view').hidden,false);
 assert.equal(routes.at(-1),'#contacts/demo-jordan');
 assert.equal(audio.src,source);assert.equal(audio.currentTime,23);
 assert.equal(audio.loads,loads);assert.equal(audio.pauses,pauses);assert.equal(audio.paused,false);
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


def test_contact_call_action_restores_a_dismissed_player_without_autoplay(tmp_path):
    run_browser_logic(tmp_path, r'''
state.snapshot=snapshot([session()],[recording()]);render();openCall();
const audio=$('call-audio');audio.play();audio.currentTime=19;
showPage('contacts');dismissAudio();
assert.equal($('audio-panel').hidden,true);
const plays=audio.plays,loads=audio.loads;
state.paused=true;
window.DashboardCalls.openCall(SID);
assert.equal(state.page,'calls');assert.equal(state.detail,true);
assert.equal($('audio-panel').hidden,false);assert.equal(audio.paused,true);
assert.equal(audio.currentTime,19);assert.equal(audio.plays,plays);assert.equal(audio.loads,loads);
''')
