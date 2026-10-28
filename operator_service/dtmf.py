"""Send IVR digits on the existing Twilio remote call.

Media Streams has no outbound DTMF message. The documented workaround updates
that call's TwiML to ``<Play digits>`` and then reconnects its media stream.
The Call SID does not change, and this module never creates a call.
"""

from __future__ import annotations

import asyncio
import logging

from twilio.base.exceptions import TwilioRestException

from voice_stack.ivr import validate_dtmf
from .sessions import OperatorRejected

log = logging.getLogger("uvicorn.error")


def _uncertain(exc: BaseException) -> bool:
    """True when the update may already have reached Twilio."""
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return True
    if isinstance(exc, TwilioRestException):
        return exc.status is None or exc.status >= 500
    return False


class TwilioDigitSender:
    """Serialize digit updates per session and keep the remote Call SID."""

    def __init__(self, controller):
        self.controller = controller
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    async def __call__(self, session_id: str, digits: str) -> None:
        digits = validate_dtmf(digits)
        async with self._lock(session_id):
            await self._send(session_id, digits)

    async def _send(self, session_id: str, digits: str) -> None:
        from .routes import leg_twiml

        store = self.controller.store
        call_sid, generation, token, previous = await store.begin_remote_dtmf(session_id)
        session = store.find(session_id)
        twiml = leg_twiml(
            self.controller.settings.public_base_url, session_id, "remote",
            generation, token, digits=digits)
        try:
            await self.controller.dialer.replace_twiml(call_sid, twiml)
        except Exception as exc:
            uncertain = _uncertain(exc)
            if not uncertain:
                await store.abort_remote_dtmf(session_id, previous)
            self.controller.trace(
                session_id, "dtmf-uncertain" if uncertain else "dtmf-failed",
                count=len(digits), error=type(exc).__name__)
            if uncertain:
                log.warning("operator_dtmf_uncertain session=%s count=%d", session_id, len(digits))
                return
            raise OperatorRejected("dtmf-failed") from exc
        same = session.legs["remote"].call_sid == call_sid if session is not None else False
        self.controller.trace(
            session_id, "dtmf-sent", count=len(digits), generation=generation,
            call_sid_unchanged=same)
        log.info("operator_dtmf_sent session=%s count=%d generation=%s",
                 session_id, len(digits), generation)
