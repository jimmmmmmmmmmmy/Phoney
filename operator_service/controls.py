"""Owner keypad commands, saved profiles, and the fixed phrases Stage 2 speaks.

The parser is pure: one digit plus a monotonic time in, one command out. That
keeps the transport's rule (only the bound owner leg may send keys) separate
from the product rule (``#1``-``#4`` select a profile, ``#0`` returns control),
and lets every row of the documented keypad table be pinned without a phone.

Two states are enough. In ``idle`` a ``#`` starts a two-second prefix; in
``after_hash`` the next digit completes a command or is consumed locally. Bare
digits are remote IVR input: they are queued only while a human is speaking, so
a menu press can never select a profile during agent mode. The parser never
deduplicates: repeated identical digits can be intentional, and the transport
already ignores a repeated ``(StreamSid, sequenceNumber)``.

``ClipLibrary`` renders one phrase per profile through ``voice_stack.tts`` and
caches the raw mu-law privately, because a keypress must not wait on a provider
twice. Nothing here dials or opens a socket, and ``load_voice_settings`` never
raises: an incomplete voice configuration simply leaves the operator in human
relay mode instead of taking a call to a voice that cannot speak.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
import secrets
import time

import httpx

from voice_stack.audio import duration_ms
from voice_stack.settings import GEMINI_MODEL, VoiceSettings
from voice_stack.tts import TTSError, speech

from .sessions import HUMAN

log = logging.getLogger("uvicorn.error")

PROFILES_FILE = "profiles.json"
PROFILE_FIELDS = ("name", "system_prompt", "model", "actions", "demo_phrase")
# The documented shortcuts. ``#0`` and ``##`` are handled separately because
# they do not select a saved profile.
PROFILE_KEYS = ("1", "2", "3", "4")
RELEASE_KEY = "0"
DEFAULT_PROFILE = "1"
# What a profile may be trusted to do. ``reply`` is agent speech on the remote
# leg; ``digits`` is an explicit IVR action, never a remote keypress.
ACTIONS = frozenset({"reply", "digits"})

CLIP_DIRNAME = "operator-clips"
# The documented three-second preparation deadline: a press that cannot produce
# audible speech in time returns control rather than leaving a silent line.
PREPARE_SECONDS = 3.0
MAX_PROMPT_CHARS = 2000
MAX_NAME_CHARS = 80
MAX_PHRASE_CHARS = 400
MAX_CLIP_MS = 60_000

IDLE = "idle"
AFTER_HASH = "after_hash"
PROFILE = "profile"
RELEASE = "release"
HASH = "hash"
DIGITS = "digits"
IGNORED = "ignored"

PREFIX_SECONDS = 2.0
COALESCE_SECONDS = 0.3
MAX_IVR_DIGITS = 32
KEYPAD_KEYS = frozenset("0123456789*#")
IVR_KEYS = frozenset("0123456789*")
# Reasons worth one log line: a real keypress that did nothing.
REPORTED = frozenset({"invalid-shortcut", "expired-shortcut", "agent-mode-digits"})


@dataclass(frozen=True)
class Command:
    """One decision from the keypad parser, ready for the controller."""

    kind: str
    value: str = ""
    reason: str = ""


@dataclass(frozen=True)
class Profile:
    """One saved instruction set behind ``#1``-``#4``.

    ``system_prompt`` is trusted owner text and belongs in the provider's system
    instruction; conversation speech is content and never selects a profile.
    """

    key: str
    name: str
    system_prompt: str
    demo_phrase: str
    actions: tuple[str, ...] = ("reply",)
    model: str = ""

    def __post_init__(self):
        if self.key not in PROFILE_KEYS:
            raise ValueError("A profile key must be one of #1-#4.")
        for label, value in (("name", self.name), ("system_prompt", self.system_prompt),
                             ("demo_phrase", self.demo_phrase), ("model", self.model)):
            if not isinstance(value, str):
                raise ValueError(f"Profile {self.key} needs text for {label}.")
        if not 1 <= len(self.name.strip()) <= MAX_NAME_CHARS or "\n" in self.name:
            raise ValueError(f"Profile {self.key} needs a one-line name.")
        if not 1 <= len(self.system_prompt.strip()) <= MAX_PROMPT_CHARS:
            raise ValueError(f"Profile {self.key} needs a system prompt within "
                             f"{MAX_PROMPT_CHARS} characters.")
        if not 1 <= len(self.demo_phrase.strip()) <= MAX_PHRASE_CHARS:
            raise ValueError(f"Profile {self.key} needs a one-line demo phrase within "
                             f"{MAX_PHRASE_CHARS} characters.")
        if "\n" in self.demo_phrase or "\r" in self.demo_phrase:
            raise ValueError(f"Profile {self.key} needs a one-line demo phrase.")
        actions = tuple(self.actions)
        if not actions or len(set(actions)) != len(actions):
            raise ValueError(f"Profile {self.key} needs at least one action, without repeats.")
        if any(action not in ACTIONS for action in actions):
            raise ValueError(f"Profile {self.key} may only allow: {', '.join(sorted(ACTIONS))}.")
        if self.model.strip() and not GEMINI_MODEL.fullmatch(self.model.strip()):
            raise ValueError(f"Profile {self.key} must name a Gemini model identifier, or nothing.")

    @property
    def speaks(self) -> bool:
        """Whether this profile is trusted to send agent speech to the remote."""
        return "reply" in self.actions


def load_profiles(path=None) -> dict[str, Profile]:
    """Read the saved profiles; a malformed file is refused, never guessed.

    The file is the source of the defaults, so a broken edit has to fail loudly
    at startup instead of quietly changing what ``#1`` means on a live call.
    """
    source = Path(path) if path is not None else Path(__file__).with_name(PROFILES_FILE)
    try:
        document = json.loads(source.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Profiles file must be readable JSON: {source.name}") from exc
    entries = document.get("profiles") if isinstance(document, dict) else None
    if not isinstance(entries, dict) or set(entries) != set(PROFILE_KEYS):
        raise ValueError("Profiles file must define exactly #1-#4.")
    profiles = {}
    for key in PROFILE_KEYS:
        entry = entries[key]
        if not isinstance(entry, dict) or set(entry) != set(PROFILE_FIELDS):
            raise ValueError(f"Profile {key} must define exactly: {', '.join(PROFILE_FIELDS)}.")
        if not isinstance(entry["actions"], list):
            raise ValueError(f"Profile {key} action allowlist must be a list.")
        profiles[key] = Profile(
            key=key, name=entry["name"], system_prompt=entry["system_prompt"],
            demo_phrase=entry["demo_phrase"], actions=tuple(entry["actions"]),
            model=entry["model"],
        )
    return profiles


class Keypad:
    """The documented two-state owner-keypad parser for one owner leg."""

    def __init__(self, *, prefix_seconds: float = PREFIX_SECONDS,
                 coalesce_seconds: float = COALESCE_SECONDS, clock=time.monotonic):
        for label, value in (("prefix_seconds", prefix_seconds),
                             ("coalesce_seconds", coalesce_seconds)):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 < value <= 10:
                raise ValueError(f"{label} must be between 0 and 10 seconds.")
        self.prefix_seconds = float(prefix_seconds)
        self.coalesce_seconds = float(coalesce_seconds)
        self.clock = clock
        self.state = IDLE
        self.prefix_at: float | None = None
        self.flush_at: float | None = None
        self._digits = ""

    @property
    def queued_digits(self) -> str:
        """The remote IVR digits waiting for 300 ms of quiet."""
        return self._digits

    @property
    def pending(self) -> str:
        """What is still undecided: ``hash``, queued digits, or nothing."""
        if self.state == AFTER_HASH:
            return HASH
        return self._digits

    @property
    def due_at(self) -> float | None:
        """When the pending prefix or digit run expires, if anything is pending."""
        points = [self.prefix_at]
        if self._digits:
            points.append(self.flush_at)
        pending = [point for point in points if point is not None]
        return min(pending) if pending else None

    def remaining(self, now=None) -> float | None:
        """Seconds until the pending deadline, or ``None`` when nothing waits."""
        due = self.due_at
        return None if due is None else max(0.0, due - self._now(now))

    def feed(self, digit, *, mode: str = HUMAN, now=None) -> Command:
        """Turn one key event into a command, remembering an unfinished prefix."""
        if not isinstance(digit, str) or len(digit) != 1 or digit not in KEYPAD_KEYS:
            return Command(IGNORED, reason="not-a-key")
        moment = self._now(now)
        if self.state == AFTER_HASH:
            self.state = IDLE
            self.prefix_at = None
            if digit == RELEASE_KEY:
                return Command(RELEASE, digit)
            if digit in PROFILE_KEYS:
                return Command(PROFILE, digit)
            if digit == "#":
                # ``##`` sends one literal ``#`` to the remote IVR.
                return Command(HASH, "#")
            return Command(IGNORED, reason="invalid-shortcut")
        if digit == "#":
            self.state = AFTER_HASH
            self.prefix_at = moment + self.prefix_seconds
            return Command(IGNORED, reason="awaiting-suffix")
        if digit in IVR_KEYS:
            if mode != HUMAN:
                # Agent IVR dialing is an explicit controller action, never a
                # key pressed while the agent is speaking.
                return Command(IGNORED, reason="agent-mode-digits")
            self._digits += digit
            if len(self._digits) >= MAX_IVR_DIGITS:
                return self._take()
            self.flush_at = moment + self.coalesce_seconds
            return Command(IGNORED, reason="coalescing")
        return Command(IGNORED, reason="not-a-key")

    def expire(self, now=None) -> Command | None:
        """Resolve an unfinished prefix or a quiet digit run, if one is due."""
        moment = self._now(now)
        if self.state == AFTER_HASH and self.prefix_at is not None and moment >= self.prefix_at:
            self.state = IDLE
            self.prefix_at = None
            return Command(IGNORED, reason="expired-shortcut")
        if self._digits and self.flush_at is not None and moment >= self.flush_at:
            return self._take()
        return None

    def _take(self) -> Command:
        digits, self._digits = self._digits, ""
        self.flush_at = None
        return Command(DIGITS, digits)

    def _now(self, now=None) -> float:
        return self.clock() if now is None else float(now)


class ClipLibrary:
    """One fixed phrase per profile, rendered once and cached privately.

    The provider returns raw mu-law at 8 kHz, so the cache holds exactly the
    bytes Twilio is sent: same voice, same model, same format, and the profile's
    own phrase. Cache files are ``0600`` inside a ``0700`` directory, and a cache
    that cannot be written is logged and ignored, because the phrase is still
    spoken for this call.
    """

    def __init__(self, voice, *, transport=None, directory=None):
        if voice is None:
            raise ValueError("Rendering a cached phrase needs voice settings.")
        if directory is not None:
            self.directory = Path(directory)
        else:
            self.directory = voice.output_path / CLIP_DIRNAME if voice.output_dir else None
        self.voice = voice
        self.transport = transport
        self.renders = 0

    def path(self, key: str, phrase: str) -> Path:
        """The private cache path for this phrase, voice, model, and format."""
        if self.directory is None:
            raise ValueError("VOICE_OUTPUT_DIR must be set before a clip can be cached.")
        digest = hashlib.sha256("\0".join((
            str(key), phrase, self.voice.elevenlabs_voice_id, self.voice.elevenlabs_model,
            self.voice.elevenlabs_output_format)).encode("utf-8")).hexdigest()[:16]
        return self.directory / f"{key}-{digest}.ulaw"

    def cached(self, key: str, phrase: str) -> bytes:
        """The cached phrase, or empty bytes when it is absent or unreadable."""
        if self.directory is None:
            return b""
        try:
            audio = self.path(key, phrase).read_bytes()
        except OSError:
            return b""
        return audio if audio else b""

    async def bytes_for(self, key: str, phrase: str, *,
                        timeout: float = PREPARE_SECONDS) -> bytes:
        """Return the phrase, rendering it through the provider only once."""
        audio = self.cached(key, phrase)
        if audio:
            return audio
        if not self.voice.elevenlabs_api_key:
            raise ValueError("ELEVENLABS_API_KEY is required before generating speech.")
        async with httpx.AsyncClient(transport=self.transport,
                                     timeout=httpx.Timeout(timeout)) as http:
            audio = await speech(http, self.voice.elevenlabs_api_key,
                                 self.voice.elevenlabs_voice_id, phrase,
                                 model=self.voice.elevenlabs_model,
                                 output_format=self.voice.elevenlabs_output_format,
                                 timeout=timeout)
        if not audio:
            raise TTSError("Speech request returned no audio")
        if duration_ms(audio) > MAX_CLIP_MS:
            raise ValueError("A cached phrase must be shorter than one minute.")
        self.renders += 1
        self.store(key, phrase, audio)
        return audio

    def store(self, key: str, phrase: str, audio: bytes) -> Path | None:
        """Write the phrase privately, or return ``None`` when that is unsafe."""
        if self.directory is None:
            return None
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.directory, 0o700)
            destination = self.path(key, phrase)
            temporary = self.directory / f".{os.getpid()}.{secrets.token_hex(4)}.tmp"
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
            try:
                handle = os.open(temporary, flags, mode=0o600)
                with os.fdopen(handle, "wb") as output:
                    os.fchmod(output.fileno(), 0o600)
                    output.write(audio)
                os.replace(temporary, destination)
            finally:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
            return destination
        except OSError as exc:
            log.warning("operator_clip_cache_failed type=%s", type(exc).__name__)
            return None


def load_voice_settings(settings=None, environ=None):
    """Voice settings for the operator, or ``None`` when the layer cannot speak.

    Never raises: the operator bridge must still serve human relay calls on a
    machine with a half-filled voice configuration, and a startup failure here
    would take the whole service down with it.
    """
    if settings is not None and not settings.voice_agent_enabled:
        return None
    try:
        voice = VoiceSettings.from_env(environ=environ)
    except ValueError as exc:
        # Validation errors name missing variables and never carry their values.
        log.warning("operator_voice_unavailable reason=%s", exc)
        return None
    return voice if voice.enabled else None
