"""Design an original voice from a written description and enroll it once.

synthesizes a new voice from text. Every candidate preview is written to
``VOICE_OUTPUT_DIR`` so you can listen before choosing, and only the preview you
pick is promoted to a permanent voice in the account. Omitting ``--text`` asks
the provider to write the line the previews speak.

Designing and promoting are deliberately separate calls, because every design
request returns a *different* set of voices and rewrites the saved previews.
Promoting straight from a fresh design would therefore enroll a voice nobody had
heard. Listen first, then pass the chosen ``--generated-voice-id`` with
``--create`` to enroll exactly that voice without designing again.
pick is promoted to a permanent voice in the account.

Requires the ``text_to_voice`` permission on the key. Designed voices are
permanent, unlike the account's ``premade`` Default voices.
"""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_stack.design import (CREATE_URL, DEFAULT_DESIGN_MODEL, Design, Preview,
                                create_designed_voice, describe_design, design_previews)
from voice_stack.settings import VoiceSettings
from voice_stack.tts import TTSError


def choose(design: Design, index: int) -> Preview:
    """Select one preview, rejecting an index the provider never returned."""
    if type(index) is not int or isinstance(index, bool) or not 0 <= index < len(design):
        raise ValueError(f"Choose --index between 0 and {len(design) - 1}.")
    return design[index]


def save_previews(design: Design, directory: Path) -> list[Path]:
    """Write every preview where it can be listened to, with a matching suffix."""
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, preview in enumerate(design.previews):
        path = directory / f"design-preview-{index}.{preview.suffix}"
        path.write_bytes(preview.audio)
        paths.append(path)
    return paths


def preview_path(directory, out_dir) -> Path:
    """Resolve the output directory from the flag or ``VOICE_OUTPUT_DIR``."""
    if out_dir is None:
        if not directory:
            raise ValueError("Set VOICE_OUTPUT_DIR, or pass --out-dir, to store the previews.")
        return Path(directory)
    resolved = Path(out_dir).expanduser()
    if not resolved.is_absolute():
        raise ValueError("--out-dir must be an absolute path.")
    return resolved


def stored_preview(directory: Path, index: int) -> Path | None:
    """The preview an earlier run saved for ``index``, if it is still there.

    The suffix came from the media type the provider reported, so the file is
    found rather than assumed.
    """
    matches = sorted(Path(directory).glob(f"design-preview-{index}.*"))
    return matches[0] if matches else None


def promote(settings, args, directory: Path) -> None:
    """Enroll a preview an earlier run already saved, designing nothing new."""
    if not args.create:
        raise ValueError("--generated-voice-id needs --create to name the new voice.")
    if args.dry_run:
        print(json.dumps({
            "endpoint": CREATE_URL,
            "generated_voice_id": args.generated_voice_id,
            "would_create": args.create,
            "designs_again": False,
        }, indent=2))
        return
    created = create_designed_voice(settings.elevenlabs_api_key, args.create,
                                    args.description, args.generated_voice_id)
    voice_id = created["voice_id"]
    print(json.dumps({
        "voice_id": voice_id,
        "voice_name": args.create,
        "generated_voice_id": args.generated_voice_id,
        "requires_verification": bool(created.get("requires_verification", False)),
        "env_line": f"ELEVENLABS_VOICE_ID={voice_id}",
    }, indent=2))
    if args.play:
        saved = stored_preview(directory, args.index)
        if saved is None:
            raise ValueError(f"No saved preview for --index {args.index} in {directory}.")
        play_file(saved)


def play_file(path: Path) -> None:
    player = shutil.which("afplay")
    if not player:
        raise ValueError(f"afplay is unavailable; open {path} in an audio player.")
    subprocess.run([player, str(path)], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True,
                        help="Explicit environment file holding ELEVENLABS_API_KEY")
    parser.add_argument("--description", required=True,
                        help="Describe the voice you want: 20 to 1000 characters")
    parser.add_argument("--text",
                        help="Line the previews speak, 100 to 1000 characters; "
                             "omit it and the provider writes one")
    parser.add_argument("--model-id", default=DEFAULT_DESIGN_MODEL,
                        help="Design model identifier")
    parser.add_argument("--out-dir", type=Path,
                        help="Absolute directory for the previews; defaults to VOICE_OUTPUT_DIR")
    parser.add_argument("--index", type=int, default=0,
                        help="Preview to play and, with --create, to enroll")
    parser.add_argument("--create", metavar="NAME",
                        help="Enroll the chosen preview as a voice with this name; "
                             "needs --generated-voice-id")
    parser.add_argument("--generated-voice-id", metavar="ID",
                        help="Enroll a preview saved by an earlier run, designing nothing again")
    parser.add_argument("--play", action="store_true",
                        help="Play the chosen preview with afplay")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the resolved plan without contacting ElevenLabs")
    args = parser.parse_args()

    settings = VoiceSettings.from_env(args.env_file)
    if not settings.elevenlabs_api_key:
        raise ValueError("Set ELEVENLABS_API_KEY in the supplied environment file.")
    directory = preview_path(settings.output_dir, args.out_dir)

    if args.generated_voice_id:
        promote(settings, args, directory)
        return
    if args.create:
        # Designing again returns a *different* set of voices and overwrites the
        # saved previews, so enrolling in the same run as designing would enroll
        # a voice nobody had heard. Name the preview instead.
        raise ValueError(
            "Pass --generated-voice-id with --create to enroll a preview you have "
            "listened to; designing again here would return a different voice.")

    if args.dry_run:
        # A scoped-key sanity check that spends nothing and enrolls nothing.
        sample = (args.text or "").strip()
        print(json.dumps({
            "endpoint": "https://api.elevenlabs.io/v1/text-to-voice/design",
            "model_id": args.model_id,
            "description_chars": len(args.description.strip()),
            # The provider writes the line only when asked, so say which it is.
            "sample_text": sample or None,
            "auto_generate_text": not sample,
            "output_dir": str(directory),
            "would_create": args.create or None,
        }, indent=2))
        return

    design = design_previews(settings.elevenlabs_api_key, args.description,
                             text=args.text, model_id=args.model_id)
    paths = save_previews(design, directory)
    chosen = choose(design, args.index)
    result = {
        "model_id": args.model_id,
        "text": design.text,
        "chosen_index": args.index,
        "chosen_voice_id": chosen.generated_voice_id,
        "enroll_hint": f"--generated-voice-id {chosen.generated_voice_id} --create NAME",
        "previews": describe_design(design, paths),
    }
    print(json.dumps(result, indent=2))
    if args.play:
        play_file(paths[args.index])


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, TTSError, subprocess.CalledProcessError) as exc:
        print(str(exc) or type(exc).__name__, file=sys.stderr)
        sys.exit(1)