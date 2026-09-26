"""Pure-Python G.711 mu-law helpers for 8 kHz Twilio audio.

No ``audioop`` (removed in Python 3.13), no NumPy and no FFmpeg: the decode
table is built once at import and only short frame-sized buffers are converted.
ElevenLabs ``ulaw_8000`` output is one byte per sample, so a 160-byte frame is
exactly one 20 ms Twilio ``media.payload`` after base64 encoding.
"""

from __future__ import annotations

import array
import math
from pathlib import Path
import struct
import sys
import wave

SAMPLE_RATE = 8000
CHANNELS = 1
SAMPLE_WIDTH = 2
FRAME_SAMPLES = 160          # 20 ms at 8 kHz: one Twilio media frame.
FRAME_BYTES = FRAME_SAMPLES  # Mu-law stores one byte per sample.
FRAME_MS = 20
SILENCE = 0xFF
_BIAS = 0x84
_CLIP = 32635
_MASK = 0x40


def _decode_byte(value: int) -> int:
    """One mu-law code to signed 16-bit PCM, using the standard G.711 curve."""
    code = ~value & 0xFF
    exponent = (code >> 4) & 0x07
    magnitude = ((code & 0x0F) << 3) + _BIAS
    magnitude <<= exponent
    sample = magnitude - _BIAS
    return -sample if code & 0x80 else sample


def _encode_sample(sample: int) -> int:
    """Signed 16-bit PCM to one mu-law code."""
    sign = 0x80 if sample < 0 else 0
    magnitude = -sample if sample < 0 else sample
    if magnitude > _CLIP:
        magnitude = _CLIP
    magnitude += _BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not magnitude & mask:
        exponent -= 1
        mask >>= 1
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


_ULAW_TO_PCM = tuple(_decode_byte(value) for value in range(256))


def _as_bytes(data, name: str) -> bytes:
    if not isinstance(data, bytes):
        raise ValueError(f"{name} must be bytes.")
    return data


def ulaw_to_pcm16(data: bytes) -> bytes:
    """Expand mu-law bytes to little-endian signed 16-bit PCM."""
    samples = array.array("h", (_ULAW_TO_PCM[value] for value in _as_bytes(data, "Mu-law audio")))
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


def pcm16_to_ulaw(data: bytes) -> bytes:
    """Compress little-endian signed 16-bit PCM to mu-law bytes."""
    pcm = _as_bytes(data, "PCM audio")
    if len(pcm) % SAMPLE_WIDTH:
        raise ValueError("PCM must contain whole 16-bit samples.")
    return bytes(_encode_sample(sample) for (sample,) in struct.iter_unpack("<h", pcm))


def iter_frames(data: bytes, *, pad: bool = True):
    """Yield fixed 160-byte frames suitable for Twilio ``media.payload``.

    A trailing partial frame is padded with mu-law silence so every message is
    a full 20 ms. Set ``pad=False`` to keep the short frame as-is.

    The buffer is validated before the first frame is handed out, so a bad
    argument fails at the call site instead of midway through a live stream.
    """
    audio = _as_bytes(data, "Mu-law audio")
    return _iter_frames(audio, pad)


def _iter_frames(audio: bytes, pad: bool):
    complete = len(audio) // FRAME_BYTES
    for index in range(complete):
        start = index * FRAME_BYTES
        yield audio[start:start + FRAME_BYTES]
    remainder = len(audio) - complete * FRAME_BYTES
    if remainder:
        tail = audio[complete * FRAME_BYTES:]
        yield tail + bytes([SILENCE]) * (FRAME_BYTES - remainder) if pad else tail


def frame_count(data: bytes) -> int:
    """Number of frames ``iter_frames`` yields, including a padded tail."""
    return math.ceil(len(_as_bytes(data, "Mu-law audio")) / FRAME_BYTES)


def duration_ms(data: bytes) -> int:
    """Playback duration of mu-law audio at 8 kHz."""
    return len(_as_bytes(data, "Mu-law audio")) * 1000 // SAMPLE_RATE


def write_ulaw_wav(path, data: bytes, sample_rate: int = SAMPLE_RATE) -> Path:
    """Write mu-law audio as a playable mono PCM16 WAV for local listening."""
    if type(sample_rate) is not int or sample_rate != SAMPLE_RATE:
        raise ValueError("Voice-layer audio is 8000 Hz mono.")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(destination), "wb") as audio:
        audio.setparams((CHANNELS, SAMPLE_WIDTH, sample_rate, 0, "NONE", "not compressed"))
        audio.writeframes(ulaw_to_pcm16(_as_bytes(data, "Mu-law audio")))
    return destination