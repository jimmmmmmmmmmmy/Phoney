"""Native Twilio human conferences with a separate streamed agent participant.

The human legs use a passive media tap and stay in Twilio's conference mixer.
Only the additional TwiML App participant uses a bidirectional Connect Stream.
Construction and TwiML generation never place a call; REST work runs off the
event loop through the existing TwilioLegs client and its bounded HTTP policy.

Provider contracts:
https://www.twilio.com/docs/voice/api/conference-participant-resource
https://www.twilio.com/docs/voice/twiml/conference
https://www.twilio.com/docs/voice/api/stream-resource
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlencode

from twilio.twiml.voice_response import VoiceResponse


ACCEPT_PROMPT = "Press 1 to connect."
SESSION_ID = re.compile(r"[0-9a-f]{32}\Z")
APP_SID = re.compile(r"AP[0-9a-fA-F]{32}\Z")
CONFERENCE_SID = re.compile(r"CF[0-9a-fA-F]{32}\Z")
CALL_SID = re.compile(r"CA[0-9a-fA-F]{32}\Z")
CONFERENCE_EVENTS = "start end join leave mute"


def _stream_identity(session_id: str, generation: int, token: str) -> None:
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        raise ValueError("Invalid native session identifier")
    if type(generation) is not int or generation < 1:
        raise ValueError("Invalid native stream generation")
    if not isinstance(token, str) or not token or len(token) > 200:
        raise ValueError("Invalid native stream token")


def _sid(value: str, pattern: re.Pattern, label: str) -> None:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"Invalid native {label} identifier")


def native_leg_twiml(settings, session, role: str, *, rejoin: bool = False) -> str:
    """Observe one human leg without putting Python in its playback path."""
    if role not in {"owner", "remote"}:
        raise ValueError("Unknown native human role")
    leg = session.legs[role]
    _stream_identity(session.id, leg.generation, leg.token)
    response = VoiceResponse()
    if not rejoin:
        stream = response.start().stream(
            url=settings.public_base_url.replace("https://", "wss://", 1)
            + f"/conference-media/{session.id}/{role}/",
            track="both_tracks" if role == "remote" else "inbound_track",
        )
        stream.parameter(name="generation", value=str(leg.generation))
        stream.parameter(name="token", value=leg.token)
    dial_options = {"method": "POST"}
    if role == "owner":
        dial_options.update(action=settings.public_base_url + f"/twilio/native-menu/{session.id}",
                            hangup_on_star=True)
    else:
        dial_options["action"] = settings.public_base_url + f"/twilio/native-finished/{session.id}/{role}"
    dial = response.dial(**dial_options)
    dial.conference(
        "phoney-" + session.id,
        participant_label=role,
        beep=False,
        start_conference_on_enter=role == "owner",
        end_conference_on_exit=role == "remote",
        muted=(role == "owner" and (rejoin or getattr(session, "mode", "human") not in {"human", "preparing"})),
        max_participants=3,
        jitter_buffer_size="small",
        status_callback=settings.public_base_url + f"/twilio/native-conference/{session.id}",
        status_callback_method="POST",
        status_callback_event=CONFERENCE_EVENTS,
    )
    return str(response)


def owner_menu_twiml(settings, session) -> str:
    """Accept an owner shortcut after star exits only their conference seat."""
    if not isinstance(session.id, str) or not SESSION_ID.fullmatch(session.id):
        raise ValueError("Invalid native session identifier")
    response = VoiceResponse()
    gather = response.gather(
        input="dtmf", num_digits=1, timeout=3, action_on_empty_result=True,
        action=settings.public_base_url + f"/twilio/native-command/{session.id}",
        method="POST",
    )
    gather.say("Press an agent number, or zero to resume speaking.", language="en-US")
    return str(response)


def owner_accept_twiml(settings, session) -> str:
    """Require acceptance before the outbound destination can be dialed."""
    if not isinstance(session.id, str) or not SESSION_ID.fullmatch(session.id):
        raise ValueError("Invalid native session identifier")
    response = VoiceResponse()
    gather = response.gather(
        num_digits=1,
        timeout=20,
        action=settings.public_base_url + f"/twilio/native-owner/{session.id}",
        method="POST",
    )
    gather.say(ACCEPT_PROMPT, language="en-US")
    response.hangup()
    return str(response)


def native_agent_twiml(settings, session_id: str, generation: int, token: str) -> str:
    """Connect only the bot's separate call to the agent media writer."""
    _stream_identity(session_id, generation, token)
    response = VoiceResponse()
    stream = response.connect().stream(
        url=settings.public_base_url.replace("https://", "wss://", 1)
        + f"/native-agent-media/{session_id}/",
    )
    stream.parameter(name="generation", value=str(generation))
    stream.parameter(name="token", value=token)
    response.hangup()
    return str(response)


class NativeConferenceGatewayMixin:
    """Add native conference operations to the existing TwilioLegs facade.

    The host supplies ``settings`` and ``_client()``. No operation retries a
    participant creation, because a lost response may conceal a successful call.
    """

    async def create_agent_participant(self, conference_sid: str, session_id: str,
                                       generation: int, token: str) -> str:
        _sid(conference_sid, CONFERENCE_SID, "conference")
        _stream_identity(session_id, generation, token)
        app_sid = self.settings.twilio_conference_app_sid
        _sid(app_sid, APP_SID, "TwiML application")
        destination = "app:" + app_sid + "?" + urlencode({
            "session_id": session_id, "generation": generation, "token": token,
        })

        def create():
            participant = self._client().conferences(conference_sid).participants.create(
                from_=self.settings.twilio_number,
                to=destination,
                label="agent",
                beep="false",
                muted=True,
                end_conference_on_exit=False,
                start_conference_on_enter=False,
                early_media=False,
                max_participants=3,
                jitter_buffer_size="small",
                record=False,
                conference_record="do-not-record",
                status_callback=self.settings.public_base_url
                + f"/twilio/native-agent/status/{session_id}/{generation}",
                status_callback_method="POST",
                status_callback_event=["initiated", "ringing", "answered", "completed"],
            )
            return participant.call_sid

        return await asyncio.to_thread(create)

    async def mute_participant(self, conference_sid: str, call_sid: str, muted: bool):
        _sid(conference_sid, CONFERENCE_SID, "conference")
        _sid(call_sid, CALL_SID, "call")
        if type(muted) is not bool:
            raise ValueError("Native participant mute must be a boolean")
        await asyncio.to_thread(lambda: self._client().conferences(conference_sid)
                                .participants(call_sid).update(muted=muted))

    async def restart_observer(self, call_sid: str, session_id: str, role: str,
                               generation: int, token: str) -> str:
        """Create a passive tap on the live call without replacing its TwiML.

        The controller bounds recovery attempts and supplies a new generation.
        Do not retry here: an unknown result may already have created a Stream.
        """
        _sid(call_sid, CALL_SID, "call")
        _stream_identity(session_id, generation, token)
        if role not in {"owner", "remote"}:
            raise ValueError("Unknown native human role")

        def create():
            stream = self._client().calls(call_sid).streams.create(
                url=self.settings.public_base_url.replace("https://", "wss://", 1)
                + f"/conference-media/{session_id}/{role}/",
                name=f"phoney-{role}-{generation}",
                track="both_tracks" if role == "remote" else "inbound_track",
                parameter1_name="generation",
                parameter1_value=str(generation),
                parameter2_name="token",
                parameter2_value=token,
            )
            return stream.sid

        return await asyncio.to_thread(create)

    async def end_conference(self, conference_sid: str):
        _sid(conference_sid, CONFERENCE_SID, "conference")
        await asyncio.to_thread(lambda: self._client().conferences(conference_sid)
                                .update(status="completed"))
