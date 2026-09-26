"""Original voice design: a permanent voice built from a written description.

Protocol: https://elevenlabs.io/docs/api-reference/text-to-voice/design
Voice Design synthesizes a *new* voice from text, so it needs no recordings and
no consent beyond the account holder's. That is what makes it usable when a
real person's identity is not available for cloning.

Two steps, deliberately separate. ``design_previews`` returns candidate voices,
each with a short audio preview, and promotes nothing; ``create_designed_voice``
promotes exactly one chosen ``generated_voice_id`` into a voice that can be
reused by ID. Only the second enrolls anything, so it is the only step that has
to be run once and deliberately.

The provider writes the line the previews speak only when asked to.
``text`` is optional, but ``auto_generate_text`` defaults to false, so leaving
both unset would ask for voices that never say anything. Omitting ``text`` here
therefore sets ``auto_generate_text``, and a supplied ``text`` must satisfy the
provider's own 100 to 1000 character window instead of failing remotely with a
422. The number of previews is the provider's to choose and is never requested,
so a response is described by what it returned, not by an expected count.

A designed voice is category ``generated`` and does not expire. The account's
``premade`` Default voices do expire (2026-12-31 at the time of writing), which
is the practical reason to enroll one.

Previews are only ever returned as decoded bytes. The base64 field is never
echoed into ``repr``, logs or JSON, matching the audio-payload rule the rest of
the repository follows.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
import httpx

from .settings import ELEVENLABS_MODEL as ELEVENLABS_MODEL_PATTERN
from .settings import VOICE_ID as VOICE_ID_PATTERN
from .tts import TTSError

DESIGN_URL = "https://api.elevenlabs.io/v1/text-to-voice/design"
CREATE_URL = "https://api.elevenlabs.io/v1/text-to-voice"
# The design model is a different family from the synthesis model in
# ``ELEVENLABS_MODEL``: it produces a voice, it does not speak for the agent.
DEFAULT_DESIGN_MODEL = "eleven_multilingual_ttv_v2"
MIN_DESCRIPTION_CHARS = 20
MAX_DESCRIPTION_CHARS = 1000
# A supplied line has to satisfy the provider's own window. Omitting it is
# meaningful and is what ``auto_generate_text`` covers.
MIN_SAMPLE_CHARS = 100
MAX_SAMPLE_CHARS = 1000
DESIGN_SECONDS = 120.0
CONNECT_SECONDS = 5.0
EXCERPT_CHARS = 300
MAX_SEED = 4_294_967_295
MEDIA_TYPES = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/ogg": "ogg",
    "audio/opus": "opus",
    "audio/webm": "webm",
    "audio/flac": "flac",
}


def _client(transport, timeout: float) -> httpx.Client:
    """One short-lived client; a supplied transport keeps tests off the network."""
    return httpx.Client(transport=transport,
                        timeout=httpx.Timeout(timeout, connect=CONNECT_SECONDS))


def _failure(action: str, status: int, body: str = "") -> TTSError:
    """A provider failure carrying status and message, never request headers."""
    detail = " ".join(str(body).split())[:EXCERPT_CHARS]
    return TTSError(f"{action} failed with HTTP {status}" + (f": {detail}" if detail else ""))


@dataclass(frozen=True)
class Preview:
    """One candidate voice, with its audio already decoded."""

    generated_voice_id: str
    audio: bytes = field(default=b"", repr=False)
    duration_secs: float = 0.0
    language: str = ""
    media_type: str = ""

    @property
    def suffix(self) -> str:
        """A file extension that matches the bytes, never a guessed one."""
        return MEDIA_TYPES.get(self.media_type.split(";")[0].strip().lower(), "audio")


@dataclass(frozen=True)
class Design:
    """The sampled text and every candidate the provider returned."""

    text: str
    previews: tuple[Preview, ...]

    def __getitem__(self, index: int) -> Preview:
        return self.previews[index]

    def __len__(self) -> int:
        return len(self.previews)


def _checked_description(description) -> str:
    description = str(description or "").strip()
    if not MIN_DESCRIPTION_CHARS <= len(description) <= MAX_DESCRIPTION_CHARS:
        raise ValueError(
            f"A voice description must be {MIN_DESCRIPTION_CHARS} to "
            f"{MAX_DESCRIPTION_CHARS} characters.")
    return description


def _checked_sample_text(text) -> str:
    """A supplied line must fit the provider's 100 to 1000 character window.

    An empty result is the caller saying "let the provider write it", which
    ``design_previews`` turns into ``auto_generate_text``. Catching a short line
    here keeps the failure local instead of a 422 from the provider.
    """
    if text is None:
        return ""
    text = str(text).strip()
    if not MIN_SAMPLE_CHARS <= len(text) <= MAX_SAMPLE_CHARS:
        raise ValueError(
            f"A design sample text must be {MIN_SAMPLE_CHARS} to {MAX_SAMPLE_CHARS} "
            "characters; omit it to let the provider write the line.")
    return text


def _checked_model(model_id) -> str:
    if not isinstance(model_id, str) or not ELEVENLABS_MODEL_PATTERN.fullmatch(model_id):
        raise ValueError("Use a documented design model identifier.")
    return model_id


def _checked_number(value, name: str, low: float, high: float):
    if value is None:
        return None
    if type(value) not in (int, float) or isinstance(value, bool):
        raise ValueError(f"{name} must be a number.")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}.")
    return value


def _checked_seed(seed):
    if seed is None:
        return None
    if type(seed) is not int or isinstance(seed, bool) or not 0 <= seed <= MAX_SEED:
        raise ValueError(f"seed must be an integer between 0 and {MAX_SEED}.")
    return seed


def _decode_audio(preview: dict) -> bytes:
    """Decode one preview's audio, rejecting anything unusable."""
    encoded = preview.get("audio_base_64")
    if not isinstance(encoded, str) or not encoded:
        raise TTSError("Voice design returned a preview without audio")
    try:
        audio = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TTSError("Voice design returned an undecodable preview") from exc
    if not audio:
        raise TTSError("Voice design returned an empty preview")
    return audio


def _checked_previews(payload) -> list[Preview]:
    previews = payload.get("previews") if isinstance(payload, dict) else None
    if not isinstance(previews, list) or not previews:
        raise TTSError("Voice design returned no previews")
    checked = []
    for preview in previews:
        if not isinstance(preview, dict):
            raise TTSError("Voice design returned a malformed preview")
        identifier = preview.get("generated_voice_id")
        # A preview ID cannot be enrolled later unless it is a usable identifier.
        if not isinstance(identifier, str) or not VOICE_ID_PATTERN.fullmatch(identifier):
            raise TTSError("Voice design returned an unusable preview identifier")
        duration = preview.get("duration_secs")
        checked.append(Preview(
            generated_voice_id=identifier,
            audio=_decode_audio(preview),
            duration_secs=float(duration) if isinstance(duration, (int, float))
            and not isinstance(duration, bool) else 0.0,
            language=str(preview.get("language") or ""),
            media_type=str(preview.get("media_type") or ""),
        ))
    return checked


def design_previews(api_key: str, voice_description: str, *, text=None,
                    model_id: str = DEFAULT_DESIGN_MODEL, loudness=None,
                    guidance_scale=None, seed=None, transport=None,
                    timeout: float = DESIGN_SECONDS) -> Design:
    """Ask for candidate voices. This promotes nothing and enrolls nothing.

    ``text`` is optional: when it is absent the provider is explicitly asked to
    write the line, because ``auto_generate_text`` defaults to false and leaving
    both unset means the previews would have nothing to say.
    """
    if not api_key:
        raise ValueError("ELEVENLABS_API_KEY is required before designing a voice.")
    body = {
        "voice_description": _checked_description(voice_description),
        "model_id": _checked_model(model_id),
    }
    sample = _checked_sample_text(text)
    if sample:
        body["text"] = sample
    else:
        # Without this the provider generates no line to speak and the request
        # is malformed, even though ``text`` is nominally optional.
        body["auto_generate_text"] = True
    for name, value in (("loudness", _checked_number(loudness, "loudness", -1.0, 1.0)),
                        ("guidance_scale",
                         _checked_number(guidance_scale, "guidance_scale", 0.0, 100.0))):
        if value is not None:
            body[name] = value
    chosen_seed = _checked_seed(seed)
    if chosen_seed is not None:
        body["seed"] = chosen_seed

    try:
        with _client(transport, timeout) as client:
            response = client.post(DESIGN_URL, headers={"xi-api-key": api_key}, json=body)
    except httpx.HTTPError as exc:
        raise TTSError(f"Voice design failed: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise _failure("Voice design", response.status_code, response.text)
    try:
        payload = response.json()
    except ValueError as exc:
        raise TTSError("Voice design returned a malformed response") from exc
    previews = _checked_previews(payload)
    returned_text = payload.get("text") if isinstance(payload, dict) else None
    return Design(text=str(returned_text or sample),
                  previews=tuple(previews))


def create_designed_voice(api_key: str, voice_name: str, voice_description: str,
                          generated_voice_id: str, *, labels=None, transport=None,
                          timeout: float = DESIGN_SECONDS) -> dict:
    """Promote one chosen preview into a reusable voice in the account."""
    if not api_key:
        raise ValueError("ELEVENLABS_API_KEY is required before creating a voice.")
    name = str(voice_name or "").strip()
    if not 1 <= len(name) <= 80 or "\n" in name or "\r" in name:
        raise ValueError("A voice name must be 1 to 80 characters without line breaks.")
    if not isinstance(generated_voice_id, str) or not VOICE_ID_PATTERN.fullmatch(generated_voice_id):
        raise ValueError("Use a generated_voice_id from a design preview.")
    body = {
        "voice_name": name,
        "voice_description": _checked_description(voice_description),
        "generated_voice_id": generated_voice_id,
    }
    if labels:
        body["labels"] = {str(key): str(value) for key, value in dict(labels).items()}

    try:
        with _client(transport, timeout) as client:
            response = client.post(CREATE_URL, headers={"xi-api-key": api_key}, json=body)
    except httpx.HTTPError as exc:
        raise TTSError(f"Voice creation failed: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise _failure("Voice creation", response.status_code, response.text)
    try:
        payload = response.json()
    except ValueError as exc:
        raise TTSError("Voice creation returned a malformed response") from exc
    if not isinstance(payload, dict) or not payload.get("voice_id"):
        raise TTSError("Voice creation returned no voice_id")
    return payload


def describe_design(design: Design, paths) -> list[dict]:
    """Preview metadata safe to print: identifiers, not payloads."""
    return [{"index": index, "generated_voice_id": preview.generated_voice_id,
             "duration_secs": preview.duration_secs, "language": preview.language,
             "media_type": preview.media_type, "bytes": len(preview.audio),
             "file": str(paths[index])}
            for index, preview in enumerate(design.previews)]


__all__ = ["Design", "Preview", "create_designed_voice", "describe_design", "design_previews",
           "DEFAULT_DESIGN_MODEL", "MIN_DESCRIPTION_CHARS", "MAX_DESCRIPTION_CHARS",
           "MIN_SAMPLE_CHARS", "MAX_SAMPLE_CHARS", "EXCERPT_CHARS"]