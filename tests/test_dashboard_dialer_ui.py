"""Focused product and boundary checks; test helpers live in support."""

from support.dashboard_dialer_ui import (BROWSER_SETUP, SDK_BROWSER_SETUP, run_dialer)


def test_sdk_flag_uses_browser_audio_for_any_approved_destination_without_phone_callback():
    run_dialer(r"""
$('dialer-button').click();dial(destination);
assert.equal($('dialer-start').getAttribute('aria-label'),'Call from this browser');
assert.equal(audioEvents.length,0,'Entering a destination does not request microphone permission');
await submit('dialer-call-form');
assert.equal(preparedOptions[0].transport,'twilio-voice-sdk');
assert.ok(preparedOptions[0].signal instanceof AbortSignal);
assert.deepEqual(JSON.parse(outbound()[0].body),{to:destination,goal:'',browser_audio:true});
assert.equal(audioEvents[0],'prepare');assert.equal(audioEvents[2],'outbound');
callingConfig.active_session.phase='connected';window.dispatch('focus');await settle();
assert.match(text($('dialer-status')),/Browser audio connected/);
assert.match(text($('dialer-status')),/Browser audio connected \(Opus\)/);
assert.equal($('dialer-live-keypad').hidden,false);
assert.equal($('dialer-agent-controls').hidden,false);
assert.equal(text($('dialer-dialog')).includes('private-token'),false);
$('dialer-minimize').click();assert.equal(audioEvents[1].stopped,false);
$('dialer-restore').click();await settle();
$('dialer-end').click();await settle();
assert.equal(audioEvents[1].stopped,true);assert.equal(outbound().length,1);
""", before=SDK_BROWSER_SETUP)


def test_sdk_connected_status_displays_actual_pcmu_fallback_without_claiming_opus():
    run_dialer(r"""
await connectBrowser();audioEvents[1].codec='pcmu';window.dispatch('focus');await settle();
assert.match(text($('dialer-status')),/Browser audio connected \(PCMU\)/);
assert.equal(text($('dialer-status')).includes('Opus'),false);
""", before=SDK_BROWSER_SETUP)


def test_sdk_phone_tones_and_authenticated_agent_commands_use_separate_transports():
    run_dialer(r"""
await connectBrowser();
const writesBefore=writes().length;
$('dialer-live-key-5').click();$('dialer-live-key-star').click();$('dialer-live-key-hash').click();
assert.deepEqual(sentDigits,['5','*','#']);assert.equal(writes().length,writesBefore);
$('dialer-agent-1').click();$('dialer-agent-1').click();await settle();
const takeover=writes().filter(item=>item.url.endsWith('/takeover'));
assert.equal(takeover.length,1,'Double click cannot duplicate agent takeover');
assert.deepEqual(JSON.parse(takeover[0].body),{slot:'1'});
assert.equal(takeover[0].credentials,'same-origin');assert.equal(takeover[0].headers['X-Agent-Request'],'1');
assert.match(text($('dialer-status')),/AI agent speaking/);
assert.equal($('dialer-agent-human').disabled,false);
$('dialer-agent-human').click();await settle();
const release=writes().find(item=>item.url.endsWith('/mode'));
assert.deepEqual(JSON.parse(release.body),{mode:'human'});
assert.deepEqual(sentDigits,['5','*','#'],'Agent commands never send hash/star escape digits into conference');
assert.equal(audioEvents[1].stopped,false);assert.equal(outbound().length,1);
""", before=SDK_BROWSER_SETUP)


def test_sdk_disconnect_closes_human_session_once_without_auto_redial():
    run_dialer(r"""
await connectBrowser();const audio=audioEvents[1];
audio.stop();audio.onDisconnect('The call ended.');await settle();
assert.equal(writes().filter(item=>item.url.endsWith('/end')).length,1);
assert.equal(outbound().length,1);assert.match(text($('dialer-status')),/Call ended/);
assert.equal($('dialer-agent-controls').hidden,true);assert.equal($('dialer-live-keypad').hidden,true);
audio.onDisconnect('The call ended.');await settle();
assert.equal(writes().filter(item=>item.url.endsWith('/end')).length,1);
""", before=SDK_BROWSER_SETUP)


def test_sdk_microphone_denial_cannot_create_nonself_call_or_retry_it_automatically():
    run_dialer(r"""
$('dialer-button').click();dial(destination);await submit('dialer-call-form');
assert.deepEqual(audioEvents,['prepare']);assert.equal(outbound().length,0);
window.dispatch('focus');intervals.forEach(callback=>callback());await settle();
assert.equal(outbound().length,0);assert.match(text($('dialer-error')),/Allow microphone/);
""", before=SDK_BROWSER_SETUP + "microphoneFailure='Allow microphone access for this site, then try the call again.';")


def test_recovered_browser_call_requires_explicit_microphone_attach():
    run_dialer(r"""
assert.equal(audioEvents.length,0);
$('dialer-restore').click(); await tick();
assert.equal(audioEvents.length,0,'Restoring a recovered call must not steal its microphone/socket');
assert.equal($('dialer-resume-audio').hidden,false);
assert.match(text($('dialer-status')), /another tab/);
$('dialer-resume-audio').click(); await tick();
assert.equal(audioEvents[0],'prepare');
assert.equal(audioEvents[2],'attach');
assert.equal(requests.filter(item=>item.url==='/api/calls/outbound').length,0);
assert.equal($('dialer-resume-audio').hidden,true);
window.dispatch('pagehide');
assert.equal(audioEvents[1].stopped,true);
""", before=BROWSER_SETUP + "callingConfig.active_session={id:selfId,phase:'reserved',browser_audio:true};\n")


def test_unknown_self_call_outcome_stops_microphone_and_preserves_retry_key():
    run_dialer(r"""
$('dialer-button').click(); await tick(); dial('3125550101');
const normalHandler=browserHandler;
browserHandler=async(path,options)=>path==='/api/calls/outbound'?Promise.reject(Error('lost response')):normalHandler(path,options);
$('dialer-call-form').dispatch('submit'); await tick();
assert.equal(audioEvents[1].stopped,true);
assert.match(text($('dialer-error')), /Your microphone is off/);
const first=requests.find(item=>item.url==='/api/calls/outbound');
window.dispatch('focus'); await tick();
assert.equal(requests.filter(item=>item.url==='/api/calls/outbound').length,1);
$('dialer-call-form').dispatch('submit'); await tick();
const starts=requests.filter(item=>item.url==='/api/calls/outbound');
assert.equal(starts.length,2);
assert.equal(starts[0].headers['Idempotency-Key'],starts[1].headers['Idempotency-Key']);
assert.equal(audioEvents.filter(value=>value==='prepare').length,2);
assert.ok(audioEvents.filter(value=>typeof value==='object').every(audio=>audio.stopped));
""", before=BROWSER_SETUP)


def test_authenticated_access_loss_closes_browser_audio():
    run_dialer(r"""
$('dialer-button').click(); await tick(); dial('3125550101');
$('dialer-call-form').dispatch('submit'); await tick();
callingConfig.authenticated=false; callingConfig.public_calling=true;
window.dispatch('focus'); await tick();
assert.equal(audioEvents[1].stopped,true);
assert.equal($('dialer-resume-audio').hidden,true);
""", before=BROWSER_SETUP)


def test_public_callers_cannot_enable_sdk_or_show_agent_controls_even_with_flag_present():
    run_dialer(r"""
$('dialer-button').click();dial(destination);await submit('dialer-call-form');
assert.equal(preparedOptions.length,0);assert.equal(audioEvents.includes('prepare'),false);
assert.deepEqual(JSON.parse(outbound()[0].body),{to:destination,goal:''});
assert.equal($('dialer-agent-controls').hidden,true);assert.equal($('dialer-live-keypad').hidden,true);
""", before=SDK_BROWSER_SETUP + r"""
callingConfig.authenticated=false;callingConfig.public_calling=true;
browserHandler=async(path,options)=>{
 if(path==='/api/calls/config')return reply(callingConfig);
 if(path==='/api/calls/outbound') {
  callingConfig.active_session={id:selfId,phase:'connected'};
  return reply({session_id:selfId,phase:'connected',browser_audio:false});
 }
 return sdkBaseHandler(path,options);
};
""")
