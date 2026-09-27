"""Voice-layer configuration that never exposes credentials in ``repr``.

Kept separate from ``config.Settings`` on purpose: the Twilio switchboard
requires an account SID and auth token, and a teammate working only on the
Gemini/ElevenLabs adapters should not need them. Load a specific file with
``from_env(path)`` so the same code runs against a local checkout or the
installed server's private environment.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

GEMINI_MODEL = re.compile(r"[a-z0-9.-]{1,80}\Z")
ELEVENLABS_MODEL = re.compile(r"[A-Za-z0-9._-]{1,80}\Z")
VOICE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
# An allowlist, not a pattern: output_format becomes a URL query value.
OUTPUT_FORMATS = frozenset({
    "ulaw_8000", "alaw_8000", "pcm_8000", "pcm_16000", "pcm_22050", "pcm_24000",
    "pcm_32000", "pcm_44100", "pcm_48000",
    "mp3_22050_32", "mp3_24000_48", "mp3_44100_32", "mp3_44100_64", "mp3_44100_96",
    "mp3_44100_128", "mp3_44100_192",
    "opus_48000_32", "opus_48000_64", "opus_48000_96", "opus_48000_128", "opus_48000_192",
})
TWILIO_FORMAT = "ulaw_8000"


@dataclass(frozen=True)
class VoiceSettings:
    """Validated provider settings for one owner voice and one Gemini model."""

    enabled: bool = False
    gemini_api_key: str = field(default="", repr=False)
    gemini_model: str = "gemini-3.8-flash"
    elevenlabs_api_key: str = field(default="", repr=False)
    elevenlabs_voice_id: str = ""
    elevenlabs_model: str = "eleven_flash_v2_5"
    elevenlabs_output_format: str = TWILIO_FORMAT
    output_dir: str = ""
    max_reply_tokens: int = 2048
    request_timeout: float = 20.0

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("VOICE_AGENT_ENABLED must be true or false.")
        if not GEMINI_MODEL.fullmatch(self.gemini_model):
            raise ValueError("GEMINI_MODEL must be a model identifier, without the models/ prefix.")
        if not ELEVENLABS_MODEL.fullmatch(self.elevenlabs_model):
            raise ValueError("ELEVENLABS_MODEL must be a model identifier.")
        if self.elevenlabs_output_format not in OUTPUT_FORMATS:
            raise ValueError("ELEVENLABS_OUTPUT_FORMAT must be a documented ElevenLabs format.")
        if self.elevenlabs_voice_id and not VOICE_ID.fullmatch(self.elevenlabs_voice_id):
            raise ValueError("ELEVENLABS_VOICE_ID must be a voice identifier from your account.")
        if type(self.max_reply_tokens) is not int or not 1 <= self.max_reply_tokens <= 8192:
            raise ValueError("max_reply_tokens must be between 1 and 8192.")
        if (type(self.request_timeout) not in (int, float) or isinstance(self.request_timeout, bool)
                or not 1 <= self.request_timeout <= 120):
            raise ValueError("request_timeout must be between 1 and 120 seconds.")
        if self.output_dir and not Path(self.output_dir).is_absolute():
            raise ValueError("VOICE_OUTPUT_DIR must be an absolute path when set.")
        if self.enabled:
            if not self.gemini_api_key:
                raise ValueError("Set GEMINI_API_KEY before enabling the voice agent.")
            if not self.elevenlabs_api_key:
                raise ValueError("Set ELEVENLABS_API_KEY before enabling the voice agent.")
            if not self.output_dir:
                raise ValueError("Set VOICE_OUTPUT_DIR to an absolute directory before enabling.")

    @property
    def configured(self) -> bool:
        """True when all three live values are present, regardless of the flag."""
        return bool(self.gemini_api_key and self.elevenlabs_api_key and self.elevenlabs_voice_id)

    @property
    def missing(self) -> list[str]:
        """Names only, never values: what a live run still needs."""
        return [name for name, value in (
            ("GEMINI_API_KEY", self.gemini_api_key),
            ("ELEVENLABS_API_KEY", self.elevenlabs_api_key),
            ("ELEVENLABS_VOICE_ID", self.elevenlabs_voice_id),
            ("VOICE_OUTPUT_DIR", self.output_dir),
        ) if not value]

    @property
    def output_path(self) -> Path:
        return Path(self.output_dir)

    @property
    def twilio_ready(self) -> bool:
        """Whether generated audio can be base64-encoded straight into Twilio."""
        return self.elevenlabs_output_format == TWILIO_FORMAT

    @classmethod
    def from_env(cls, path=None, environ=None):
        """Read settings from an explicit file, ``VOICE_ENV_FILE``, or the environment.

        An explicitly passed file wins over ambient variables, matching the
        ``--env-file`` a caller just asked for; ``VOICE_ENV_FILE`` does not
        override, so a server's real environment keeps precedence. Passing
        ``environ`` bypasses loading entirely, which keeps tests independent of
        the developer machine's private configuration.
        """
        if environ is None:
            if path is not None:
                env_path = Path(path)
                if not env_path.is_file():
                    raise ValueError(f"Voice environment file not found: {env_path}")
                # Parsed, not injected into os.environ: no global side effects.
                from_file = {key: value for key, value in dotenv_values(env_path).items()
                             if value is not None}
                environ = {**os.environ, **from_file}
            else:
                configured = os.getenv("VOICE_ENV_FILE", "").strip()
                if configured:
                    load_dotenv(Path(configured), override=False)
                environ = os.environ

        def value(name: str, default: str = "") -> str:
            return str(environ.get(name, default)).strip()

        flag = value("VOICE_AGENT_ENABLED", "false").lower()
        if flag not in {"true", "false"}:
            raise ValueError("VOICE_AGENT_ENABLED must be true or false.")
        return cls(
            enabled=flag == "true",
            gemini_api_key=value("GEMINI_API_KEY"),
            gemini_model=value("GEMINI_MODEL", "gemini-3.8-flash"),
            elevenlabs_api_key=value("ELEVENLABS_API_KEY"),
            elevenlabs_voice_id=value("ELEVENLABS_VOICE_ID"),
            elevenlabs_model=value("ELEVENLABS_MODEL", "eleven_flash_v2_5"),
            elevenlabs_output_format=value("ELEVENLABS_OUTPUT_FORMAT", TWILIO_FORMAT),
            output_dir=value("VOICE_OUTPUT_DIR"),
            max_reply_tokens=int(value("VOICE_MAX_REPLY_TOKENS", "2048")),
            request_timeout=float(value("VOICE_REQUEST_TIMEOUT", "20")),
        )
