"""Mu-law must round-trip exactly and frame to Twilio's 20 ms media size."""

import math
import struct
import wave

import pytest

from voice_stack.audio import (
    FRAME_BYTES, SAMPLE_RATE, duration_ms, frame_count, iter_frames,
    pcm16_to_ulaw, ulaw_to_pcm16, write_ulaw_wav,
)


def levels():
    return [struct.unpack("<h", ulaw_to_pcm16(bytes([value])))[0] for value in range(256)]


def test_both_zero_codes_decode_to_silence():
    assert ulaw_to_pcm16(b"\xff\x7f") == b"\x00\x00\x00\x00"
    assert pcm16_to_ulaw(b"\x00\x00") == b"\xff"          # One 16-bit sample.
    assert pcm16_to_ulaw(b"\x00\x00" * 2) == b"\xff\xff"


def test_every_code_reencodes_to_itself():
    for value in range(256):
        if value == 0x7F:  # The second zero code normalizes to 0xFF.
            continue
        assert pcm16_to_ulaw(ulaw_to_pcm16(bytes([value]))) == bytes([value])


def test_codes_are_monotonic_about_zero():
    decoded = levels()
    assert decoded[0x00] < 0 < decoded[0x80]
    assert decoded[:0x80] == sorted(decoded[:0x80])
    assert decoded[0x80:] == sorted(decoded[0x80:], reverse=True)
    assert decoded[0xFF] == decoded[0x7F] == 0


def test_pcm_round_trip_stays_within_one_quantization_step():
    original = [int(12000 * math.sin(index / 40)) for index in range(1000)]
    pcm = struct.pack(f"<{len(original)}h", *original)
    restored = struct.unpack(f"<{len(original)}h", ulaw_to_pcm16(pcm16_to_ulaw(pcm)))
    assert max(abs(before - after) for before, after in zip(original, restored)) < 400


def test_pcm_conversion_scales_length_by_two():
    assert len(ulaw_to_pcm16(b"\xff" * 160)) == FRAME_BYTES * 2
    assert len(pcm16_to_ulaw(b"\x00\x00" * 160)) == FRAME_BYTES


def test_empty_audio_produces_no_frames():
    assert list(iter_frames(b"")) == []
    assert frame_count(b"") == 0
    assert duration_ms(b"") == 0


def test_frames_are_exactly_one_twilio_media_frame():
    audio = bytes(range(256)) * 4  # 1024 bytes: six whole frames plus a 64-byte tail.
    frames = list(iter_frames(audio))
    assert frame_count(audio) == len(frames) == 7
    assert all(len(frame) == FRAME_BYTES for frame in frames)
    assert b"".join(frames) == audio + b"\xff" * (FRAME_BYTES - 64)
    assert duration_ms(audio) == 128


def test_partial_frame_is_padded_with_silence_unless_asked_otherwise():
    padded = list(iter_frames(b"\x11" * 200))
    assert len(padded) == 2 and padded[1] == b"\x11" * 40 + b"\xff" * 120
    short = list(iter_frames(b"\x11" * 200, pad=False))
    assert len(short) == 2 and short[1] == b"\x11" * 40


@pytest.mark.parametrize("call", [
    lambda: ulaw_to_pcm16("\xff" * 160),
    lambda: pcm16_to_ulaw("pcm"),
    lambda: pcm16_to_ulaw(b"\x01"),
    lambda: iter_frames(None),
    lambda: frame_count("audio"),
    lambda: duration_ms(bytearray(4)),
])
def test_malformed_audio_is_rejected(call):
    with pytest.raises(ValueError):
        call()


def test_written_wav_is_playable_mono_8khz(tmp_path):
    audio = pcm16_to_ulaw(struct.pack("<8h", -12000, -6000, -1, 0, 1, 6000, 12000, 30000))
    path = write_ulaw_wav(tmp_path / "reply.wav", audio)
    assert path == tmp_path / "reply.wav"
    with wave.open(str(path), "rb") as written:
        assert written.getparams()[:3] == (1, 2, SAMPLE_RATE)
        assert written.getnframes() == 8
        assert written.readframes(8) == ulaw_to_pcm16(audio)


def test_writing_creates_missing_directories_and_rejects_other_rates(tmp_path):
    path = write_ulaw_wav(tmp_path / "turns" / "turn-1.wav", b"\xff" * FRAME_BYTES)
    assert path.is_file()
    with pytest.raises(ValueError):
        write_ulaw_wav(tmp_path / "wide.wav", b"\xff", sample_rate=16000)