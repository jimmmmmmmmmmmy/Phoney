"""Focused product and boundary checks; test helpers live in support."""

from support.dashboard_browser_audio_ui import (run_audio)


def test_browser_handshake_audio_frames_playback_marks_and_cleanup():
    run_audio(r"""
const audio=await window.DashboardBrowserAudio.prepare();
assert.deepEqual(events,['resume','microphone','worklet']);
captures[0].port.onmessage({data:new Uint8Array(160).fill(255).buffer});
assert.equal(sockets.length,0,'Preparing never opens a socket or sends audio');
const socket=await attach(audio);
assert.equal(socket.url,`wss://phoney.example/browser-media/${sid}/`);
assert.equal(socket.url.includes(credentials.token),false,'Tokens must not enter URL logs');
assert.deepEqual(socket.sent,[{event:'start',token:credentials.token}]);
captures[0].port.onmessage({data:new Uint8Array(160).fill(255).buffer});
captures[0].port.onmessage({data:new Uint8Array(160).fill(255).buffer});
assert.equal(socket.sent[1].media.track,'inbound');
assert.equal(socket.sent[1].media.timestamp,'0');
assert.equal(socket.sent[2].media.timestamp,'20');
assert.equal(atob(socket.sent[1].media.payload).length,160);
socket.receive({event:'media',media:{payload:btoa(String.fromCharCode(...new Uint8Array(160).fill(255)))}});
assert.equal(played[0].buffer.sampleRate,8000);
assert.equal(played[0].buffer.length,160);
assert.ok(played[0].buffer.getChannelData(0).every(value=>value===0));
socket.receive({event:'mark',mark:{name:'speech-1'}});
assert.equal(socket.sent.filter(value=>value.event==='mark').length,0,'Mark must wait for actual playback');
contexts[0].currentTime=.04;played[0].onended();
assert.deepEqual(socket.sent.at(-1),{event:'mark',mark:{name:'speech-1'}});
socket.receive({event:'media',media:{payload:btoa('\xff'.repeat(160))}});
socket.receive({event:'mark',mark:{name:'speech-cleared'}});
socket.receive({event:'clear'});
assert.equal(played[1].stopped,true);
assert.deepEqual(socket.sent.at(-1),{event:'mark',mark:{name:'speech-cleared'}});
audio.stop();audio.stop();
assert.equal(track.stopped,true);assert.equal(contexts[0].closed,true);assert.equal(socket.closed,true);
assert.equal(captures[0].port.onmessage,null);
assert.equal(events.filter(value=>value==='track-stop').length,1,'Cleanup must be idempotent');
""")
