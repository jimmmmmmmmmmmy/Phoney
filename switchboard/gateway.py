"""Bounded async facade over the blocking Twilio REST SDK.

The engine's injectable gateway contract consists of five async methods:
create_participant(conference_sid, parent_sid) -> outbound CallSid;
find_participant(conference_sid, label='callee') -> CallSid or None;
end_conference(conference_sid) -> None; end_call(call_sid) -> None;
redirect_call(call_sid, url) -> None.
Construction is side-effect free. Only create_participant places a call.
"""

import asyncio
import threading

from twilio.base.exceptions import TwilioRestException
from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client

TERMINAL_STATUSES = {"completed", "busy", "no-answer", "failed", "canceled"}


class TwilioGateway:
    def __init__(self, settings):
        self.settings = settings
        # Requests sessions are not shared between concurrent executor threads.
        self._local = threading.local()

    def _client(self):
        if not hasattr(self._local, "client"):
            key = self.settings.api_key or self.settings.account_sid
            secret = self.settings.api_secret or self.settings.auth_token
            self._local.client = Client(
                key, secret, account_sid=self.settings.account_sid,
                http_client=TwilioHttpClient(timeout=10, max_retries=0),
            )
        return self._local.client

    async def create_participant(self, conference_sid, parent_sid):
        def create():
            result = self._client().conferences(conference_sid).participants.create(
                from_=self.settings.twilio_number,
                to=self.settings.callee_number,
                label="callee",
                timeout=20 if getattr(self.settings, "voicemail_enabled", False) else 25,
                max_participants=2,
                beep="false",
                start_conference_on_enter=True,
                end_conference_on_exit=True,
                record=False,
                conference_record="do-not-record",
                status_callback=(
                    f"{self.settings.public_base_url}/calls/status/{parent_sid}"
                ),
                status_callback_method="POST",
                status_callback_event=["initiated", "ringing", "answered", "completed"],
            )
            return result.call_sid

        return await asyncio.to_thread(create)

    async def find_participant(self, conference_sid, label="callee"):
        def find():
            try:
                return self._client().conferences(conference_sid).participants(label).fetch().call_sid
            except TwilioRestException as exc:
                if exc.status == 404:
                    return None
                raise

        return await asyncio.to_thread(find)

    async def redirect_call(self, call_sid, url):
        """One redirect attempt; retries can restart the voicemail recording."""
        await asyncio.to_thread(lambda: self._client().calls(call_sid).update(url=url, method="POST"))

    async def end_conference(self, conference_sid):
        def end():
            try:
                self._client().conferences(conference_sid).update(status="completed")
            except TwilioRestException as exc:
                if exc.status != 404:
                    raise

        await asyncio.to_thread(end)

    async def end_call(self, call_sid):
        def end():
            call = self._client().calls(call_sid)
            try:
                status = call.fetch().status
                if status in TERMINAL_STATUSES:
                    return
                if status in {"queued", "initiated", "ringing"}:
                    try:
                        call.update(status="canceled")
                        return
                    except TwilioRestException as exc:
                        # The callee can answer between fetch and cancel. Only a
                        # call-state error warrants the alternate terminal update.
                        if exc.code != 21220:
                            raise
                call.update(status="completed")
            except TwilioRestException as exc:
                if exc.status != 404:
                    raise

        await asyncio.to_thread(end)
