"""Generate outbound-only Voice SDK tokens for a reserved browser session.

The token is returned only by the authenticated call-token endpoint. It carries
neither an arbitrary phone destination nor incoming-call permission. The signed
TwiML endpoint separately validates the session identity and its one-call nonce.
"""

import re

from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant


SESSION_ID = re.compile(r"[0-9a-f]{32}\Z")
TOKEN_SETUP_SECONDS = 300
MAX_TOKEN_SECONDS = 24 * 60 * 60


def browser_voice_identity(session_id: str) -> str:
    """A Voice-compatible identity unique to one operator session."""
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        raise ValueError("Browser voice requires a valid session identifier.")
    return "phoney_" + session_id


def browser_voice_token(settings, session) -> str:
    """Sign a short-lived token without contacting Twilio or logging its value."""
    if not settings.browser_voice_enabled:
        raise ValueError("Browser voice is disabled.")
    identity = browser_voice_identity(session.id)
    # Cover the configured call duration plus bounded setup/reconnect grace.
    # Twilio caps access-token lifetimes at 24 hours.
    ttl = min(MAX_TOKEN_SECONDS, settings.max_call_seconds + TOKEN_SETUP_SECONDS)
    token = AccessToken(settings.account_sid, settings.api_key, settings.api_secret,
                        identity=identity, ttl=ttl)
    token.add_grant(VoiceGrant(incoming_allow=False,
                              outgoing_application_sid=settings.twilio_browser_app_sid))
    return token.to_jwt()
