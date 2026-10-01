"""Check the Gemini and ElevenLabs credentials without placing a phone call.

Four independent actions keep provider problems separate from Twilio routing:
``--list-voices`` proves the ElevenLabs key and voice access for free, ``--say``
renders one phrase to a file you can listen to, ``--ask`` sends one question to
Gemini, and ``--chat`` holds a typed conversation so a later turn can rely on
what an earlier turn said. One synthesis request is made per phrase, matching
the relay's contract. Use ``--text-only`` to keep ``--ask`` or ``--chat`` to
text. Run with no action to print the resolved status.
"""

import argparse
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx

from voice_stack.agent import Conversation, GeminiError
from voice_stack.audio import duration_ms, write_ulaw_wav
from voice_stack.relay import speak_reply
from voice_stack.settings import VoiceSettings
from voice_stack.tts import TTSError, list_voices, speech


def require(value, name: str) -> None:
    if not value:
        raise ValueError(f"Set {name} in the supplied environment file.")


def describe(settings) -> dict:
    """Resolved configuration by name: no value here is a credential."""
    return {
        "enabled": settings.enabled,
        "configured": settings.configured,
        "missing": settings.missing,
        "gemini_model": settings.gemini_model,
        "elevenlabs_voice_id": settings.elevenlabs_voice_id,
        "elevenlabs_model": settings.elevenlabs_model,
        "output_format": settings.elevenlabs_output_format,
        "twilio_ready": settings.twilio_ready,
    }


def show_voices(settings, voice_id: str = "") -> None:
    """Side-effect-free credential check: no audio is generated, no credits used."""
    require(settings.elevenlabs_api_key, "ELEVENLABS_API_KEY")
    voice_id = voice_id or settings.elevenlabs_voice_id
    voices = list_voices(settings.elevenlabs_api_key)
    known = {voice.get("voice_id") for voice in voices}
    print(json.dumps({
        "count": len(voices),
        "configured_voice_id": voice_id,
        "configured_voice_present": voice_id in known,
        "voices": [{"voice_id": voice.get("voice_id"), "name": voice.get("name"),
                    "category": voice.get("category")} for voice in voices],
    }, indent=2))


def file_suffix(output_format: str) -> str:
    """A real extension for the bytes, never the format name itself.

    ``mp3_44100_128`` is a container plus a bitrate, so the extension is the
    part before the first underscore. Naming a file ``.mp3_44100_128`` would
    mislabel the container and defeat every tool that dispatches on a suffix.
    """
    return output_format.split("_", 1)[0]


def store_audio(settings, audio: bytes, stem: str) -> tuple[Path, dict]:
    """Write generated audio where it can be listened to, never mislabelled."""
    if not audio:
        raise TTSError("Speech request returned no audio")
    directory = settings.output_path
    directory.mkdir(parents=True, exist_ok=True)
    if settings.twilio_ready:
        path = write_ulaw_wav(directory / f"{stem}.wav", audio)
        return path, {"file": str(path), "duration_ms": duration_ms(audio)}
    # Never label PCM, MP3 or Opus bytes as mu-law or wrap them in a WAV header.
    path = directory / f"{stem}.{file_suffix(settings.elevenlabs_output_format)}"
    path.write_bytes(audio)
    return path, {"file": str(path),
                  "note": "Not mu-law: play it with a tool that understands this format."}


def relay_options(settings, voice_id: str, speak: bool) -> dict:
    """Arguments for one relayed reply; a text-only run drops the synthesis half."""
    options = {
        "gemini_api_key": settings.gemini_api_key,
        "model": settings.gemini_model,
        "max_output_tokens": settings.max_reply_tokens,
        "request_timeout": settings.request_timeout,
    }
    if speak:
        options.update(tts_api_key=settings.elevenlabs_api_key, voice_id=voice_id,
                       voice_model=settings.elevenlabs_model,
                       output_format=settings.elevenlabs_output_format,
                       delivery=settings.delivery)
    return options


def require_providers(settings, voice_id: str, speak: bool) -> None:
    """Fail before the first request when the run is not fully configured."""
    require(settings.gemini_api_key, "GEMINI_API_KEY")
    if speak:
        require(settings.elevenlabs_api_key, "ELEVENLABS_API_KEY")
        require(voice_id, "ELEVENLABS_VOICE_ID")
        require(settings.output_dir, "VOICE_OUTPUT_DIR")


async def ask(settings, question: str, *, voice_id: str | None = None,
              speak: bool = True, play: bool = False, transport=None) -> None:
    """Ask Gemini one question and, by default, speak every finished phrase.

    One bounded synthesis request per phrase is the relay's contract, so the
    phrase count, the request count and the saved audio all describe the same
    answer. A generation that fails mid-stream raises before anything plays.
    """
    voice_id = voice_id or settings.elevenlabs_voice_id
    require_providers(settings, voice_id, speak)
    if play and not speak:
        # Playing needs a rendered file, so refuse before spending a request.
        raise ValueError("Playing needs spoken audio; drop --text-only to play.")
    conversation = Conversation.handoff(
        [("owner", "Answer the test question that follows as the owner's delegate.")],
        goal="Reply to the caller in one or two short spoken sentences.")
    conversation.add_remote(question)
    async with httpx.AsyncClient(transport=transport) as http:
        reply = await speak_reply(
            http, conversation, on_text=lambda delta: print(delta, end="", flush=True),
            **relay_options(settings, voice_id, speak))
    print()
    result = {
        "model": settings.gemini_model,
        "first_text_ms": reply.first_text_ms,
        "total_ms": reply.total_ms,
        "phrases": reply.phrases,
        "recorded_turns": len(conversation.contents),
    }
    path = None
    if speak:
        path, stored = store_audio(settings, reply.audio, "voice-check-ask")
        result.update(stored, voice_id=voice_id, phrases_spoken=len(reply.phrases),
                      first_audio_ms=reply.first_audio_ms, audio_bytes=len(reply.audio))
    print(json.dumps(result, indent=2))
    if play:
        play_file(path)


CHAT_COMMANDS = frozenset({"/exit", "/quit"})


async def chat(settings, *, voice_id: str | None = None, speak: bool = True,
               play: bool = False, stream=None, transport=None) -> None:
    """Converse over many typed turns, keeping the history a later turn needs.

    Every turn appends to the same conversation, so the provider sees the whole
    dialogue and an answer can use a detail given several turns earlier. Typing
    ``/exit`` or ``/quit``, or closing the input, ends the session; a blank line
    is skipped without reaching the provider.

    The prompt and the streamed reply go to standard error so standard output
    stays one JSON object per line, one line per turn, which can be piped into
    another tool. Each spoken turn is saved as its own playable file, and
    ``--play`` plays a turn as soon as it has been spoken rather than at the end
    of the session.
    """
    voice_id = voice_id or settings.elevenlabs_voice_id
    require_providers(settings, voice_id, speak)
    if play and not speak:
        # Playing needs a rendered file, so refuse before spending a request.
        raise ValueError("Playing needs spoken audio; drop --text-only to play.")
    conversation = Conversation.handoff(
        [("owner", "Answer the owner's questions as the owner's delegate.")],
        goal="Reply to the caller in one or two short spoken sentences.")
    options = relay_options(settings, voice_id, speak)
    source = stream if stream is not None else sys.stdin
    turns = 0

    async with httpx.AsyncClient(transport=transport) as http:
        while True:
            print(f"you[{turns + 1}]> ", end="", file=sys.stderr, flush=True)
            # Read off the event loop: a terminal stalls until the line arrives.
            line = await asyncio.to_thread(source.readline)
            if not line:
                break
            question = line.strip()
            if not question:
                continue
            if question in CHAT_COMMANDS:
                break
            turns += 1
            conversation.add_remote(question)
            print(f"clone[{turns}]> ", end="", file=sys.stderr, flush=True)
            reply = await speak_reply(
                http, conversation, **options,
                on_text=lambda delta: print(delta, end="", file=sys.stderr, flush=True))
            print(file=sys.stderr)
            record = {
                "turn": turns,
                "question": question,
                "reply": reply.text,
                "phrases": reply.phrases,
                "model": settings.gemini_model,
                "recorded_turns": len(conversation.contents),
                "first_text_ms": reply.first_text_ms,
                "total_ms": reply.total_ms,
            }
            path = None
            if speak:
                path, stored = store_audio(settings, reply.audio,
                                           f"voice-check-chat-{turns:02d}")
                record.update(stored, voice_id=voice_id,
                              phrases_spoken=len(reply.phrases),
                              first_audio_ms=reply.first_audio_ms,
                              audio_bytes=len(reply.audio))
            # One line per turn: indented output here would break the piping.
            print(json.dumps(record))
            if play:
                play_file(path)
    print(f"ended after {turns} turn(s)", file=sys.stderr)


async def say(settings, text: str, *, voice_id: str | None = None, play: bool = False,
              transport=None) -> None:
    """Render one phrase to a file, using the override voice when supplied."""
    voice_id = voice_id or settings.elevenlabs_voice_id
    require(settings.elevenlabs_api_key, "ELEVENLABS_API_KEY")
    require(voice_id, "ELEVENLABS_VOICE_ID")
    require(settings.output_dir, "VOICE_OUTPUT_DIR")
    async with httpx.AsyncClient(transport=transport) as http:
        audio = await speech(http, settings.elevenlabs_api_key, voice_id,
                             text, model=settings.elevenlabs_model,
                             output_format=settings.elevenlabs_output_format,
                             delivery=settings.delivery,
                             timeout=settings.request_timeout)
    path, stored = store_audio(settings, audio, "voice-check")
    result = {"voice_id": voice_id, "model": settings.elevenlabs_model,
              "output_format": settings.elevenlabs_output_format, "bytes": len(audio)}
    result.update(stored)
    print(json.dumps(result, indent=2))
    if play:
        play_file(path)


def play_file(path: Path) -> None:
    player = shutil.which("afplay")
    if not player:
        raise ValueError(f"afplay is unavailable; open {path} in an audio player.")
    subprocess.run([player, str(path)], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True,
                        help="Explicit environment file holding the provider credentials")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--list-voices", action="store_true",
                        help="List account voices; free, and proves the key works")
    action.add_argument("--ask", metavar="TEXT",
                        help="Send one question to Gemini and speak the answer")
    action.add_argument("--chat", action="store_true",
                        help="Hold a typed conversation and speak every answer")
    action.add_argument("--say", metavar="TEXT", help="Render TEXT in the configured voice")
    parser.add_argument("--voice-id",
                        help="Override ELEVENLABS_VOICE_ID for this run only")
    parser.add_argument("--text-only", action="store_true",
                        help="With --ask or --chat, print the answer without speaking it")
    parser.add_argument("--play", action="store_true", help="Play the rendered file with afplay")
    args = parser.parse_args()
    relayed = bool(args.ask or args.chat)
    if args.text_only and not relayed:
        raise ValueError("--text-only only applies to --ask or --chat.")
    speaks = bool(args.say) or (relayed and not args.text_only)
    if args.play and not speaks:
        raise ValueError("--play needs --say, or --ask or --chat without --text-only.")

    settings = VoiceSettings.from_env(args.env_file)
    if args.list_voices:
        show_voices(settings, args.voice_id or "")
    elif args.ask:
        asyncio.run(ask(settings, args.ask, voice_id=args.voice_id,
                        speak=not args.text_only, play=args.play))
    elif args.chat:
        asyncio.run(chat(settings, voice_id=args.voice_id,
                         speak=not args.text_only, play=args.play))
    elif args.say:
        asyncio.run(say(settings, args.say, voice_id=args.voice_id, play=args.play))
    else:
        print(json.dumps(describe(settings), indent=2))


if __name__ == "__main__":
    try:
        main()
    except TimeoutError:
        print("A provider request timed out before it answered.", file=sys.stderr)
        sys.exit(1)
    except (ValueError, OSError, TTSError, GeminiError, httpx.HTTPError,
            subprocess.CalledProcessError) as exc:
        # A dropped connection can arrive with an empty message, so name the
        # type as well: "The check failed." alone is not diagnosable.
        print(str(exc) or type(exc).__name__, file=sys.stderr)
        sys.exit(1)
