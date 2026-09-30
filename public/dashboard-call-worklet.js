/* Mono microphone capture, resampled from the actual device rate to G.711 μ-law. */
class DashboardCallCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.inputPerSample = sampleRate / 8000;
    this.remaining = this.inputPerSample;
    this.sum = 0;
    this.frame = new Uint8Array(160);
    this.offset = 0;
  }

  encode(value) {
    let sample = Math.round(Math.max(-1, Math.min(1, value)) * 32768);
    const sign = sample < 0 ? 0x80 : 0;
    sample = Math.min(32635, Math.abs(sample)) + 132;
    let exponent = 7;
    for (let mask = 0x4000; exponent > 0 && !(sample & mask); mask >>= 1) exponent--;
    return ~(sign | (exponent << 4) | ((sample >> (exponent + 3)) & 0x0f)) & 0xff;
  }

  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input) return true;
    // Fractional weights keep both 44.1 kHz and 48 kHz devices at exactly 8 kHz.
    for (const value of input) {
      let weight = 1;
      while (weight > 1e-8) {
        const used = Math.min(weight, this.remaining);
        this.sum += value * used;
        this.remaining -= used;
        weight -= used;
        if (this.remaining < 1e-8) {
          this.frame[this.offset++] = this.encode(this.sum / this.inputPerSample);
          this.remaining = this.inputPerSample;
          this.sum = 0;
          if (this.offset === 160) {
            this.port.postMessage(this.frame.buffer, [this.frame.buffer]);
            this.frame = new Uint8Array(160);
            this.offset = 0;
          }
        }
      }
    }
    return true;
  }
}
registerProcessor("dashboard-call-capture", DashboardCallCapture);
