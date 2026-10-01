/* Browser audio for authenticated dashboard calls. Tokens stay in this tab. */
(() => {
  "use strict";
  const READY_MS = 10000, MAX_BACKLOG = 1, MAX_SEND_BYTES = 64000;

  function microphoneError(failure) {
    if (failure?.name === "NotAllowedError" || failure?.name === "SecurityError") {
      return new Error("Allow microphone access for this site, then try the call again.");
    }
    if (failure?.name === "NotFoundError") return new Error("Connect a microphone, then try the call again.");
    if (failure?.name === "NotReadableError") return new Error("Your microphone is unavailable. Close other apps using it, then try again.");
    return new Error("Browser audio could not start. Check your microphone and try again.");
  }

  const cancelled = () => new Error("Browser calling was cancelled. Try again.");

  const QUALITY_METRICS = {
    jitter: [0, 10000], rtt: [0, 60000], mos: [1, 5],
    packetsLostFraction: [0, 100], packetsLost: [0, 1000000],
    packetsReceived: [0, 1000000], packetsSent: [0, 1000000],
    audioInputLevel: [0, 32767], audioOutputLevel: [0, 32767]
  };
  const QUALITY_WARNINGS = new Set([
    "high-jitter", "high-rtt", "low-mos", "high-packet-loss",
    "high-packets-lost-fraction", "constant-audio-input-level",
    "constant-audio-output-level", "low-bytes-received", "low-bytes-sent",
    "low-audio-input-level", "low-audio-output-level"
  ]);

  // SDK samples are once per second. Never forward raw SDK objects: they can
  // contain connection details. Only bounded measurements leave this tab.
  class QualityReporter {
    constructor(sessionId, stream) {
      this.sessionId = sessionId;
      this.started = performance.now();
      this.samples = []; this.warnings = []; this.sequence = 0;
      this.codec = null; this.final = false; this.sending = false;
      this.device = {};
      const agent = String(navigator.userAgent || "");
      this.device.browser = /Edg\//.test(agent) ? "edge" : /Firefox\//.test(agent) ? "firefox"
        : /Chrome\//.test(agent) ? "chrome" : /Safari\//.test(agent) ? "safari" : "other";
      this.device.platform = /Android/.test(agent) ? "android" : /iPhone|iPad|iPod/.test(agent) ? "ios"
        : /Mac/.test(agent) ? "mac" : /Windows/.test(agent) ? "windows" : /Linux/.test(agent) ? "linux" : "other";
      const version = window.Twilio?.Device?.version;
      if (typeof version === "string" && /^[0-9]{1,2}\.[0-9]{1,3}\.[0-9]{1,3}$/.test(version)) this.device.sdk_version = version;
      const tracks = stream.getAudioTracks?.() || stream.getTracks();
      this.device.audio_track_count = Math.min(16, tracks.length);
      try {
        const settings = tracks[0]?.getSettings?.() || {};
        for (const [source, target, low, high] of [["sampleRate", "sample_rate", 8000, 192000], ["channelCount", "channel_count", 1, 8]]) {
          if (Number.isInteger(settings[source]) && settings[source] >= low && settings[source] <= high) this.device[target] = settings[source];
        }
        for (const [source, target] of [["echoCancellation", "echo_cancellation"], ["noiseSuppression", "noise_suppression"], ["autoGainControl", "auto_gain_control"]]) {
          if (typeof settings[source] === "boolean") this.device[target] = settings[source];
        }
      } catch (_) {}
      this.pagehide = () => this.stop();
      if (typeof window.fetch === "function") {
        window.addEventListener?.("pagehide", this.pagehide);
        this.schedule();
      }
    }

    elapsed() {return Math.max(0, Math.min(86400000, Math.round(performance.now() - this.started)));}

    sample(value) {
      if (this.final || !value || typeof value !== "object") return;
      const sample = {elapsed_ms: this.elapsed()};
      for (const [name, [low, high]] of Object.entries(QUALITY_METRICS)) {
        if (typeof value[name] === "number" && Number.isFinite(value[name]) && value[name] >= low && value[name] <= high) sample[name] = value[name];
      }
      if (Object.keys(sample).length > 1) {
        this.samples.push(sample); this.samples = this.samples.slice(-15);
      }
      if (["opus", "pcmu"].includes(value.codecName)) this.codec = value.codecName;
    }

    warning(name, cleared) {
      if (this.final || !QUALITY_WARNINGS.has(name)) return;
      this.warnings.push({elapsed_ms: this.elapsed(), name, cleared});
      this.warnings = this.warnings.slice(-16);
    }

    schedule() {
      if (this.final) return;
      this.timer = setTimeout(() => {this.timer = null; this.flush(); this.schedule();}, 10000);
    }

    flush() {
      if (typeof window.fetch !== "function" || this.sending || this.sentFinal) return;
      const payload = {version: 1, sequence: ++this.sequence, final: this.final,
        elapsed_ms: this.elapsed(), codec: this.codec, device: this.device,
        samples: this.samples.splice(0), warnings: this.warnings.splice(0)};
      this.sending = true;
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 4000);
      // keepalive permits the final bounded report to survive page navigation.
      // Diagnostics failures never reject attach(), stop() or microphone events.
      Promise.resolve().then(() => window.fetch(`/api/sessions/${this.sessionId}/browser-quality`, {
        method: "POST", credentials: "same-origin", cache: "no-store", keepalive: true,
        headers: {"Content-Type": "application/json", "X-Agent-Request": "1"},
        body: JSON.stringify(payload), signal: controller.signal
      })).catch(() => {}).finally(() => {
        clearTimeout(timeout); this.sending = false;
        if (payload.final) this.sentFinal = true;
        else if (this.final) this.flush();
      });
    }

    stop() {
      if (this.final) return;
      this.final = true;
      clearTimeout(this.timer); this.timer = null;
      window.removeEventListener?.("pagehide", this.pagehide);
      this.flush();
    }
  }

  // Browser permission prompts cannot be dismissed programmatically. A cancelled
  // request releases any stream that arrives later and never proceeds to dial.
  function requestMicrophone(signal) {
    if (signal?.aborted) return Promise.reject(cancelled());
    const microphone = navigator.mediaDevices.getUserMedia({audio: {
      echoCancellation: true, noiseSuppression: true, autoGainControl: true
    }, video: false});
    return new Promise((resolve, reject) => {
      let settled = false;
      const abort = () => {if (!settled) {settled = true; reject(cancelled());}};
      signal?.addEventListener("abort", abort, {once: true});
      microphone.then(stream => {
        signal?.removeEventListener("abort", abort);
        if (settled || signal?.aborted) {
          for (const track of stream.getTracks()) track.stop();
          if (!settled) reject(cancelled());
          return;
        }
        settled = true; resolve(stream);
      }, failure => {
        signal?.removeEventListener("abort", abort);
        if (!settled) {settled = true; reject(microphoneError(failure));}
      });
    });
  }

  class VoiceSdkAudio {
    constructor(stream) {
      this.stream = stream;
      this.transport = "twilio-voice-sdk";
      this.sessionId = null;
      this.connected = false;
      this.codec = null;
      this.stopped = false;
      this.device = null;
      this.call = null;
      this.quality = null;
      this.cancelAttach = null;
      this.onDisconnect = null;
      this.microphoneEnded = () => this.fail("The microphone disconnected or its permission was revoked. The call is ending.");
      for (const track of stream.getTracks()) track.addEventListener("ended", this.microphoneEnded);
    }

    async attach(credentials, sessionId) {
      if (this.stopped) throw cancelled();
      if (this.device) throw new Error("Browser audio is already connecting.");
      const params = credentials?.params;
      if (credentials?.transport !== this.transport || !/^[0-9a-f]{32}$/.test(sessionId) ||
          typeof credentials.token !== "string" || !credentials.token || credentials.token.length > 16384 ||
          !params || Object.keys(params).sort().join(",") !== "SessionId,Token" || params.SessionId !== sessionId ||
          typeof params.Token !== "string" || !params.Token || params.Token.length > 2048) {
        throw new Error("Invalid browser audio response.");
      }
      this.sessionId = sessionId;
      // Optional reporting must never prevent the browser from placing a call.
      try {this.quality = new QualityReporter(sessionId, this.stream);} catch (_) {}
      await new Promise((resolve, reject) => {
        let settled = false;
        const finish = failure => {
          if (settled) return;
          settled = true; clearTimeout(timeout); this.cancelAttach = null;
          if (failure) {this.stop(); reject(failure);} else resolve();
        };
        const timeout = setTimeout(() => finish(new Error("Browser audio did not connect. Try again.")), READY_MS);
        this.cancelAttach = finish;
        const failed = message => {
          if (this.stopped) return;
          if (!settled) finish(new Error(message)); else this.fail(message);
        };
        try {
          const device = this.device = new window.Twilio.Device(credentials.token, {
            codecPreferences: ["opus", "pcmu"], allowIncomingWhileBusy: false,
            closeProtection: false, logLevel: "silent",
            // Reuse the microphone granted in the click gesture. The SDK owns
            // encoding/playback; this path never uses the 8 kHz capture worklet.
            getUserMedia: () => this.stopped ? Promise.reject(cancelled()) : Promise.resolve(this.stream)
          });
          device.on("incoming", call => call.reject());
          device.on("error", () => failed("Browser audio disconnected. The call is ending."));
          // Outgoing only: register() would enable incoming SDK calls.
          Promise.resolve(device.connect({params: {SessionId: params.SessionId, Token: params.Token}})).then(call => {
            if (this.stopped) {try {call.disconnect();} catch (_) {} return;}
            this.call = call;
            call.on("sample", sample => {try {this.quality?.sample(sample);} catch (_) {}});
            call.on("warning", name => {try {this.quality?.warning(name, false);} catch (_) {}});
            call.on("warning-cleared", name => {try {this.quality?.warning(name, true);} catch (_) {}});
            const accepted = () => {
              if (this.stopped || this.connected) return;
              this.codec = ["opus", "pcmu"].includes(call.codec) ? call.codec : null;
              if (this.quality && this.codec) this.quality.codec = this.codec;
              this.connected = true; finish();
            };
            call.on("accept", accepted);
            call.on("disconnect", () => failed("The call ended."));
            call.on("cancel", () => failed("The call ended before browser audio connected."));
            call.on("reject", () => failed("Browser audio could not connect. Try again."));
            call.on("error", () => failed("Browser audio disconnected. The call is ending."));
            if (call.status() === "open") accepted();
          }).catch(() => failed("Browser audio could not connect. Check your connection and try again."));
        } catch (_) {finish(new Error("Browser audio could not start. Refresh this page and try again."));}
      });
    }

    sendDigits(digit) {
      if (!this.connected || this.stopped || !/^[0-9*#]$/.test(digit)) return;
      this.call.sendDigits(digit);
    }

    fail(message) {
      if (this.stopped) return;
      const notify = this.onDisconnect;
      this.stop();
      if (notify) notify(message);
    }

    stop() {
      if (this.stopped) return;
      try {this.quality?.stop();} catch (_) {}
      this.stopped = true; this.connected = false; this.codec = null;
      const cancel = this.cancelAttach; this.cancelAttach = null;
      if (cancel) cancel(cancelled());
      for (const track of this.stream.getTracks()) {
        track.removeEventListener("ended", this.microphoneEnded); track.stop();
      }
      const call = this.call, device = this.device;
      this.call = this.device = null;
      try {call?.disconnect();} catch (_) {}
      try {device?.destroy();} catch (_) {}
      call?.removeAllListeners(); device?.removeAllListeners();
    }
  }

  class BrowserAudio {
    constructor(context, stream, capture, source, gain) {
      Object.assign(this, {context, stream, capture, source, gain});
      this.sessionId = null;
      this.connected = false;
      this.stopped = false;
      this.sources = new Set();
      this.marks = [];
      this.playUntil = 0;
      this.timestamp = 0;
      this.socket = null;
      this.cancelAttach = null;
      this.onDisconnect = null;
      this.microphoneEnded = () => this.fail("The microphone disconnected or its permission was revoked. The call is ending.");
      this.contextChanged = () => {
        if (!this.stopped && context.state !== "running") this.fail("Browser audio was interrupted. The call is ending. Start another call.");
      };
      for (const track of stream.getTracks()) track.addEventListener("ended", this.microphoneEnded);
      context.addEventListener("statechange", this.contextChanged);
      capture.port.onmessage = event => {
        if (!this.connected || this.stopped || this.socket?.readyState !== 1) return;
        // A stalled transport ends the call instead of accumulating old speech.
        if (this.socket.bufferedAmount > MAX_SEND_BYTES) {this.fail("The audio connection stalled. Start another call."); return;}
        let bytes = "";
        for (const value of new Uint8Array(event.data)) bytes += String.fromCharCode(value);
        this.send({event: "media", media: {track: "inbound", timestamp: String(this.timestamp), payload: btoa(bytes)}});
        this.timestamp += 20;
      };
    }

    send(value) {
      if (this.socket?.readyState === 1) this.socket.send(JSON.stringify(value));
    }

    async attach(credentials, sessionId) {
      if (this.stopped) throw new Error("Browser audio has stopped. Try again.");
      const url = new URL(credentials.url, window.location.href);
      if (url.origin !== window.location.origin || !/^[0-9a-f]{32}$/.test(sessionId) || url.pathname !== `/browser-media/${sessionId}/` ||
          typeof credentials.token !== "string" || !credentials.token) throw new Error("Invalid browser audio response.");
      url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
      this.sessionId = sessionId;
      await new Promise((resolve, reject) => {
        let ready = false, settled = false;
        const socket = this.socket = new WebSocket(url.href);
        const timeout = setTimeout(() => finish(new Error("Browser audio did not connect. Try again.")), READY_MS);
        const finish = failure => {
          if (settled) return;
          settled = true; clearTimeout(timeout); this.cancelAttach = null;
          if (failure) {this.stop(); reject(failure);} else resolve();
        };
        this.cancelAttach = finish;
        socket.onopen = () => this.send({event: "start", token: credentials.token});
        socket.onmessage = event => {
          try {
            const value = JSON.parse(event.data);
            if (value.event === "ready" && !ready) {
              ready = true; this.connected = true; this.timestamp = 0; finish();
            } else if (ready) this.receive(value);
            else if (value.event === "error") finish(new Error(value.message || "Browser audio could not connect."));
          } catch (_) {
            const message = "The browser received invalid call audio. Start another call.";
            if (!ready) finish(new Error(message)); else this.fail(message);
          }
        };
        socket.onerror = () => {if (!ready) finish(new Error("Browser audio could not connect. Check your connection and try again."));};
        socket.onclose = () => {
          if (!ready) finish(new Error("Browser audio could not connect. The call may already be open in another tab."));
          else if (!this.stopped) this.fail("Browser audio disconnected. The call is ending.");
        };
      });
    }

    receive(value) {
      if (value.event === "media") {
        const payload = value.media?.payload;
        if (typeof payload !== "string" || payload.length > 32000) throw new Error("Invalid audio frame");
        const bytes = atob(payload);
        if (!bytes.length || bytes.length > 8000) throw new Error("Invalid audio frame");
        // Keep conversational latency bounded if the tab or speaker was stalled.
        if (this.playUntil - this.context.currentTime + bytes.length / 8000 > MAX_BACKLOG) this.clear();
        const buffer = this.context.createBuffer(1, bytes.length, 8000), channel = buffer.getChannelData(0);
        for (let i = 0; i < bytes.length; i++) {
          const sample = (~bytes.charCodeAt(i)) & 0xff;
          const magnitude = (((sample & 0x0f) << 3) + 132) << ((sample & 0x70) >> 4);
          channel[i] = ((sample & 0x80) ? 132 - magnitude : magnitude - 132) / 32768;
        }
        const source = this.context.createBufferSource();
        source.buffer = buffer; source.connect(this.context.destination);
        const start = Math.max(this.context.currentTime + 0.01, this.playUntil);
        this.playUntil = start + buffer.duration;
        this.sources.add(source);
        source.onended = () => {this.sources.delete(source); source.disconnect(); this.flushMarks();};
        source.start(start);
      } else if (value.event === "mark" && typeof value.mark?.name === "string") {
        this.marks.push({name: value.mark.name, due: this.playUntil});
        this.flushMarks();
      } else if (value.event === "clear") this.clear();
      else if (value.event === "stop") this.fail("The call ended.");
      else if (value.event === "error") this.fail(value.message || "Browser audio disconnected. The call is ending.");
    }

    flushMarks() {
      clearTimeout(this.markTimer);
      while (this.marks.length && this.marks[0].due <= this.context.currentTime + 0.001) {
        this.send({event: "mark", mark: {name: this.marks.shift().name}});
      }
      if (this.marks.length && !this.stopped) {
        this.markTimer = setTimeout(() => this.flushMarks(), Math.max(20, (this.marks[0].due - this.context.currentTime) * 1000));
      }
    }

    clear() {
      for (const source of this.sources) {try {source.stop(); source.disconnect();} catch (_) {}}
      this.sources.clear(); this.playUntil = this.context.currentTime;
      clearTimeout(this.markTimer);
      for (const mark of this.marks) this.send({event: "mark", mark: {name: mark.name}});
      this.marks = [];
    }

    fail(message) {
      const notify = this.onDisconnect;
      this.stop();
      if (notify) notify(message);
    }

    stop() {
      if (this.stopped) return;
      this.stopped = true; this.connected = false;
      for (const track of this.stream.getTracks()) track.removeEventListener("ended", this.microphoneEnded);
      this.context.removeEventListener("statechange", this.contextChanged);
      const cancel = this.cancelAttach;
      this.cancelAttach = null;
      if (cancel) cancel(new Error("Browser calling was cancelled. Try again."));
      this.clear();
      this.capture.port.onmessage = null;
      for (const item of [this.capture, this.source, this.gain]) {try {item.disconnect();} catch (_) {}}
      for (const track of this.stream.getTracks()) track.stop();
      if (this.socket) {this.socket.onclose = null; this.socket.onerror = null; this.socket.onmessage = null; this.socket.onopen = null; this.socket.close();}
      this.context.close().catch(() => {});
    }
  }

  async function prepare(options = {}) {
    if (options.transport === "twilio-voice-sdk") {
      if (!window.isSecureContext) {
        throw new Error("Browser calling needs HTTPS. Open the secure Phoney dashboard and try again.");
      }
      if (typeof window.Twilio?.Device !== "function") {
        throw new Error("The calling library did not load. Refresh this page and try again.");
      }
      if (typeof navigator.mediaDevices?.getUserMedia !== "function") {
        throw new Error("This browser cannot access the microphone. Open the secure dashboard in a browser with microphone support.");
      }
      const stream = await requestMicrophone(options.signal);
      if (!stream.getTracks().length || stream.getTracks().some(track => track.readyState !== "live")) {
        for (const track of stream.getTracks()) track.stop();
        throw new Error("Browser audio was interrupted before the call started. Try again.");
      }
      return new VoiceSdkAudio(stream);
    }
    const Audio = window.AudioContext || window.webkitAudioContext;
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || !Audio || typeof AudioWorkletNode !== "function" || typeof WebSocket !== "function") {
      throw new Error("Browser calling needs HTTPS and a browser with microphone support. Open this site in current Chrome, Edge, Firefox, or Safari.");
    }
    let context, stream, source, capture, gain;
    try {
      context = new Audio({latencyHint: "interactive"});
      // Resume and request permission in the click gesture before waiting for either.
      const resume = context.resume();
      resume.catch(() => {});
      const microphone = navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true}, video: false});
      stream = await microphone;
      await resume;
      if (!context.audioWorklet) throw new Error("AudioWorklet unavailable");
      await context.audioWorklet.addModule("/assets/dashboard-call-worklet.js");
      source = context.createMediaStreamSource(stream);
      capture = new AudioWorkletNode(context, "dashboard-call-capture", {numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1]});
      gain = context.createGain(); gain.gain.value = 0;
      source.connect(capture); capture.connect(gain); gain.connect(context.destination);
      // Device state can change while the worklet module is loading, before
      // the live-call listeners exist. Do not dial with an already-dead source.
      if (context.state !== "running" || !stream.getTracks().length ||
          stream.getTracks().some(track => track.readyState !== "live")) {
        throw new Error("Browser audio was interrupted during setup");
      }
      return new BrowserAudio(context, stream, capture, source, gain);
    } catch (failure) {
      for (const item of [capture, source, gain]) {try {item?.disconnect();} catch (_) {}}
      for (const track of stream?.getTracks() || []) track.stop();
      if (context) await context.close().catch(() => {});
      throw microphoneError(failure);
    }
  }

  window.DashboardBrowserAudio = {prepare};
})();
