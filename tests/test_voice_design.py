"""Network-free Voice Design checks: previews, decoding, and enrollment.

Voice Design is the one non-cloning route to a distinctive voice, so the
important properties are that designing enrolls nothing, that a chosen preview
is the only thing promoted, and that preview audio is never echoed as base64.
"""

import base64
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from voice_stack.design import (Design, Preview, create_designed_voice, describe_design,
                                design_previews)
from voice_stack.tts import TTSError

KEY = "unit-test-elevenlabs-key"
DESIGN_URL = "https://api.elevenlabs.io/v1/text-to-voice/design"
CREATE_URL = "https://api.elevenlabs.io/v1/text-to-voice"
DESCRIPTION = "A calm, low-pitched voice with a warm tone and slow, clear delivery."
GENERATED = "aBcDeFgHiJkLmNoPqRsT"
SAMPLE = b"\xff\xfb\x90\x00ID3-ish-preview-bytes"
# A supplied line has to clear the provider's 100 character floor.
SAMPLE_TEXT = ("Good evening. I am calling on behalf of the owner to confirm the details "
               "you asked about, so please hold the line for just a moment.")
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def preview_payload(identifier=GENERATED, audio=SAMPLE, **overrides) -> dict:
    # ``None`` means "the provider omitted the field"; ``b""`` means "empty".
    encoded = None if audio is None else base64.b64encode(audio).decode()
    payload = {"generated_voice_id": identifier,
               "audio_base_64": encoded,
               "duration_secs": 5.5, "language": "en", "media_type": "audio/mpeg"}
    payload.update(overrides)
    return payload


class FakeDesign:
    """A synchronous stand-in that records what the adapter sent."""

    def __init__(self, payload=None, status=200, text=""):
        self.payload = payload
        self.status = status
        self.text = text
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if self.payload is None:
            return httpx.Response(self.status, text=self.text)
        return httpx.Response(self.status, json=self.payload)

    @property
    def transport(self):
        return httpx.MockTransport(self)

    def body(self, index=0) -> dict:
        return json.loads(self.requests[index].content)


def load_script(name: str):
    """Import a ``scripts/`` entry point so its helpers can be unit tested."""
    path = SCRIPTS / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_design_sends_a_description_and_decodes_every_preview():
    fake = FakeDesign(payload={"previews": [preview_payload(),
                                            preview_payload(identifier="ZzYyXxWwVvUuTtSsRrQq",
                                                             language="fr")],
                               "text": "Please hold while I check."})
    design = design_previews(KEY, DESCRIPTION, transport=fake.transport)

    assert [preview.generated_voice_id for preview in design.previews] == [
        GENERATED, "ZzYyXxWwVvUuTtSsRrQq"]
    assert all(preview.audio == SAMPLE for preview in design.previews)
    assert design.text == "Please hold while I check."
    assert design[0].language == "en" and len(design) == 2
    request = fake.requests[0]
    assert str(request.url) == DESIGN_URL
    assert request.method == "POST"
    assert request.headers["xi-api-key"] == KEY
    assert fake.body()["voice_description"] == DESCRIPTION
    # No line was supplied, so the request has to ask the provider to write one.
    # ``auto_generate_text`` defaults to false, so omitting both would ask for
    # previews that have nothing to say.
    assert "text" not in fake.body()
    assert fake.body()["auto_generate_text"] is True


def test_design_sends_a_supplied_line_instead_of_asking_for_one():
    fake = FakeDesign(payload={"previews": [preview_payload()], "text": SAMPLE_TEXT})
    design = design_previews(KEY, DESCRIPTION, text=SAMPLE_TEXT, transport=fake.transport)

    assert fake.body()["text"] == SAMPLE_TEXT
    assert "auto_generate_text" not in fake.body()
    assert design.text == SAMPLE_TEXT


def test_design_never_echoes_preview_audio_in_repr():
    preview = Preview(generated_voice_id=GENERATED, audio=SAMPLE,
                      duration_secs=1.0, language="en", media_type="audio/mpeg")
    encoded = base64.b64encode(SAMPLE).decode()
    assert encoded not in repr(preview)
    assert SAMPLE.decode("latin-1") not in repr(preview)
    assert GENERATED in repr(preview)


@pytest.mark.parametrize("media_type,suffix", [
    ("audio/mpeg", "mp3"), ("audio/wav", "wav"), ("audio/ogg; codecs=opus", "ogg"),
    ("application/octet-stream", "audio"), ("", "audio"),
])
def test_preview_suffix_follows_the_reported_media_type(media_type, suffix):
    preview = Preview(generated_voice_id=GENERATED, media_type=media_type)
    assert preview.suffix == suffix


def test_design_reports_a_provider_rejection_without_leaking_the_key():
    fake = FakeDesign(status=401, text='{"detail":{"message":"missing_permission"}}')
    with pytest.raises(TTSError) as error:
        design_previews(KEY, DESCRIPTION, transport=fake.transport)
    message = str(error.value)
    assert "HTTP 401" in message and "missing_permission" in message
    assert KEY not in message


def test_a_missing_api_key_fails_before_any_request_is_made():
    fake = FakeDesign(payload={"previews": [preview_payload()]})
    with pytest.raises(ValueError):
        design_previews("", DESCRIPTION, transport=fake.transport)
    assert fake.requests == []


@pytest.mark.parametrize("overrides", [
    {"description": "too short"},
    {"description": "x" * 1001},
    {"description": "   "},
    {"model_id": "models/eleven_multilingual_ttv_v2"},
    {"model_id": ""},
    {"loudness": 5.0},
    {"loudness": True},
    {"loudness": "loud"},
    {"guidance_scale": 1000.0},
    {"seed": -1},
    {"seed": 1.5},
    {"text": "too short"},
    {"text": ""},
    {"text": "x" * 99},
    {"text": "x" * 1001},
])
def test_design_rejects_an_unusable_argument_before_the_request(overrides):
    fake = FakeDesign(payload={"previews": [preview_payload()]})
    arguments = {"description": DESCRIPTION, **overrides}
    with pytest.raises(ValueError):
        design_previews(KEY, arguments.pop("description"),
                        transport=fake.transport, **arguments)
    assert fake.requests == []


@pytest.mark.parametrize("payload", [
    {"previews": []},
    {"previews": "wrong"},
    {},
    {"previews": [preview_payload(identifier="has spaces")]},
    {"previews": [preview_payload(identifier="")]},
    {"previews": [preview_payload(audio=b"")]},
    {"previews": [preview_payload(audio=None)]},
    {"previews": ["not-a-dict"]},
    {"previews": [preview_payload(audio_base_64="not base64!!")]},
])
def test_design_rejects_a_response_it_cannot_use(payload):
    fake = FakeDesign(payload=payload)
    with pytest.raises(TTSError):
        design_previews(KEY, DESCRIPTION, transport=fake.transport)


def test_design_rejects_a_non_json_body():
    fake = FakeDesign(status=200, text="<html>not json</html>")
    with pytest.raises(TTSError):
        design_previews(KEY, DESCRIPTION, transport=fake.transport)


def test_create_promotes_exactly_the_chosen_preview():
    fake = FakeDesign(payload={"voice_id": "21m00Tcm4TlvDq8ikWAM",
                               "requires_verification": False})
    created = create_designed_voice(KEY, "delegate-voice", DESCRIPTION, GENERATED,
                                   labels={"accent": "american"}, transport=fake.transport)
    assert created["voice_id"] == "21m00Tcm4TlvDq8ikWAM"
    request = fake.requests[0]
    assert str(request.url) == CREATE_URL
    assert request.headers["xi-api-key"] == KEY
    assert fake.body() == {"voice_name": "delegate-voice", "voice_description": DESCRIPTION,
                           "generated_voice_id": GENERATED, "labels": {"accent": "american"}}


@pytest.mark.parametrize("overrides", [
    {"name": ""},
    {"name": "x" * 81},
    {"name": "line\nbreak"},
    {"identifier": "has spaces"},
    {"identifier": ""},
    {"identifier": "../escape"},
])
def test_create_validates_the_name_and_the_generated_id(overrides):
    fake = FakeDesign(payload={"voice_id": "21m00Tcm4TlvDq8ikWAM"})
    with pytest.raises(ValueError):
        create_designed_voice(KEY, overrides.get("name", "delegate-voice"), DESCRIPTION,
                              overrides.get("identifier", GENERATED),
                              transport=fake.transport)
    assert fake.requests == []


@pytest.mark.parametrize("payload", [{}, {"voice_id": ""}, {"voice_id": None}])
def test_create_requires_a_usable_voice_id(payload):
    with pytest.raises(TTSError):
        create_designed_voice(KEY, "delegate-voice", DESCRIPTION, GENERATED,
                              transport=FakeDesign(payload=payload).transport)


def test_describe_design_reports_metadata_and_never_the_payload(tmp_path):
    design = Design(text="Hello.", previews=(
        Preview(generated_voice_id=GENERATED, audio=SAMPLE, duration_secs=2.0,
                language="en", media_type="audio/mpeg"),))
    paths = [tmp_path / "design-preview-0.mp3"]
    described = describe_design(design, paths)
    assert described == [{"index": 0, "generated_voice_id": GENERATED,
                          "duration_secs": 2.0, "language": "en",
                          "media_type": "audio/mpeg", "bytes": len(SAMPLE),
                          "file": str(paths[0])}]
    assert base64.b64encode(SAMPLE).decode() not in json.dumps(described)


def test_script_choose_saves_and_never_guesses_an_extension(tmp_path):
    module = load_script("design_voice.py")
    design = Design(text="Hello.", previews=(
        Preview(generated_voice_id=GENERATED, audio=SAMPLE, media_type="audio/mpeg"),
        Preview(generated_voice_id="ZzYyXxWwVvUuTtSsRrQq", audio=b"\x00\x01",
                media_type="application/octet-stream"),))
    paths = module.save_previews(design, tmp_path / "voice_output")
    assert [path.name for path in paths] == ["design-preview-0.mp3", "design-preview-1.audio"]
    assert paths[0].read_bytes() == SAMPLE
    assert module.choose(design, 1).generated_voice_id == "ZzYyXxWwVvUuTtSsRrQq"
    with pytest.raises(ValueError, match="--index"):
        module.choose(design, 2)


def test_script_preview_path_requires_an_absolute_directory(tmp_path):
    module = load_script("design_voice.py")
    assert module.preview_path(str(tmp_path), None) == tmp_path
    assert module.preview_path("", tmp_path) == tmp_path
    with pytest.raises(ValueError, match="VOICE_OUTPUT_DIR"):
        module.preview_path("", None)
    with pytest.raises(ValueError, match="absolute"):
        module.preview_path("", "relative/previews")


def test_stored_preview_finds_the_saved_file_by_its_reported_suffix(tmp_path):
    module = load_script("design_voice.py")
    assert module.stored_preview(tmp_path, 0) is None
    (tmp_path / "design-preview-0.mp3").write_bytes(SAMPLE)
    assert module.stored_preview(tmp_path, 0) == tmp_path / "design-preview-0.mp3"
    assert module.stored_preview(tmp_path, 1) is None


def test_enrolling_refuses_to_design_a_different_voice(tmp_path):
    # A fresh design returns other voices and overwrites the saved previews, so
    # enrolling must name a preview that was already listened to.
    env = tmp_path / "voice.env"
    env.write_text(f"ELEVENLABS_API_KEY={KEY}\nVOICE_OUTPUT_DIR={tmp_path / 'voice_output'}\n")
    result = subprocess_run(tmp_path, env, "--description", DESCRIPTION,
                            "--create", "delegate-voice")
    assert result.returncode == 1
    assert "--generated-voice-id" in result.stderr


def test_promote_dry_run_enrolls_a_saved_preview_without_designing(tmp_path):
    env = tmp_path / "voice.env"
    env.write_text(f"ELEVENLABS_API_KEY={KEY}\nVOICE_OUTPUT_DIR={tmp_path / 'voice_output'}\n")
    result = subprocess_run(tmp_path, env, "--description", DESCRIPTION,
                            "--generated-voice-id", GENERATED,
                            "--create", "delegate-voice", "--dry-run")
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["endpoint"] == CREATE_URL
    assert plan["generated_voice_id"] == GENERATED
    assert plan["would_create"] == "delegate-voice"
    assert plan["designs_again"] is False


def test_promote_needs_a_name_and_a_usable_id(tmp_path):
    env = tmp_path / "voice.env"
    env.write_text(f"ELEVENLABS_API_KEY={KEY}\nVOICE_OUTPUT_DIR={tmp_path / 'voice_output'}\n")
    unnamed = subprocess_run(tmp_path, env, "--description", DESCRIPTION,
                             "--generated-voice-id", GENERATED)
    assert unnamed.returncode == 1
    assert "--create" in unnamed.stderr
    # A bad id is rejected locally, so no request is ever attempted.
    unusable = subprocess_run(tmp_path, env, "--description", DESCRIPTION,
                              "--generated-voice-id", "has spaces", "--create", "x")
    assert unusable.returncode == 1
    assert "generated_voice_id" in unusable.stderr


def test_design_cli_dry_run_spends_nothing_and_leaks_nothing(tmp_path):
    env = tmp_path / "voice.env"
    env.write_text(f"ELEVENLABS_API_KEY={KEY}\nVOICE_OUTPUT_DIR={tmp_path / 'voice_output'}\n")
    result = subprocess_run(tmp_path, env, "--description", DESCRIPTION, "--dry-run")
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["endpoint"] == DESIGN_URL
    assert plan["would_create"] is None
    # The plan must describe what would really be sent, not assume the provider
    # volunteers a line it was never asked to write.
    assert plan["sample_text"] is None and plan["auto_generate_text"] is True
    # A dry run must not authenticate, spend credits, or print the key.
    assert KEY not in result.stdout
    assert not (tmp_path / "voice_output").exists()


def test_design_cli_rejects_a_short_description(tmp_path):
    env = tmp_path / "voice.env"
    env.write_text(f"ELEVENLABS_API_KEY={KEY}\nVOICE_OUTPUT_DIR={tmp_path / 'voice_output'}\n")
    result = subprocess_run(tmp_path, env, "--description", "too short")
    assert result.returncode == 1
    assert "20 to 1000 characters" in result.stderr


def subprocess_run(tmp_path, env, *arguments):
    import subprocess
    import sys
    return subprocess.run([sys.executable, str(SCRIPTS / "design_voice.py"),
                           "--env-file", str(env), *arguments],
                          capture_output=True, text=True)