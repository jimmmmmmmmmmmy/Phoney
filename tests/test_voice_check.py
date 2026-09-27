"""Milestone 2: ``--ask`` speaks each phrase once, and the CLI guards stay honest.

The relay's contract is one bounded synthesis request per finished phrase, so
these checks assert the phrase list, the request count and the saved audio all
describe the same answer. The CLI checks run the real script but assert on
guards that fire before any configuration or network access.
<<<<<<< Updated upstream

``--chat`` is checked the same way, with one extra rule: every turn must append
to a single conversation, so a later turn is sent the earlier dialogue. Its
records are read back from standard output, which is one JSON object per line.
"""

import asyncio
import io
=======
"""

import asyncio
>>>>>>> Stashed changes
import json
from pathlib import Path
import subprocess
import sys
import wave

import httpx
import pytest

from test_voice_agent import FakeGemini, frame, text_event
from test_voice_design import load_script
from test_voice_tts import FakeSpeech
from voice_stack.settings import VoiceSettings
from voice_stack.tts import TTSError

GEMINI_KEY = "unit-test-gemini-key"
ELEVEN_KEY = "unit-test-elevenlabs-key"
VOICE = "EXAVITQu4vr4xnSDxMaL"
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
<<<<<<< Updated upstream
PLAY_GUARD = "--play needs --say, or --ask or --chat without --text-only."
=======
PLAY_GUARD = "--play needs --say, or --ask without --text-only."
>>>>>>> Stashed changes


def settings_for(tmp_path, **overrides) -> VoiceSettings:
    base = dict(gemini_api_key=GEMINI_KEY, elevenlabs_api_key=ELEVEN_KEY,
                elevenlabs_voice_id=VOICE, output_dir=str(tmp_path / "voice_output"))
    base.update(overrides)
    return VoiceSettings(**base)


def routed(gemini, speech_fake) -> httpx.MockTransport:
    """One transport for both providers, so a test needs no credential."""

    async def route(request):
        if request.url.host == "generativelanguage.googleapis.com":
            return await gemini.handle(request)
        return await speech_fake.handle(request)

    return httpx.MockTransport(route)


def run_cli(tmp_path, *arguments):
    """Run the real script against a deliberately missing environment file."""
    return subprocess.run([sys.executable, str(SCRIPTS / "voice_check.py"),
                           "--env-file", str(tmp_path / "missing.env"), *arguments],
                          capture_output=True, text=True)


def test_ask_speaks_one_request_per_phrase_and_saves_a_playable_wav(tmp_path, capsys):
    gemini = FakeGemini(frame(text_event("We open at nine. ")),
                        frame(text_event("Anything else?", finish="STOP")))
    speech_fake = FakeSpeech(b"\xff" * 160)
    module = load_script("voice_check.py")

    asyncio.run(module.ask(settings_for(tmp_path), "When do you open?",
                           transport=routed(gemini, speech_fake)))

    # One synthesis request per phrase, in order, carrying exactly that phrase.
    assert [json.loads(body)["text"] for body in speech_fake.bodies] == [
        "We open at nine.", "Anything else?"]
    assert len(speech_fake.requests) == 2
    assert len(gemini.requests) == 1

    printed = capsys.readouterr().out
    assert "We open at nine. Anything else?" in printed
    assert '"phrases_spoken": 2' in printed
    # Two 160-byte mu-law phrases at 8 kHz: 40 ms, and never the API key.
    assert '"duration_ms": 40' in printed
    assert ELEVEN_KEY not in printed and GEMINI_KEY not in printed

    written = tmp_path / "voice_output" / "voice-check-ask.wav"
    assert written.is_file()
    with wave.open(str(written)) as audio:
        assert (audio.getnchannels(), audio.getframerate(), audio.getsampwidth()) == (1, 8000, 2)
        # Two 160-byte mu-law phrases: 320 samples, one frame each.
        assert audio.getnframes() == 320


def test_ask_text_only_stays_text_and_writes_nothing(tmp_path, capsys):
    gemini = FakeGemini(frame(text_event("Nine.", finish="STOP")))
    speech_fake = FakeSpeech(b"\xff")
    module = load_script("voice_check.py")

    asyncio.run(module.ask(settings_for(tmp_path), "When?", speak=False,
                           transport=routed(gemini, speech_fake)))

    assert speech_fake.requests == []
    assert '"phrases"' in capsys.readouterr().out
    assert not (tmp_path / "voice_output").exists()


def test_voice_override_reaches_the_synthesis_url(tmp_path):
    gemini = FakeGemini(frame(text_event("Done.", finish="STOP")))
    speech_fake = FakeSpeech(b"\xff" * 160)
    module = load_script("voice_check.py")

    asyncio.run(module.ask(settings_for(tmp_path), "Hi", voice_id="overrideVoiceId123",
                           transport=routed(gemini, speech_fake)))

    assert "/text-to-speech/overrideVoiceId123/stream" in str(speech_fake.requests[0].url)


def test_say_uses_the_override_voice(tmp_path, capsys):
    speech_fake = FakeSpeech(b"\xff" * 160)
    module = load_script("voice_check.py")

    asyncio.run(module.say(settings_for(tmp_path), "Hello.", voice_id="overrideVoiceId123",
                           transport=httpx.MockTransport(speech_fake.handle)))

    assert "/text-to-speech/overrideVoiceId123/stream" in str(speech_fake.requests[0].url)
    assert '"voice_id": "overrideVoiceId123"' in capsys.readouterr().out


def test_ask_needs_a_voice_id_before_it_speaks(tmp_path):
    module = load_script("voice_check.py")
    settings = settings_for(tmp_path, elevenlabs_voice_id="")
    with pytest.raises(ValueError, match="ELEVENLABS_VOICE_ID"):
        asyncio.run(module.ask(settings, "Hi",
                               transport=httpx.MockTransport(lambda request: httpx.Response(200))))


def test_ask_refuses_to_play_text_it_never_spoke(tmp_path):
    module = load_script("voice_check.py")
    with pytest.raises(ValueError, match="Playing needs spoken audio"):
        asyncio.run(module.ask(settings_for(tmp_path), "Hi", speak=False, play=True,
                               transport=httpx.MockTransport(
                                   lambda request: httpx.Response(200))))


def test_store_audio_wraps_mulaw_and_never_mislabels_another_format(tmp_path):
    module = load_script("voice_check.py")

    path, stored = module.store_audio(settings_for(tmp_path), b"\xff" * 160, "clip")
    assert path.suffix == ".wav" and stored["duration_ms"] == 20

    # MP3 bytes keep their own extension: never wrapped in a WAV header.
    path, stored = module.store_audio(settings_for(tmp_path, elevenlabs_output_format="mp3_44100_128"),
                                     b"ID3\x03\x00", "clip")
    assert path.suffix == ".mp3"
    assert path.name == "clip.mp3"
    assert path.read_bytes() == b"ID3\x03\x00"
    assert "duration_ms" not in stored and "note" in stored


@pytest.mark.parametrize("output_format,suffix", [
    ("mp3_44100_128", "mp3"), ("pcm_16000", "pcm"), ("alaw_8000", "alaw"),
    ("opus_48000_64", "opus"),
])
def test_file_suffix_is_a_container_extension_not_a_bitrate(tmp_path, output_format, suffix):
    module = load_script("voice_check.py")
    assert module.file_suffix(output_format) == suffix
    path, stored = module.store_audio(
        settings_for(tmp_path, elevenlabs_output_format=output_format), b"\x01\x02", "clip")
    assert path.name == f"clip.{suffix}"
    assert path.read_bytes() == b"\x01\x02"


def test_store_audio_refuses_empty_audio(tmp_path):
    module = load_script("voice_check.py")
    with pytest.raises(TTSError):
        module.store_audio(settings_for(tmp_path), b"", "clip")


def test_list_voices_reports_the_override_as_the_configured_voice(tmp_path, capsys, monkeypatch):
    module = load_script("voice_check.py")
    monkeypatch.setattr(module, "list_voices",
                        lambda key: [{"voice_id": VOICE, "name": "Sarah",
                                      "category": "premade"}])
    module.show_voices(settings_for(tmp_path, elevenlabs_voice_id=""), "overrideVoiceId123")
    report = json.loads(capsys.readouterr().out)
    assert report["configured_voice_id"] == "overrideVoiceId123"
    assert report["configured_voice_present"] is False
    assert report["voices"] == [{"voice_id": VOICE, "name": "Sarah", "category": "premade"}]


def test_cli_allows_play_with_a_speaking_ask(tmp_path):
    """The old guard rejected this: ``--play`` used to require ``--say``."""
    result = run_cli(tmp_path, "--ask", "hi", "--play")
    assert result.returncode == 1
    # It got past the guard and failed on the missing configuration instead.
    assert PLAY_GUARD not in result.stderr
    assert "not found" in result.stderr


@pytest.mark.parametrize("arguments,expected", [
    (["--play"], PLAY_GUARD),
    (["--ask", "hi", "--text-only", "--play"], PLAY_GUARD),
<<<<<<< Updated upstream
    (["--chat", "--text-only", "--play"], PLAY_GUARD),
    (["--say", "hi", "--text-only"], "--text-only only applies to --ask or --chat."),
=======
    (["--say", "hi", "--text-only"], "--text-only only applies to --ask."),
>>>>>>> Stashed changes
])
def test_cli_rejects_a_conflicting_action_before_reading_configuration(tmp_path, arguments, expected):
    result = run_cli(tmp_path, *arguments)
    assert result.returncode == 1
    assert expected in result.stderr


def test_cli_no_action_prints_the_resolved_status(tmp_path):
    env = tmp_path / "voice.env"
    env.write_text(f"GEMINI_API_KEY={GEMINI_KEY}\nELEVENLABS_API_KEY={ELEVEN_KEY}\n"
                   f"ELEVENLABS_VOICE_ID={VOICE}\n")
    result = subprocess.run([sys.executable, str(SCRIPTS / "voice_check.py"),
                             "--env-file", str(env)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["configured"] is True and report["missing"] == ["VOICE_OUTPUT_DIR"]
    assert report["twilio_ready"] is True
<<<<<<< Updated upstream
    assert GEMINI_KEY not in result.stdout and ELEVEN_KEY not in result.stdout


def test_cli_allows_play_with_a_speaking_chat(tmp_path):
    result = run_cli(tmp_path, "--chat", "--play")
    assert result.returncode == 1
    # It got past the guard and failed on the missing configuration instead.
    assert PLAY_GUARD not in result.stderr
    assert "not found" in result.stderr


def test_cli_treats_chat_as_a_single_action(tmp_path):
    """Two actions in one run stay an argument error, not a silent preference."""
    result = run_cli(tmp_path, "--chat", "--ask", "hi")
    assert result.returncode == 2
    assert "not allowed with argument" in result.stderr


def chat_records(capsys) -> list[dict]:
    """Standard output is one JSON object per line, one line per turn."""
    captured = capsys.readouterr()
    return [json.loads(line) for line in captured.out.splitlines()]


def test_chat_keeps_one_conversation_so_a_later_turn_sees_earlier_turns(tmp_path, capsys):
    """Milestone L1: turn two is sent the whole dialogue, not just turn two."""
    gemini = FakeGemini(frame(text_event("We open at nine. ")),
                        frame(text_event("Anything else?", finish="STOP")))
    speech_fake = FakeSpeech(b"\xff" * 160)
    module = load_script("voice_check.py")

    asyncio.run(module.chat(settings_for(tmp_path),
                            stream=io.StringIO("When do you open?\nWho am I?\n"),
                            transport=routed(gemini, speech_fake)))

    assert len(gemini.requests) == 2
    second = json.loads(gemini.bodies[1])["contents"]
    # The question and the reply from turn one are both in the second request.
    assert [turn["parts"][0]["text"] for turn in second
            if turn["parts"][0]["text"].startswith("remote:")] == [
        "remote: When do you open?", "remote: Who am I?"]
    assert any(turn["role"] == "model" for turn in second)

    records = chat_records(capsys)
    assert [record["turn"] for record in records] == [1, 2]
    # One handoff packet, then each turn adds its own question and the reply.
    assert [record["recorded_turns"] for record in records] == [3, 5]
    assert records[1]["reply"] == "We open at nine. Anything else?"


def test_chat_speaks_one_request_per_phrase_and_saves_a_file_per_turn(tmp_path, capsys):
    gemini = FakeGemini(frame(text_event("We open at nine. ")),
                        frame(text_event("Anything else?", finish="STOP")))
    speech_fake = FakeSpeech(b"\xff" * 160)
    module = load_script("voice_check.py")

    asyncio.run(module.chat(settings_for(tmp_path),
                            stream=io.StringIO("When?\nAgain?\n"),
                            transport=routed(gemini, speech_fake)))

    # Two turns, two phrases each, and one bounded request per phrase.
    assert [json.loads(body)["text"] for body in speech_fake.bodies] == [
        "We open at nine.", "Anything else?"] * 2
    records = chat_records(capsys)
    assert [record["phrases_spoken"] for record in records] == [2, 2]
    assert [record["audio_bytes"] for record in records] == [320, 320]
    assert [record["voice_id"] for record in records] == [VOICE, VOICE]

    for name in ("voice-check-chat-01.wav", "voice-check-chat-02.wav"):
        written = tmp_path / "voice_output" / name
        assert written.is_file()
        with wave.open(str(written)) as audio:
            assert (audio.getnchannels(), audio.getframerate(),
                    audio.getsampwidth()) == (1, 8000, 2)
            assert audio.getnframes() == 320


def test_chat_prompt_and_spoken_reply_go_to_stderr(tmp_path, capsys):
    gemini = FakeGemini(frame(text_event("Nine.", finish="STOP")))
    module = load_script("voice_check.py")

    asyncio.run(module.chat(settings_for(tmp_path), stream=io.StringIO("When?\n"),
                            transport=routed(gemini, FakeSpeech(b"\xff"))))

    captured = capsys.readouterr()
    assert captured.err.startswith("you[1]> ")
    assert "clone[1]> Nine." in captured.err
    assert captured.err.rstrip().endswith("ended after 1 turn(s)")
    # Standard output stays machine readable: one record, nothing else.
    assert len([json.loads(line) for line in captured.out.splitlines()]) == 1


@pytest.mark.parametrize("typed,turns", [
    ("", 0),
    ("One?\n", 1),
    ("One?", 1),
    ("One?\n/exit\nTwo?\n", 1),
    ("One?\n/quit\nTwo?\n", 1),
    ("\n   \nOne?\n", 1),
])
def test_chat_answers_every_question_and_stops_on_a_command(tmp_path, capsys,
                                                            typed, turns):
    gemini = FakeGemini(frame(text_event("Nine.", finish="STOP")))
    module = load_script("voice_check.py")

    asyncio.run(module.chat(settings_for(tmp_path), stream=io.StringIO(typed),
                            transport=routed(gemini, FakeSpeech(b"\xff"))))

    # A blank line and a command must never reach the provider as a question.
    assert len(gemini.requests) == turns
    records = chat_records(capsys)
    assert [record["turn"] for record in records] == list(range(1, turns + 1))
    assert all(record["question"] for record in records)


def test_chat_text_only_stays_text_and_writes_nothing(tmp_path, capsys):
    gemini = FakeGemini(frame(text_event("Nine.", finish="STOP")))
    speech_fake = FakeSpeech(b"\xff")
    module = load_script("voice_check.py")

    asyncio.run(module.chat(settings_for(tmp_path), speak=False,
                            stream=io.StringIO("When?\n"),
                            transport=routed(gemini, speech_fake)))

    assert speech_fake.requests == []
    assert not (tmp_path / "voice_output").exists()
    record = chat_records(capsys)[0]
    assert record["phrases"] == ["Nine."] and "audio" not in record


def test_chat_needs_a_voice_id_before_it_speaks(tmp_path):
    module = load_script("voice_check.py")
    settings = settings_for(tmp_path, elevenlabs_voice_id="")
    with pytest.raises(ValueError, match="ELEVENLABS_VOICE_ID"):
        asyncio.run(module.chat(settings, stream=io.StringIO("Hi\n"),
                                transport=httpx.MockTransport(
                                    lambda request: httpx.Response(200))))


def test_chat_refuses_to_play_text_it_never_spoke(tmp_path):
    module = load_script("voice_check.py")
    with pytest.raises(ValueError, match="Playing needs spoken audio"):
        asyncio.run(module.chat(settings_for(tmp_path), speak=False, play=True,
                                stream=io.StringIO("Hi\n"),
                                transport=httpx.MockTransport(
                                    lambda request: httpx.Response(200))))
=======
    assert GEMINI_KEY not in result.stdout and ELEVEN_KEY not in result.stdout
>>>>>>> Stashed changes
