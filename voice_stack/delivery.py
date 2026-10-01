"""Shared acoustic direction, independent of what any agent says or does.

ElevenLabs has no universal freeform delivery field. Dialogue models accept
audio tags; legacy TTS models accept numeric settings. This profile maps the
same delivery intent to those documented controls, without rewriting speech or
injecting instructions into the conversation model.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass

LATEST_REALTIME_MODEL = "eleven_v4_turbo"
LEGACY_MODEL = "eleven_flash_v2_5"
DELIVERY_REVISION = "2026-10-01.2"
DEFAULT_DELIVERY_PROMPT = (
    "Relaxed conversational delivery, subtle warmth and emotion, "
    "natural pace and intonation."
)


@dataclass(frozen=True)
class VoiceDelivery:
    """One immutable profile applied to every selected voice and speech path."""

    prompt: str = DEFAULT_DELIVERY_PROMPT
    stability: float = 0.5
    similarity: float = 0.75

    def __post_init__(self):
        if (not isinstance(self.prompt, str) or not self.prompt.strip()
                or len(self.prompt) > 300 or any(c in self.prompt for c in "[]\n\r")):
            raise ValueError("VOICE_DELIVERY_PROMPT must be one acoustic direction of 1 to 300 characters, without brackets.")
        for name, value in (("STABILITY", self.stability), ("SIMILARITY", self.similarity)):
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or not 0 <= value <= 1):
                raise ValueError(f"VOICE_DELIVERY_{name} must be between 0 and 1.")

    def settings_for(self, model: str) -> dict:
        """Emit only settings documented for the transport/model in use.

        The dialogue WebSocket schema currently exposes stability only, despite
        product docs describing a similarity slider for v4. Do not guess its
        wire format. V3 uses discrete Creative/Natural/Robust stability values.
        """
        if model.startswith("eleven_v4"):
            return {"stability": self.stability}
        if model.startswith("eleven_v3"):
            return {"stability": min((0.0, 0.5, 1.0), key=lambda n: abs(n - self.stability))}
        return {"stability": self.stability, "similarity_boost": self.similarity,
                "style": 0.0, "speed": 1.0, "use_speaker_boost": False}

    def text_for(self, text: str, model: str) -> str:
        """Direction is provider-only; callers keep original text in history."""
        if model.startswith(("eleven_v3", "eleven_v4")):
            return f"[{self.prompt.rstrip('. ')}] {text}"
        return text

    def description_for(self, description: str) -> str:
        """Give newly designed voices the same direction, preserving identity."""
        return f"{description.strip()}\nVocal delivery: {self.prompt}"

    @property
    def cache_key(self) -> str:
        """Any acoustic setting or policy revision invalidates rendered clips."""
        payload = json.dumps({"revision": DELIVERY_REVISION, **asdict(self)},
                             sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


DEFAULT_DELIVERY = VoiceDelivery()
