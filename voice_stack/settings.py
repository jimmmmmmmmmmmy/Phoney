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
from urllib.parse import urlsplit

from .delivery import DEFAULT_DELIVERY_PROMPT, LATEST_REALTIME_MODEL, VoiceDelivery

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
TTS_PROVIDERS = frozenset({"elevenlabs", "phonon"})
LLM_PROVIDERS = frozenset({"gemini", "nemotron"})
LOCAL_MODEL = re.compile(r"[A-Za-z0-9._-]{1,80}\Z")


def _local_base_url(url: str, name: str) -> str:
    """Accept an http(s) base URL with no userinfo, query, or fragment."""
    if not url:
        return ""
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError(f"{name} must be an http(s) base URL without credentials or a query.")
    return url.rstrip("/")


@dataclass(frozen=True)
class VoiceSettings:
    """Validated provider settings for one owner voice and one Gemini model."""

    enabled: bool = False
    gemini_api_key: str = field(default="", repr=False)
    gemini_model: str = "gemini-3.5-flash-lite"
    elevenlabs_api_key: str = field(default="", repr=False)
    elevenlabs_voice_id: str = ""
    elevenlabs_model: str = LATEST_REALTIME_MODEL
    elevenlabs_output_format: str = TWILIO_FORMAT
    output_dir: str = ""
    max_reply_tokens: int = 2048
    request_timeout: float = 20.0
    delivery: VoiceDelivery = field(default_factory=VoiceDelivery)
    tts_provider: str = "elevenlabs"
    phonon_base_url: str = ""
    phonon_model: str = "phonon-40m"
    phonon_model_path: str = ""
    phonon_voice: str = "phonon-40m"
    llm_provider: str = "gemini"
    nemotron_base_url: str = ""
    nemotron_model: str = "nemotron-en"
    nemotron_model_path: str = ""
    nemotron_api_key: str = field(default="", repr=False)

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("VOICE_AGENT_ENABLED must be true or false.")
        if not isinstance(self.delivery, VoiceDelivery):
            raise ValueError("Voice delivery must be a VoiceDelivery profile.")
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
        if self.tts_provider not in TTS_PROVIDERS:
            raise ValueError("VOICE_TTS_PROVIDER must be elevenlabs or phonon.")
        if self.llm_provider not in LLM_PROVIDERS:
            raise ValueError("AGENT_LLM_PROVIDER must be gemini or nemotron.")
        if not LOCAL_MODEL.fullmatch(self.phonon_model) or not LOCAL_MODEL.fullmatch(self.nemotron_model):
            raise ValueError("Local model ids must be short identifiers, not file paths.")
        if self.phonon_voice and not VOICE_ID.fullmatch(self.phonon_voice):
            raise ValueError("PHONON_VOICE must be a short voice identifier.")
        object.__setattr__(self, "phonon_base_url", _local_base_url(self.phonon_base_url, "PHONON_BASE_URL"))
        object.__setattr__(self, "nemotron_base_url", _local_base_url(self.nemotron_base_url, "NEMOTRON_BASE_URL"))
        for label, path in (("PHONON_MODEL_PATH", self.phonon_model_path),
                            ("NEMOTRON_MODEL_PATH", self.nemotron_model_path)):
            if path and (not Path(path).is_absolute() or ".." in Path(path).parts):
                raise ValueError(f"{label} must be an absolute path when set.")
        if self.enabled:
            if not self.llm_configured:
                raise ValueError("Set GEMINI_API_KEY, or AGENT_LLM_PROVIDER=nemotron with NEMOTRON_BASE_URL.")
            if not self.tts_configured:
                raise ValueError("Set ELEVENLABS_API_KEY, or VOICE_TTS_PROVIDER=phonon with PHONON_BASE_URL.")
            if not self.output_dir:
                raise ValueError("Set VOICE_OUTPUT_DIR to an absolute directory before enabling.")

    @property
    def llm_configured(self) -> bool:
        """True when the selected dialogue provider has what it needs to be called."""
        if self.llm_provider == "nemotron":
            return bool(self.nemotron_base_url)
        return bool(self.gemini_api_key)

    @property
    def tts_configured(self) -> bool:
        """True when the selected speech provider has what it needs to be called."""
        if self.tts_provider == "phonon":
            return bool(self.phonon_base_url)
        return bool(self.elevenlabs_api_key)

    @property
    def configured(self) -> bool:
        """True when the selected providers can run, regardless of the flag."""
        speech = self.tts_configured if self.tts_provider == "phonon" else bool(
            self.elevenlabs_api_key and self.elevenlabs_voice_id)
        return bool(self.llm_configured and speech)

    @property
    def missing(self) -> list[str]:
        """Names only, never values: what a live run still needs."""
        required = []
        if self.llm_provider == "nemotron":
            required.append(("NEMOTRON_BASE_URL", self.nemotron_base_url))
        else:
            required.append(("GEMINI_API_KEY", self.gemini_api_key))
        if self.tts_provider == "phonon":
            required.append(("PHONON_BASE_URL", self.phonon_base_url))
        else:
            required.extend((
                ("ELEVENLABS_API_KEY", self.elevenlabs_api_key),
                ("ELEVENLABS_VOICE_ID", self.elevenlabs_voice_id),
            ))
        required.append(("VOICE_OUTPUT_DIR", self.output_dir))
        return [name for name, value in required if not value]

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
            gemini_model=value("GEMINI_MODEL", "gemini-3.5-flash-lite"),
            elevenlabs_api_key=value("ELEVENLABS_API_KEY"),
            elevenlabs_voice_id=value("ELEVENLABS_VOICE_ID"),
            elevenlabs_model=value("ELEVENLABS_MODEL", LATEST_REALTIME_MODEL),
            elevenlabs_output_format=value("ELEVENLABS_OUTPUT_FORMAT", TWILIO_FORMAT),
            output_dir=value("VOICE_OUTPUT_DIR"),
            max_reply_tokens=int(value("VOICE_MAX_REPLY_TOKENS", "2048")),
            request_timeout=float(value("VOICE_REQUEST_TIMEOUT", "20")),
            tts_provider=value("VOICE_TTS_PROVIDER", "elevenlabs").lower(),
            phonon_base_url=value("PHONON_BASE_URL"),
            phonon_model=value("PHONON_MODEL", "phonon-40m"),
            phonon_model_path=value("PHONON_MODEL_PATH"),
            phonon_voice=value("PHONON_VOICE", "phonon-40m"),
            llm_provider=value("AGENT_LLM_PROVIDER", "gemini").lower(),
            nemotron_base_url=value("NEMOTRON_BASE_URL"),
            nemotron_model=value("NEMOTRON_MODEL", "nemotron-en"),
            nemotron_model_path=value("NEMOTRON_MODEL_PATH"),
            nemotron_api_key=value("NEMOTRON_API_KEY"),
            delivery=VoiceDelivery(
                prompt=value("VOICE_DELIVERY_PROMPT", DEFAULT_DELIVERY_PROMPT),
                stability=float(value("VOICE_DELIVERY_STABILITY", "0.5")),
                similarity=float(value("VOICE_DELIVERY_SIMILARITY", "0.75")),
            ),
        )
