"""Voice-path budgets and provider selection. No network and no dial.

Deepgram is the only speech-to-text client in this repository. Phonon speech
is requested as one HTTP body, then sliced into chunks. Nemotron and Gemini
stream text. ElevenLabs streams audio when it is the fallback.
"""

from __future__ import annotations

import time

# How long the runtime waits after a finalized caller turn before the agent
# starts. speech_final uses the shorter settle; legacy final uses the longer one.
REPLY_SETTLE_FINAL_SECONDS = 0.1
REPLY_SETTLE_LEGACY_SECONDS = 0.3
# Phonon returns the whole phrase, then the client yields this many mu-law
# bytes at a time. 8000 bytes is one second of 8 kHz audio.
PHONON_CHUNK_BYTES = 8000


class LatencyTrace:
    """Monotonic marks for unit tests. This does not place a call."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.marks: list[tuple[str, float]] = []

    def mark(self, name: str) -> None:
        self.marks.append((str(name), float(self.clock())))

    def ms(self, start: str, end: str) -> int:
        times = {name: stamp for name, stamp in self.marks}
        return int(round((times[end] - times[start]) * 1000))


def select_stt(settings) -> dict:
    """Deepgram when a key is configured. There is no other STT implementation."""
    key = str(getattr(settings, "deepgram_api_key", "") or "")
    model = str(getattr(settings, "deepgram_model", "") or "nova-3")
    enabled = bool(getattr(settings, "transcription_enabled", False))
    configured = bool(key) and key != "REPLACE_ME"
    return {
        "provider": "deepgram" if configured else "unconfigured",
        "preferred": "deepgram",
        "model": model if configured else "",
        "enabled": bool(enabled and configured),
        "alternatives": [],
    }


def select_voice(voice, settings) -> dict:
    """STT, TTS, and LLM for a mission, including cloud fallbacks when keyed."""
    stt = select_stt(settings)
    tts = getattr(voice, "tts_provider", "elevenlabs")
    llm = getattr(voice, "llm_provider", "gemini")
    tts_fallback = ""
    if tts == "phonon" and getattr(voice, "elevenlabs_api_key", "") and getattr(voice, "elevenlabs_voice_id", ""):
        tts_fallback = "elevenlabs"
    llm_fallback = ""
    if llm == "nemotron" and getattr(voice, "gemini_api_key", ""):
        llm_fallback = "gemini"
    return {
        "stt": stt["provider"],
        "stt_model": stt["model"],
        "stt_enabled": stt["enabled"],
        "stt_preferred": "deepgram",
        "tts": tts,
        "tts_fallback": tts_fallback,
        "llm": llm,
        "llm_fallback": llm_fallback,
    }


def budgets() -> dict:
    """Numbers taken from the live stack, so the audit cannot drift quietly."""
    from operator_service.runtime import FIRST_TEXT_SECONDS, PREPARATION_SECONDS
    from transcription.service import CONNECT_SECONDS, ENDPOINTING_MS, UTTERANCE_END_MS
    from voice_stack.local_providers import HEALTH_SECONDS

    return {
        "stt_provider": "deepgram",
        "stt_endpointing_ms": ENDPOINTING_MS,
        "stt_utterance_end_ms": UTTERANCE_END_MS,
        "stt_connect_seconds": CONNECT_SECONDS,
        "reply_settle_final_seconds": REPLY_SETTLE_FINAL_SECONDS,
        "reply_settle_legacy_seconds": REPLY_SETTLE_LEGACY_SECONDS,
        "llm_first_text_seconds": FIRST_TEXT_SECONDS,
        "llm_preparation_seconds": PREPARATION_SECONDS,
        "phonon_health_seconds": HEALTH_SECONDS,
        "phonon_chunk_bytes": PHONON_CHUNK_BYTES,
        "phonon_first_audio": "after the full HTTP body; chunks are not streamed from the server",
        "ivr_auto": "playbook digit after STT endpoint, no LLM and no TTS",
        "barge_in": "Deepgram SpeechStarted and final caller words clear playback",
    }


def report_lines(voice=None, settings=None) -> list[str]:
    """Text for the Mac measurement script. It does not dial."""
    table = budgets()
    lines = [
        "Voice latency budgets (no call placed)",
        f"stt={table['stt_provider']} endpointing_ms={table['stt_endpointing_ms']} "
        f"utterance_end_ms={table['stt_utterance_end_ms']}",
        f"reply_settle_final_s={table['reply_settle_final_seconds']} "
        f"llm_first_text_s={table['llm_first_text_seconds']}",
        f"phonon_chunk_bytes={table['phonon_chunk_bytes']} ({table['phonon_first_audio']})",
        f"ivr_auto={table['ivr_auto']}",
        f"barge_in={table['barge_in']}",
    ]
    if voice is not None and settings is not None:
        chosen = select_voice(voice, settings)
        lines.append(
            f"selected stt={chosen['stt']} tts={chosen['tts']} llm={chosen['llm']} "
            f"tts_fallback={chosen['tts_fallback'] or 'none'} "
            f"llm_fallback={chosen['llm_fallback'] or 'none'}"
        )
    lines.append("Measure on the Mac by reading operator trace events. Do not set PHONEY_ALLOW_LIVE_CALL here.")
    return lines
