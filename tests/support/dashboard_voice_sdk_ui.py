"""Shared fixtures and fakes for focused integration checks."""

import shutil


import subprocess


import pytest


from support.dashboard_browser_audio_ui import HARNESS, SCRIPT


SDK_HARNESS = HARNESS + r"""
const timers = new Map(); let timerId = 0;
const setTimeout = (callback, ms) => {timers.set(++timerId, {callback, ms}); return timerId;};
const clearTimeout = id => timers.delete(id);
class Emitter {
  constructor(){this.listeners={};}
  on(name, handler){(this.listeners[name] ||= []).push(handler);return this;}
  emit(name, ...args){for(const handler of [...(this.listeners[name] || [])])handler(...args);}
  removeAllListeners(){this.listeners={};}
}
class FakeCall extends Emitter {
  constructor(){super();this.state='pending';this.codec='opus';this.digits=[];this.disconnects=0;}
  status(){return this.state;}
  accept(){this.state='open';this.emit('accept');}
  disconnect(){this.disconnects++;this.state='closed';this.emit('disconnect');}
  sendDigits(value){this.digits.push(value);}
  reject(){this.rejected=true;}
}
const devices=[], calls=[];let connectGate=null, connectFailure=false;
class Device extends Emitter {
  constructor(token, options){super();this.token=token;this.options=options;this.connects=[];this.destroyed=false;devices.push(this);}
  async connect(options){this.connects.push(options);if(connectFailure)throw Error('jwt-secret');
    const call=new FakeCall();calls.push(call);if(connectGate)await connectGate;return call;}
  register(){throw Error('Outgoing browser must not register for incoming calls');}
  destroy(){this.destroyed=true;this.emit('error',Error('jwt-secret'));}
}
window.Twilio={Device};
window.localStorage={getItem(){throw Error('Token persistence forbidden');},setItem(){throw Error('Token persistence forbidden');}};
window.sessionStorage=window.localStorage;
const sdkCredentials={transport:'twilio-voice-sdk',token:'jwt-secret',params:{SessionId:sid,Token:'one-time-nonce'}};
const prepareSdk=options=>window.DashboardBrowserAudio.prepare({transport:'twilio-voice-sdk',...options});
const sdkAttach=async audio=>{const pending=audio.attach(sdkCredentials,sid);await tick();calls.at(-1).accept();await pending;};
"""


def run_sdk(checks, *, before=""):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for browser SDK tests")
    result = subprocess.run(
        [node, "-e", SDK_HARNESS + before + "\n" + SCRIPT.read_text()
         + "\n(async()=>{\n" + checks
         + "\n})().catch(error=>{console.error(error);process.exitCode=1;});"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
