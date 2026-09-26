"""Standalone voice layer: Google Gemini dialogue with ElevenLabs cloned speech.

Deliberately independent of the Twilio bridge. Nothing in ``app.py`` imports
this package, so the implemented Build 3 conference, capture, and Deepgram
transcription path is unaffected.

* ``settings`` parses and validates provider configuration.
* ``audio`` converts between μ-law and PCM without ``audioop``.
* ``agent`` streams Gemini replies over the text-only GenerateContent adapter.
* ``tts`` streams cloned speech and enrolls the owner's voice once.
* ``design`` creates an original voice from a written description, for when a
  real person's recordings are not available to clone.

Only ``VoiceSettings`` is re-exported here: ``agent`` and ``tts`` pull in
``httpx``, and importing this package should not force that on a caller that
only needs configuration. Import the submodule you need explicitly.
"""

from .settings import VoiceSettings

__all__ = ["VoiceSettings"]