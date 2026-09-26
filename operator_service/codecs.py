"""Pure Twilio media helpers: base64 framing, track checks, and μ-law mixing.

Nothing here awaits anything, so the router's framing and mixing rules can be
tested without Twilio, a provider, or a socket. Frame size (160 bytes) and
pacing (20 ms) are this bridge's own design choices; base64 raw μ-law audio is
Twilio's wire format, and no message ever carries a WAV header.
"""

from __future__ import annotations

import base64
import binascii
import math
import struct

from voice_stack.audio import FRAME_BYTES, SAMPLE_RATE, SILENCE
from voice_stack.audio import iter_frames, pcm16_to_ulaw, ulaw_to_pcm16

MEDIA_ENCODING = "audio/x-mulaw"
INBOUND_TRACKS = frozenset({"inbound", "inbound_track"})
OUTBOUND_TRACKS = frozenset({"outbound", "outbound_track"})
MAX_PAYLOAD_BYTES = 8_000
MAX_PAYLOAD_CHARS = MAX_PAYLOAD_BYTES * 4 // 3 + 4
MAX_TONE_MS = 5_000
TONE_AMPLITUDE = 8_000
SILENCE_FRAME = bytes([SILENCE]) * FRAME_BYTES
# μ-law has no true zero: 0xff is +0 and 0x7f is −0.
_SILENT_CODES = frozenset({SILENCE, 0x7F})


def valid_media_format(media_format) -> bool:
    """Twilio must hand us mono 8 kHz μ-law, where one frame is exactly 20 ms."""
    if not isinstance(media_format, dict):
        return False
    sample_rate = media_format.get("sampleRate")
    return (media_format.get("encoding") == MEDIA_ENCODING
            and media_format.get("channels") == 1
            and not isinstance(sample_rate, bool)
            and sample_rate == SAMPLE_RATE)


def decode_payload(payload) -> bytes:
    """Decode one ``media.payload``; anything malformed raises instead of flowing."""
    if not isinstance(payload, str) or len(payload) > MAX_PAYLOAD_CHARS or len(payload) % 4:
        raise ValueError("Media payload must be bounded, padded base64 text.")
    try:
        audio = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("Media payload is not valid base64.") from None
    if len(audio) > MAX_PAYLOAD_BYTES:
        raise ValueError("Media payload is longer than one Twilio message.")
    return audio


def inbound_frames(message) -> tuple[bytes, ...]:
    """Normalize one Twilio ``media`` event into complete 20 ms μ-law frames."""
    media = message.get("media") if isinstance(message, dict) else None
    if not isinstance(media, dict):
        raise ValueError("Media event has no media object.")
    track = media.get("track", "inbound")
    if not isinstance(track, str) or track in OUTBOUND_TRACKS or (track and track not in INBOUND_TRACKS):
        # Only the speaker's own input is routed: forwarding the far end's
        # playback back to it would build a feedback loop.
        raise ValueError("Unexpected media track.")
    audio = decode_payload(media.get("payload"))
    # Twilio's first packet can carry an empty payload, and a jittered packet
    # can carry more than one frame.
    return () if not audio else tuple(iter_frames(audio))


def media_message(destination_stream_sid: str, frame: bytes) -> dict:
    """One outbound media message for the destination leg's own Stream SID."""
    return {"event": "media", "streamSid": destination_stream_sid,
            "media": {"payload": base64.b64encode(frame).decode("ascii")}}


def clear_message(stream_sid: str) -> dict:
    """Ask Twilio to drop audio it has already buffered for that stream."""
    return {"event": "clear", "streamSid": stream_sid}


def mark_message(stream_sid: str, name: str) -> dict:
    """Label the end of a spoken phrase so playback can be confirmed later."""
    return {"event": "mark", "streamSid": stream_sid, "mark": {"name": name}}


def is_silence(frame: bytes) -> bool:
    return bool(frame) and not set(frame) - _SILENT_CODES


def mix_ulaw(first: bytes, second: bytes) -> bytes:
    """Mix two μ-law frames of equal length, halving each only when both speak.

    The owner's monitor mix decodes both directions to signed 16-bit PCM,
    halves them, adds with saturation, and re-encodes. Two simultaneous
    streams are never concatenated: that would double playback duration.
    """
    if len(first) != len(second):
        raise ValueError("Mixed frames must be the same length.")
    if is_silence(second):
        return first
    if is_silence(first):
        return second
    mixed = bytearray()
    for (left,), (right,) in zip(struct.iter_unpack("<h", ulaw_to_pcm16(first)),
                                 struct.iter_unpack("<h", ulaw_to_pcm16(second))):
        total = (left >> 1) + (right >> 1)
        total = 32767 if total > 32767 else (-32768 if total < -32768 else total)
        mixed += struct.pack("<h", total)
    return pcm16_to_ulaw(bytes(mixed))


def tone_ulaw(frequency: float = 440.0, milliseconds: int = 250,
              amplitude: float = TONE_AMPLITUDE) -> bytes:
    """A pure sine as μ-law, for private cues that need no provider or file."""
    if not 100 <= frequency <= 3400:
        raise ValueError("Cue tone frequency must be between 100 and 3400 Hz.")
    if type(milliseconds) is not int or not 1 <= milliseconds <= MAX_TONE_MS:
        raise ValueError("Cue tone duration must be between 1 and 5000 milliseconds.")
    if not 0 <= amplitude <= 16000:
        raise ValueError("Cue tone amplitude must be between 0 and 16000.")
    samples = SAMPLE_RATE * milliseconds // 1000
    step = 2 * math.pi * frequency / SAMPLE_RATE
    pcm = b"".join(struct.pack("<h", int(amplitude * math.sin(step * index)))
                   for index in range(samples))
    return pcm16_to_ulaw(pcm)


def silence_ulaw(milliseconds: int) -> bytes:
    if type(milliseconds) is not int or not 0 <= milliseconds <= MAX_TONE_MS:
        raise ValueError("Silence must be between 0 and 5000 milliseconds.")
    return bytes([SILENCE]) * (SAMPLE_RATE * milliseconds // 1000)


def ringback_pattern() -> bytes:
    """The owner's private cue while the remote leg is dialing.

    Exactly 210 frames (4.2 seconds) so the cue loop can pace it frame by frame.
    """
    return (tone_ulaw(440.0, 300) + silence_ulaw(200)
            + tone_ulaw(440.0, 300) + silence_ulaw(3400))
