"""Keypad parsing, profile files, and the private phrase cache; no phone, no socket.

The parser is pure, so every row of the documented keypad table is pinned here:
shortcuts, the two-state ``#`` prefix, digit coalescing, and the digit cap. The
phrase cache runs against a ``MockTransport``, so the real ``voice_stack.tts``
request path is exercised without contacting ElevenLabs.
"""

import asyncio
import json
from pathlib import Path
import stat

import httpx
import pytest

from operator_service.controls import (AFTER_HASH, COALESCE_SECONDS, DIGITS, HASH, IDLE,
                                       IGNORED, MAX_IVR_DIGITS, PREPARE_SECONDS, PROFILE,
                                       PROFILE_KEYS, RELEASE, ClipLibrary, Command, Keypad,
                                       load_profiles, load_voice_settings)
from operator_service.sessions import AGENT, HUMAN
from voice_stack.tts import TTSError
from voice_stack.settings import VoiceSettings

SHIPPED = Path(__file__).resolve().parent.parent / "operator_service" / "profiles.json"
CLIP = bytes([0x2A]) * 320          # 40 ms, or two complete mu-law frames


def voice(tmp_path, **overrides) -> VoiceSettings:
    values = {"enabled": True, "gemini_api_key": "gemini-key",
              "elevenlabs_api_key": "elevenlabs-key", "elevenlabs_voice_id": "voiceid123",
              "output_dir": str(tmp_path / "voice")}
    values.update(overrides)
    return VoiceSettings(**values)


def feed(keys, *, mode=HUMAN, keypad=None, step=0.05):
    """Feed keys on an explicit clock and return the parser with its commands."""
    keypad = keypad if keypad is not None else Keypad()
    moment = 0.0
    commands = []
    for key in keys:
        moment += step
        commands.append(keypad.feed(key, mode=mode, now=moment))
    return keypad, commands


# ------------------------------------------------------------------- profiles


def test_the_profiles_file_keeps_the_four_documented_defaults():
    profiles = load_profiles()
    assert list(profiles) == list(PROFILE_KEYS)
    assert [profile.name for profile in profiles.values()] == [
        "Continue for me", "Handle the wait", "Complete this enquiry", "My custom prompt"]
    # Every default may speak, and only the enquiry profile may also dial a menu.
    assert all(profile.speaks for profile in profiles.values())
    assert profiles["3"].actions == ("reply", "digits")
    assert profiles["1"].actions == ("reply",)
    # An empty model means "use VOICE_ settings"; nothing is hard-coded here.
    assert all(profile.model == "" for profile in profiles.values())
    assert all(0 < len(profile.demo_phrase) <= 400 for profile in profiles.values())
    assert all(len(set(profile.demo_phrase.split())) > 3 for profile in profiles.values())
    assert profiles["1"].demo_phrase == load_profiles(SHIPPED)["1"].demo_phrase


def test_profiles_are_loaded_from_an_explicit_path(tmp_path):
    document = json.loads(SHIPPED.read_text("utf-8"))
    document["profiles"]["2"]["name"] = "Hold please"
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert load_profiles(path)["2"].name == "Hold please"


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda doc: doc["profiles"].pop("3"), id="missing-key"),
    pytest.param(lambda doc: doc["profiles"].update({"5": doc["profiles"]["1"]}), id="extra-key"),
    pytest.param(lambda doc: doc.pop("profiles"), id="no-profiles"),
    pytest.param(lambda doc: doc["profiles"]["1"].pop("system_prompt"), id="missing-field"),
    pytest.param(lambda doc: doc["profiles"]["1"].update({"tone": "brisk"}), id="unknown-field"),
    pytest.param(lambda doc: doc["profiles"]["1"].update({"actions": []}), id="no-actions"),
    pytest.param(lambda doc: doc["profiles"]["1"].update({"actions": ["dial"]}), id="bad-action"),
    pytest.param(lambda doc: doc["profiles"]["1"].update({"actions": ["reply", "reply"]}),
                 id="repeated-action"),
    pytest.param(lambda doc: doc["profiles"]["1"].update({"actions": "reply"}), id="actions-text"),
    pytest.param(lambda doc: doc["profiles"]["1"].update({"system_prompt": "  "}), id="blank-prompt"),
    pytest.param(lambda doc: doc["profiles"]["1"].update({"demo_phrase": "two\nlines"}),
                 id="multi-line-phrase"),
    pytest.param(lambda doc: doc["profiles"]["1"].update({"name": 12}), id="numeric-name"),
    pytest.param(lambda doc: doc["profiles"]["1"].update({"model": None}), id="null-model"),
    pytest.param(lambda doc: doc["profiles"]["1"].update({"model": "models/gemini-3.8-flash"}),
                 id="prefixed-model"),
    pytest.param(lambda doc: doc["profiles"]["1"].update({"model": "gemini 3.8 flash"}),
                 id="spaced-model"),
    pytest.param(lambda doc: doc["profiles"]["1"].update({"system_prompt": "x" * 2001}),
                 id="oversized-prompt"),
])
def test_a_broken_profiles_file_is_refused(tmp_path, mutate):
    document = json.loads(SHIPPED.read_text("utf-8"))
    mutate(document)
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError):
        load_profiles(path)


@pytest.mark.parametrize("text", ['{"profiles":', "", "[]", "null"])
def test_an_unreadable_profiles_file_is_refused(tmp_path, text):
    path = tmp_path / "profiles.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_profiles(path)


def test_a_missing_profiles_file_is_refused(tmp_path):
    with pytest.raises(ValueError):
        load_profiles(tmp_path / "absent.json")


def test_a_profile_may_name_its_own_gemini_model(tmp_path):
    document = json.loads(SHIPPED.read_text("utf-8"))
    document["profiles"]["2"]["model"] = "gemini-3.8-flash"
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    profiles = load_profiles(path)
    assert profiles["2"].model == "gemini-3.8-flash"
    assert profiles["1"].model == ""


# --------------------------------------------------------------------- keypad


@pytest.mark.parametrize("key", PROFILE_KEYS)
def test_each_shortcut_selects_its_own_profile(key):
    keypad, commands = feed(["#", key])
    assert commands[0] == Command(IGNORED, reason="awaiting-suffix")
    assert commands[1] == Command(PROFILE, key)
    assert keypad.state == IDLE and keypad.pending == ""
    assert keypad.remaining() is None


@pytest.mark.parametrize("mode", [HUMAN, AGENT])
def test_zero_returns_control_in_either_mode(mode):
    keypad, commands = feed(["#", "0"], mode=mode)
    assert commands[1] == Command(RELEASE, "0")
    assert keypad.state == IDLE


def test_two_hashes_send_one_literal_hash_to_the_remote_menu():
    keypad, commands = feed(["#", "#"])
    assert commands[1] == Command(HASH, "#")
    assert keypad.state == IDLE and keypad.queued_digits == ""


@pytest.mark.parametrize("suffix", ["5", "6", "7", "8", "9", "*"])
def test_an_invalid_suffix_is_consumed_locally(suffix):
    keypad, commands = feed(["#", suffix])
    assert commands[1] == Command(IGNORED, reason="invalid-shortcut")
    assert keypad.state == IDLE and keypad.pending == ""
    # The refusal leaves no state behind: the next shortcut still works.
    assert keypad.feed("#", now=0.4) == Command(IGNORED, reason="awaiting-suffix")
    assert keypad.feed("3", now=0.5) == Command(PROFILE, "3")


def test_a_shortcut_is_honoured_while_the_agent_is_speaking():
    _, commands = feed(["#", "2"], mode=AGENT)
    assert commands[1] == Command(PROFILE, "2")


def test_a_repeated_shortcut_is_two_separate_events():
    # Deduplication belongs to the transport's (stream, sequence) memory: the
    # parser must not invent it, because a real repeat can be intentional.
    _, commands = feed(["#", "1", "#", "1"])
    assert [command.kind for command in commands] == [IGNORED, PROFILE, IGNORED, PROFILE]


def test_an_incomplete_prefix_expires_after_two_seconds():
    keypad, commands = feed(["#"])
    assert keypad.state == AFTER_HASH and keypad.pending == HASH
    assert keypad.remaining(now=0.05) == pytest.approx(2.0)
    assert keypad.expire(now=2.0) is None
    assert keypad.state == AFTER_HASH
    assert keypad.expire(now=2.06) == Command(IGNORED, reason="expired-shortcut")
    assert keypad.state == IDLE and keypad.remaining(now=2.06) is None
    # A stale prefix can never promote the next digit into a shortcut.
    assert keypad.feed("1", mode=AGENT, now=2.1) == Command(IGNORED, reason="agent-mode-digits")


def test_a_digit_after_an_expired_prefix_is_menu_input_not_a_shortcut():
    keypad, _ = feed(["#"])
    keypad.expire(now=3.0)
    assert keypad.feed("4", mode=HUMAN, now=3.1) == Command(IGNORED, reason="coalescing")
    assert keypad.queued_digits == "4"
    assert keypad.expire(now=3.1 + COALESCE_SECONDS) == Command(DIGITS, "4")


def test_bare_digits_are_queued_only_while_a_human_speaks():
    keypad, commands = feed(["9"], mode=HUMAN)
    assert commands == [Command(IGNORED, reason="coalescing")]
    assert keypad.queued_digits == "9"
    assert keypad.expire(now=1.0) == Command(DIGITS, "9")
    assert keypad.queued_digits == "" and keypad.remaining(now=1.0) is None
    _, agent_commands = feed(["9", "0", "*"], mode=AGENT)
    assert all(command == Command(IGNORED, reason="agent-mode-digits")
               for command in agent_commands)


@pytest.mark.parametrize("key", list("0123456789*"))
def test_every_menu_key_is_accepted_in_human_mode(key):
    keypad, commands = feed([key], mode=HUMAN)
    assert commands[0].reason == "coalescing" and keypad.queued_digits == key


def test_digits_coalesce_until_the_line_goes_quiet():
    keypad, commands = feed(["1", "2", "3"], mode=HUMAN)
    assert [command.reason for command in commands] == ["coalescing"] * 3
    assert keypad.remaining(now=0.15) == pytest.approx(COALESCE_SECONDS)
    assert keypad.expire(now=0.44) is None          # the window keeps moving
    assert keypad.expire(now=0.45) == Command(DIGITS, "123")
    assert keypad.queued_digits == ""


def test_a_menu_answer_is_capped_at_thirty_two_keys():
    keypad, commands = feed(["7"] * MAX_IVR_DIGITS)
    assert commands[-1] == Command(DIGITS, "7" * MAX_IVR_DIGITS)
    assert keypad.queued_digits == ""
    keypad.feed("8", now=1.0)
    assert keypad.queued_digits == "8"


@pytest.mark.parametrize("digit", ["!", "", None, 1, "##"])
def test_an_unrelated_key_is_ignored_without_state(digit):
    keypad, commands = feed([digit])
    assert commands[0] == Command(IGNORED, reason="not-a-key")
    assert keypad.state == IDLE and keypad.pending == ""


def test_a_keypad_refuses_a_nonsense_deadline():
    for kwargs in ({"prefix_seconds": 0}, {"prefix_seconds": 11}, {"coalesce_seconds": -1},
                   {"coalesce_seconds": True}, {"prefix_seconds": None}):
        with pytest.raises(ValueError):
            Keypad(**kwargs)


def test_the_parser_uses_its_clock_when_no_time_is_given():
    moments = iter([10.0, 10.2, 12.5])
    keypad = Keypad(clock=lambda: next(moments))
    assert keypad.feed("#").reason == "awaiting-suffix"
    assert keypad.remaining() == pytest.approx(1.8)
    assert keypad.expire() == Command(IGNORED, reason="expired-shortcut")


# -------------------------------------------------------------- phrase cache


class Provider:
    """A fake ElevenLabs surface that records each request and never dials out."""

    def __init__(self, audio=CLIP, status=200):
        self.audio = audio
        self.status = status
        self.requests = []

    def handle(self, request):
        self.requests.append(json.loads(request.content.decode("utf-8")))
        if self.status != 200:
            return httpx.Response(self.status, text="provider unavailable")
        return httpx.Response(200, content=self.audio)


def test_a_phrase_is_rendered_once_and_cached_privately(tmp_path):
    provider = Provider()
    library = ClipLibrary(voice(tmp_path), transport=httpx.MockTransport(provider.handle))

    async def run():
        return (await library.bytes_for("1", "Continue please."),
                await library.bytes_for("1", "Continue please."))

    first, second = asyncio.run(run())
    assert first == second == CLIP
    assert library.renders == 1 and len(provider.requests) == 1
    assert provider.requests[0]["text"] == "Continue please."
    path = library.path("1", "Continue please.")
    assert path.parent.name == "operator-clips"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_bytes() == CLIP
    assert list(path.parent.iterdir()) == [path]      # no temporary files left behind


def test_the_cache_name_covers_the_phrase_voice_model_and_format(tmp_path):
    library = ClipLibrary(voice(tmp_path))
    assert library.path("1", "one") != library.path("1", "two")
    assert library.path("1", "one") != library.path("2", "one")
    other_voice = ClipLibrary(voice(tmp_path, elevenlabs_voice_id="othervoice"))
    assert other_voice.path("1", "one") != library.path("1", "one")
    other_model = ClipLibrary(voice(tmp_path, elevenlabs_model="eleven_turbo_v2_5"))
    assert other_model.path("1", "one") != library.path("1", "one")
    assert library.path("1", "one").suffix == ".ulaw"


def test_a_changed_phrase_is_rendered_again(tmp_path):
    provider = Provider()
    library = ClipLibrary(voice(tmp_path), transport=httpx.MockTransport(provider.handle))

    async def run():
        await library.bytes_for("1", "First phrase.")
        await library.bytes_for("1", "Second phrase.")

    asyncio.run(run())
    assert library.renders == 2 and len(provider.requests) == 2


def test_an_empty_cache_file_is_rendered_again(tmp_path):
    provider = Provider()
    library = ClipLibrary(voice(tmp_path), transport=httpx.MockTransport(provider.handle))
    path = library.path("1", "Again please.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    assert asyncio.run(library.bytes_for("1", "Again please.")) == CLIP
    assert library.renders == 1 and path.read_bytes() == CLIP


def test_a_provider_failure_never_writes_a_cache_file(tmp_path):
    provider = Provider(status=503)
    library = ClipLibrary(voice(tmp_path), transport=httpx.MockTransport(provider.handle))
    with pytest.raises(TTSError):
        asyncio.run(library.bytes_for("1", "Unavailable."))
    assert library.renders == 0
    assert not library.path("1", "Unavailable.").exists()


def test_a_missing_api_key_fails_before_any_request(tmp_path):
    provider = Provider()
    library = ClipLibrary(voice(tmp_path, enabled=False, elevenlabs_api_key=""),
                          transport=httpx.MockTransport(provider.handle))
    with pytest.raises(ValueError):
        asyncio.run(library.bytes_for("1", "No key."))
    assert provider.requests == []


def test_a_phrase_longer_than_a_minute_is_refused(tmp_path):
    provider = Provider(audio=bytes([0x2A]) * (8_000 * 61))
    library = ClipLibrary(voice(tmp_path), transport=httpx.MockTransport(provider.handle))
    with pytest.raises(ValueError):
        asyncio.run(library.bytes_for("1", "Too long."))
    assert library.renders == 0


def test_an_empty_provider_response_is_refused(tmp_path):
    library = ClipLibrary(voice(tmp_path), transport=httpx.MockTransport(Provider(audio=b"").handle))
    with pytest.raises(TTSError):
        asyncio.run(library.bytes_for("1", "Silence."))


def test_a_cache_that_cannot_be_written_still_speaks(tmp_path):
    provider = Provider()
    directory = tmp_path / "voice" / "operator-clips"
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.write_text("not a directory")
    library = ClipLibrary(voice(tmp_path), transport=httpx.MockTransport(provider.handle))
    assert asyncio.run(library.bytes_for("1", "Still spoken.")) == CLIP
    assert library.renders == 1


def test_a_library_without_settings_or_an_output_directory_is_predictable(tmp_path):
    with pytest.raises(ValueError):
        ClipLibrary(None)
    provider = Provider()
    library = ClipLibrary(voice(tmp_path, output_dir="", enabled=False),
                          transport=httpx.MockTransport(provider.handle))
    assert library.directory is None and library.cached("1", "anything") == b""
    with pytest.raises(ValueError):
        library.path("1", "anything")
    assert asyncio.run(library.bytes_for("1", "Spoken but not cached.")) == CLIP
    assert library.renders == 1 and len(provider.requests) == 1


# ------------------------------------------------------------ voice settings


def test_voice_settings_load_only_when_they_can_speak(tmp_path):
    complete = {"VOICE_AGENT_ENABLED": "true", "GEMINI_API_KEY": "gemini",
                "ELEVENLABS_API_KEY": "elevenlabs", "ELEVENLABS_VOICE_ID": "voiceid123",
                "VOICE_OUTPUT_DIR": str(tmp_path)}
    loaded = load_voice_settings(environ=complete)
    assert loaded is not None and loaded.enabled
    assert loaded.elevenlabs_voice_id == "voiceid123"
    assert load_voice_settings(environ={}) is None
    assert load_voice_settings(environ={**complete, "VOICE_AGENT_ENABLED": "false"}) is None
    assert load_voice_settings(environ={"VOICE_AGENT_ENABLED": "possibly"}) is None


def test_incomplete_voice_settings_never_stop_the_bridge(tmp_path):
    partial = {"VOICE_AGENT_ENABLED": "true", "GEMINI_API_KEY": "gemini",
               "ELEVENLABS_API_KEY": "elevenlabs", "VOICE_OUTPUT_DIR": str(tmp_path)}
    assert load_voice_settings(environ=partial) is None
    relative = {**partial, "ELEVENLABS_VOICE_ID": "voiceid123", "VOICE_OUTPUT_DIR": "voice"}
    assert load_voice_settings(environ=relative) is None


def test_the_operator_flag_gates_the_voice_layer(tmp_path):
    complete = {"VOICE_AGENT_ENABLED": "true", "GEMINI_API_KEY": "gemini",
                "ELEVENLABS_API_KEY": "elevenlabs", "ELEVENLABS_VOICE_ID": "voiceid123",
                "VOICE_OUTPUT_DIR": str(tmp_path)}

    class Settings:
        voice_agent_enabled = False

    assert load_voice_settings(Settings(), environ=complete) is None


def test_the_preparation_deadline_is_the_documented_three_seconds():
    assert PREPARE_SECONDS == 3.0
