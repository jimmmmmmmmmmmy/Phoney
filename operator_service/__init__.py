"""Build 4: two Twilio call legs joined by a Python audio router.

Mounted beside the Build 1–3 conference path rather than replacing it: the
existing single-caller conference, passive capture, live transcript, and
voicemail routes keep their behavior, and this package adds the owner-first
callback with one bidirectional Media Stream per phone.

* ``sessions`` reserves legs, authenticates stream bindings, and runs deadlines.
* ``audio`` reads both sockets and writes both outputs on a 20 ms clock.
* ``codecs`` frames, decodes, and mixes μ-law audio without ``audioop``.
* ``controls`` parses the owner keypad, holds the saved profiles, and caches one
  fixed phrase per profile privately.
* ``routes`` owns the authenticated API, the TwiML, and the Twilio REST calls.
"""

from .audio import CallRouter
from .controls import (ClipLibrary, Command, Keypad, Profile, load_profiles,
                       load_voice_settings)
from .routes import OperatorController, TwilioLegs, leg_twiml, register_operator_routes
from .sessions import (AGENT, CONNECTED, ENDED, HUMAN, OWNER, REMOTE,
                       OperatorRejected, OperatorSession, OperatorSessions, SessionLeg)

__all__ = [
    "AGENT", "CONNECTED", "ClipLibrary", "CallRouter", "Command", "ENDED", "HUMAN",
    "Keypad", "OWNER", "OperatorController", "OperatorRejected", "OperatorSession",
    "OperatorSessions", "Profile", "REMOTE", "SessionLeg", "TwilioLegs", "leg_twiml",
    "load_profiles", "load_voice_settings", "register_operator_routes",
]
