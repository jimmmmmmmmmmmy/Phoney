"""Native conference control and passive media with local fake phone providers."""

import asyncio
import base64
from contextlib import contextmanager
from dataclasses import replace
import json
import uuid
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient
import pytest
from twilio.base.exceptions import TwilioRestException

from app import create_app
from operator_service import OperatorSessions
from operator_service.native_conference import NativeConferenceRouter
from operator_service.routes import OperatorController
from operator_service.sessions import (AGENT, ANNOUNCING, CONNECTED, ENDED,
                                       HUMAN, OWNER, OWNER_PROMPT, OWNER_RINGING, PREPARING, REMOTE,
                                       OperatorRejected)
from test_media_webhooks import Gateway, signed_post
from test_operator_keypad import (Provider, Registry, SETTINGS, Socket,
                                  OWNER_SID, REMOTE_SID, OWNER_STREAM,
                                  REMOTE_STREAM, until)
from voice_stack.settings import VoiceSettings


BOT_SID = "CA" + "5" * 32
BOT_STREAM = "MZ" + "6" * 32
CONFERENCE = "CF" + "7" * 32
DESTINATION = "+12025550103"
NATIVE_SETTINGS = replace(SETTINGS, native_conference_enabled=True,
                          twilio_conference_app_sid="AP" + "8" * 32)


class NativeDialer:
    def __init__(self):
        self.created, self.operations, self.ended, self.ended_conferences = [], [], [], []
        self.controller = None
        self.bot_hold = None
        self.bot_entered = asyncio.Event()
        self.fail_owner_mute = False
        self.owner_mute_hold = None
        self.owner_mute_entered = asyncio.Event()
        self.fail_bot_mute_404 = False
        self.actual_muted = {OWNER_SID: False, BOT_SID: True}

    async def create_leg(self, **kwargs):
        self.created.append(kwargs)
        return OWNER_SID if len(self.created) == 1 else REMOTE_SID

    async def create_agent_participant(self, conference_sid, session_id, generation, token):
        self.operations.append(("create-agent", conference_sid, generation))
        self.bot_entered.set()
        if self.bot_hold is not None:
            await self.bot_hold.wait()
        if self.controller is not None:
            session = self.controller.store.find(session_id)
            if session and session.active:
                state = self.controller.native_state(session_id)
                socket = Socket(self.controller, session_id, REMOTE)
                self.controller.router(session_id).channels[REMOTE].attach(
                    socket, BOT_STREAM, generation, {})
                state.bot_ready.set()
                self.bot_socket = socket
        return BOT_SID

    async def mute_participant(self, conference_sid, call_sid, muted):
        self.operations.append(("mute", call_sid, muted))
        if call_sid == OWNER_SID and muted and self.fail_owner_mute:
            raise RuntimeError("Owner mute failed")
        if call_sid == OWNER_SID and muted and self.owner_mute_hold is not None:
            self.owner_mute_entered.set()
            await self.owner_mute_hold.wait()
        if call_sid == BOT_SID and muted and self.fail_bot_mute_404:
            raise TwilioRestException(404, "https://api.twilio.example", msg="Participant ended")
        self.actual_muted[call_sid] = muted

    async def end_call(self, call_sid):
        self.ended.append(call_sid)

    async def end_conference(self, conference_sid):
        self.ended_conferences.append(conference_sid)


class NativeHarness:
    def __init__(self, tmp_path):
        self.provider, self.registry, self.dialer = Provider(), Registry(), NativeDialer()
        self.store = OperatorSessions(NATIVE_SETTINGS)
        self.observed, self.delivered, self.ends = [], [], []
        self.voice = VoiceSettings(enabled=True, gemini_api_key="test", elevenlabs_api_key="test",
            output_dir=str(tmp_path), elevenlabs_voice_id="")
        self.controller = OperatorController(NATIVE_SETTINGS, self.store, self.dialer,
            voice=self.voice, registry=self.registry, provider_transport=self.provider.transport(),
            on_native_audio=lambda *args: self.observed.append(args),
            on_agent_turn=lambda *args, **kwargs: self.delivered.append((args, kwargs)),
            on_call_end=lambda s: self.ends.append(s.id))
        self.dialer.controller = self.controller

    async def joined(self, *, observers=True):
        self.session, _ = await self.store.reserve_outbound(DESTINATION, "Get a quote", str(uuid.uuid4()))
        call = self.session
        call.native_conference = True
        await self.store.bind_call_sid(call.id, OWNER, OWNER_SID)
        await self.store.bind_call_sid(call.id, REMOTE, REMOTE_SID)
        call.canonical_call_sid = REMOTE_SID
        await self.store.mark_owner_prompt(call.id)
        await self.store.begin_remote_dial(call.id)
        self.state = self.controller.native_state(call.id)
        self.state.sid = CONFERENCE
        self.state.participants.update((OWNER, REMOTE))
        for role, stream in ((OWNER, OWNER_STREAM), (REMOTE, REMOTE_STREAM)):
            call.legs[role].attached = observers
            call.legs[role].stream_sid = stream if observers else ""
        await self.controller.native_connected(call.id)
        self.router = self.controller.router(call.id)
        return call

    async def close(self):
        await self.store.close()


class ReadSocket:
    """A finite inbound WebSocket; passive observers must never send media."""
    def __init__(self, events):
        self.events = list(events)
        self.sent, self.closed = [], []
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def receive(self):
        if self.events:
            return {"type": "websocket.receive", "text": json.dumps(self.events.pop(0))}
        return {"type": "websocket.disconnect"}

    async def send_json(self, value):
        self.sent.append(value)

    async def close(self, code):
        self.closed.append(code)


def start_event(call, role):
    leg = call.legs[role]
    stream = OWNER_STREAM if role == OWNER else REMOTE_STREAM
    return {"event": "start", "streamSid": stream, "start": {
        "accountSid": NATIVE_SETTINGS.account_sid, "callSid": leg.call_sid,
        "streamSid": stream,
        "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
        "customParameters": {"token": leg.token, "generation": str(leg.generation)}}}


def media_event(stream, track, marker, timestamp=0):
    return {"event": "media", "streamSid": stream, "media": {
        "track": track, "timestamp": str(timestamp),
        "payload": base64.b64encode(bytes([marker]) * 160).decode("ascii")}}


def bot_start(harness, *, token=None):
    return {"event": "start", "streamSid": BOT_STREAM, "start": {
        "accountSid": NATIVE_SETTINGS.account_sid, "callSid": BOT_SID,
        "streamSid": BOT_STREAM,
        "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
        "customParameters": {"generation": str(harness.state.bot_generation),
                             "token": harness.state.bot_token if token is None else token}}}


def test_native_takeover_mutes_owner_before_bot_and_release_reverses_safely(tmp_path):
    async def run():
        h = NativeHarness(tmp_path)
        call = await h.joined()
        assert isinstance(h.router, NativeConferenceRouter)
        assert not h.router.channels[OWNER].attached
        await h.controller.set_mode(call.id, AGENT, slot="1")
        await until(lambda: call.mode == AGENT and not h.controller.playing(call.id))
        mutes = [op for op in h.dialer.operations if op[0] == "mute"]
        assert mutes[:2] == [("mute", OWNER_SID, True), ("mute", BOT_SID, False)]
        assert call.native_owner_muted
        assert h.dialer.bot_socket.frames(0x2A)
        assert h.delivered[-1][1]["delivery"] == "played"
        assert not h.router.channels[OWNER].attached
        await h.controller.set_mode(call.id, HUMAN)
        assert call.mode == HUMAN and not call.native_owner_muted
        assert [op for op in h.dialer.operations if op[0] == "mute"][-2:] == [
            ("mute", BOT_SID, True), ("mute", OWNER_SID, False)]
        await h.close()

    asyncio.run(run())


def test_owner_mute_failure_never_unmutes_bot(tmp_path):
    async def run():
        h = NativeHarness(tmp_path)
        call = await h.joined()
        h.dialer.fail_owner_mute = True
        await h.controller.set_mode(call.id, AGENT, slot="1")
        await until(lambda: call.mode == HUMAN and not h.controller.playing(call.id))
        assert ("mute", BOT_SID, False) not in h.dialer.operations
        assert not call.native_owner_muted
        assert not h.delivered
        await h.close()

    asyncio.run(run())


def test_native_observer_tracks_share_clock_without_writes_or_relay_and_drop_keeps_call(tmp_path):
    async def run():
        h = NativeHarness(tmp_path)
        call = await h.joined(observers=False)
        h.controller.elapsed_ms = lambda session_id: 0
        socket = ReadSocket([start_event(call, REMOTE),
            media_event(REMOTE_STREAM, "inbound", 1),
            media_event(REMOTE_STREAM, "outbound", 2),
            media_event(REMOTE_STREAM, "inbound", 3, timestamp=20)])
        await h.router.serve_observer(socket, REMOTE, h.store)
        assert socket.sent == []
        assert not h.router.channels[OWNER].attached and not h.router.channels[REMOTE].attached
        assert [(args[1], args[2], args[3][0], args[4]) for args in h.observed] == [
            (REMOTE, "inbound", 1, 0), (REMOTE, "outbound", 2, 0), (REMOTE, "inbound", 3, 20)]
        assert call.active and call.phase == CONNECTED
        assert h.dialer.ended == [] and h.dialer.ended_conferences == []
        assert h.ends == []
        with pytest.raises(RuntimeError, match="must not enter the relay"):
            h.router.forward(REMOTE, bytes([1]) * 160)
        await h.close()

    asyncio.run(run())


def test_passive_owner_stream_rejects_conference_playback_track(tmp_path):
    async def run():
        h = NativeHarness(tmp_path)
        call = await h.joined(observers=False)
        socket = ReadSocket([start_event(call, OWNER),
            media_event(OWNER_STREAM, "inbound", 1),
            media_event(OWNER_STREAM, "outbound", 2)])
        await h.router.serve_observer(socket, OWNER, h.store)
        assert [(args[1], args[2], args[3][0]) for args in h.observed] == [(OWNER, "inbound", 1)]
        assert socket.sent == [] and call.active
        await h.close()

    asyncio.run(run())


@pytest.mark.parametrize("token,expected_bound", [(None, True), ("wrong-token", False)])
def test_native_bot_audio_is_ignored_and_bad_token_cannot_claim_writer(tmp_path, token, expected_bound):
    async def run():
        h = NativeHarness(tmp_path)
        await h.joined()
        h.state.bot_application_call = BOT_SID
        socket = ReadSocket([bot_start(h, token=token),
                             media_event(BOT_STREAM, "inbound", 9)])
        await h.router.serve_bot(socket)
        assert h.observed == []
        assert h.state.bot_token_used is expected_bound
        assert not h.router.channels[REMOTE].attached
        assert socket.closed == [1000 if expected_bound else 1008]
        await h.close()

    asyncio.run(run())


def test_release_during_bot_creation_revokes_transition_before_any_unmute(tmp_path):
    async def run():
        h = NativeHarness(tmp_path)
        call = await h.joined()
        h.dialer.bot_hold = asyncio.Event()
        await h.store.set_mode(call.id, PREPARING)
        epoch = call.reply_epoch
        transition = asyncio.create_task(h.controller.transition_audio_mode(call.id, epoch, ANNOUNCING))
        await h.dialer.bot_entered.wait()
        release = asyncio.create_task(h.controller._release(call.id))
        await until(lambda: call.mode == HUMAN)
        h.dialer.bot_hold.set()
        with pytest.raises(OperatorRejected, match="stale-reply"):
            await transition
        await release
        assert ("mute", BOT_SID, False) not in h.dialer.operations
        assert not call.native_owner_muted
        await h.close()

    asyncio.run(run())


def test_canceled_inflight_owner_mute_settles_before_owner_is_restored(tmp_path):
    async def run():
        h = NativeHarness(tmp_path)
        call = await h.joined()
        h.dialer.owner_mute_hold = asyncio.Event()
        await h.store.set_mode(call.id, PREPARING)
        transition = asyncio.create_task(h.controller.transition_audio_mode(
            call.id, call.reply_epoch, ANNOUNCING))
        await h.dialer.owner_mute_entered.wait()
        transition.cancel()
        release = asyncio.create_task(h.controller._release(call.id))
        await until(lambda: call.mode == HUMAN)
        assert not release.done()
        h.dialer.owner_mute_hold.set()
        with pytest.raises(asyncio.CancelledError):
            await transition
        await release
        assert not h.dialer.actual_muted[OWNER_SID]
        assert h.dialer.actual_muted[BOT_SID]
        assert not call.native_owner_muted
        assert ("mute", BOT_SID, False) not in h.dialer.operations
        await h.close()

    asyncio.run(run())


def test_terminal_bot_participant_404_still_restores_owner(tmp_path):
    async def run():
        h = NativeHarness(tmp_path)
        call = await h.joined()
        await h.store.set_mode(call.id, PREPARING)
        await h.controller.transition_audio_mode(call.id, call.reply_epoch, AGENT)
        assert h.dialer.actual_muted[OWNER_SID]
        h.dialer.fail_bot_mute_404 = True
        await h.controller._release(call.id)
        assert call.mode == HUMAN and not call.native_owner_muted
        assert not h.dialer.actual_muted[OWNER_SID]
        assert call.active
        await h.close()

    asyncio.run(run())


def test_session_end_cleans_up_late_agent_creation_and_conference_once(tmp_path):
    async def run():
        h = NativeHarness(tmp_path)
        call = await h.joined()
        h.dialer.bot_hold = asyncio.Event()
        h.state.bot_task = h.store.spawn(h.controller._provision_native_bot(call))
        await h.dialer.bot_entered.wait()
        await h.controller.end(call.id, "test-end")
        await until(lambda: OWNER_SID in h.dialer.ended and REMOTE_SID in h.dialer.ended)
        assert call.phase == ENDED
        assert call.id not in h.controller.routers
        assert h.dialer.ended_conferences == [CONFERENCE]
        assert h.ends == [call.id]
        h.dialer.bot_hold.set()
        await h.state.bot_task
        assert h.dialer.ended.count(BOT_SID) == 1
        await h.controller.end(call.id, "duplicate-end")
        assert h.dialer.ended_conferences == [CONFERENCE] and h.ends == [call.id]
        await h.close()

    asyncio.run(run())


@contextmanager
def native_client(*, raise_server_exceptions=True):
    dialer = NativeDialer()
    with TestClient(create_app(NATIVE_SETTINGS, gateway=Gateway(), operator_dialer=dialer),
                    base_url=NATIVE_SETTINGS.public_base_url,
                    raise_server_exceptions=raise_server_exceptions) as client:
        dialer.controller = client.app.state.operator_controller
        yield client, dialer


def wait_client(client, predicate):
    client.portal.call(until, predicate)


def accepted_call(client, dialer):
    response = client.post("/api/calls/outbound", json={"to": DESTINATION, "goal": "Get a quote"},
        headers={"Authorization": "Bearer " + NATIVE_SETTINGS.operator_admin_token,
                 "Idempotency-Key": str(uuid.uuid4())})
    assert response.status_code == 202
    session_id = response.json()["session_id"]
    wait_client(client, lambda: len(dialer.created) == 1)
    call = client.app.state.operator.find(session_id)
    response = signed_post(client, NATIVE_SETTINGS, f"/twilio/native-owner/{session_id}", {
        "AccountSid": NATIVE_SETTINGS.account_sid, "CallSid": OWNER_SID, "Digits": "1"})
    assert response.status_code == 200
    wait_client(client, lambda: len(dialer.created) == 2 and bool(call.legs[REMOTE].call_sid))
    return call


def conference_join(client, call, role, sequence):
    return signed_post(client, NATIVE_SETTINGS, f"/twilio/native-conference/{call.id}", {
        "AccountSid": NATIVE_SETTINGS.account_sid, "ConferenceSid": CONFERENCE,
        "FriendlyName": "phoney-" + call.id, "ParticipantLabel": role,
        "CallSid": call.legs[role].call_sid, "StatusCallbackEvent": "participant-join",
        "SequenceNumber": str(sequence)})


def test_signed_phone_menu_only_requests_agent_after_participants_connect():
    with native_client() as (client, dialer):
        call = accepted_call(client, dialer)
        controller = client.app.state.operator_controller
        commands = []

        async def take_over(session_id, digit, **kwargs):
            commands.append((session_id, digit))

        controller._take_over = take_over
        menu = signed_post(client, NATIVE_SETTINGS, f"/twilio/native-menu/{call.id}", {
            "AccountSid": NATIVE_SETTINGS.account_sid, "CallSid": OWNER_SID,
            "DialCallStatus": "answered"})
        assert ET.fromstring(menu.text).find("Gather") is not None
        command_form = {"AccountSid": NATIVE_SETTINGS.account_sid,
                        "CallSid": OWNER_SID, "Digits": "2"}
        response = signed_post(client, NATIVE_SETTINGS, f"/twilio/native-command/{call.id}", command_form)
        assert response.status_code == 200 and commands == []
        assert conference_join(client, call, OWNER, 1).status_code == 204
        assert conference_join(client, call, REMOTE, 2).status_code == 204
        assert call.phase == CONNECTED
        signed_post(client, NATIVE_SETTINGS, f"/twilio/native-menu/{call.id}", {
            "AccountSid": NATIVE_SETTINGS.account_sid, "CallSid": OWNER_SID,
            "DialCallStatus": "answered"})
        response = signed_post(client, NATIVE_SETTINGS, f"/twilio/native-command/{call.id}", command_form)
        assert response.status_code == 200
        wait_client(client, lambda: bool(commands))
        assert commands == [(call.id, "2")]
        assert ET.fromstring(response.text).find("Start") is None


def test_native_owner_answer_arms_acceptance_deadline_without_dialing_recipient():
    with native_client() as (client, dialer):
        response = client.post("/api/calls/outbound", json={"to": DESTINATION, "goal": "Get a quote"},
            headers={"Authorization": "Bearer " + NATIVE_SETTINGS.operator_admin_token,
                     "Idempotency-Key": str(uuid.uuid4())})
        assert response.status_code == 202
        store = client.app.state.operator
        call = store.find(response.json()["session_id"])
        wait_client(client, lambda: len(dialer.created) == 1 and bool(call.legs[OWNER].call_sid))
        assert call.phase == OWNER_RINGING
        assert (call.id, "owner_ring") in store._timers
        form = {"AccountSid": NATIVE_SETTINGS.account_sid, "CallSid": OWNER_SID,
                "CallStatus": "in-progress"}
        path = f"/twilio/status/{call.id}/{OWNER}"
        assert signed_post(client, NATIVE_SETTINGS, path, form).status_code == 204
        assert call.phase == OWNER_PROMPT and call.legs[OWNER].answered
        assert (call.id, "owner_ring") not in store._timers
        acceptance_timer = store._timers[(call.id, "owner_accept")]
        assert not acceptance_timer.done()
        assert len(dialer.created) == 1 and not call.legs[REMOTE].call_sid
        assert not call.legs[OWNER].attached
        # An answered callback starts the prompt budget, while repeated delivery
        # cannot extend it or substitute for the owner's explicit acceptance.
        assert signed_post(client, NATIVE_SETTINGS, path, form).status_code == 204
        assert store._timers[(call.id, "owner_accept")] is acceptance_timer
        assert len(dialer.created) == 1 and call.phase == OWNER_PROMPT


def test_owner_menu_takeover_skips_absent_owner_mute_and_rejoin_reconciles_actual_mode():
    with native_client() as (client, dialer):
        call = accepted_call(client, dialer)
        controller = client.app.state.operator_controller
        assert conference_join(client, call, OWNER, 1).status_code == 204
        assert conference_join(client, call, REMOTE, 2).status_code == 204
        state = controller.native_state(call.id)
        state.owner_menu = True
        state.participants.discard(OWNER)
        client.portal.call(controller.store.set_mode, call.id, PREPARING)
        client.portal.call(controller.transition_audio_mode, call.id, call.reply_epoch, ANNOUNCING)
        assert call.native_owner_muted
        assert ("mute", OWNER_SID, True) not in dialer.operations
        assert ("mute", BOT_SID, False) in dialer.operations
        assert conference_join(client, call, OWNER, 3).status_code == 204
        assert dialer.operations[-1] == ("mute", OWNER_SID, True)
        assert not state.owner_menu and call.native_owner_muted
        # A queued rejoin generated while the bot spoke must respect a later
        # return to human mode when the signed participant callback arrives.
        state.owner_menu = True
        state.participants.discard(OWNER)
        client.portal.call(controller._release, call.id)
        assert call.mode == HUMAN
        assert conference_join(client, call, OWNER, 4).status_code == 204
        assert dialer.operations[-1] == ("mute", OWNER_SID, False)
        assert not call.native_owner_muted


def test_failed_owner_rejoin_unmute_is_retried_for_same_callback_sequence():
    with native_client(raise_server_exceptions=False) as (client, dialer):
        call = accepted_call(client, dialer)
        controller = client.app.state.operator_controller
        assert conference_join(client, call, OWNER, 1).status_code == 204
        assert conference_join(client, call, REMOTE, 2).status_code == 204
        state = controller.native_state(call.id)
        state.owner_menu = True
        state.participants.discard(OWNER)
        call.native_owner_muted = True
        dialer.actual_muted[OWNER_SID] = True
        attempts = []
        original_mute = dialer.mute_participant

        async def fail_once(conference_sid, call_sid, muted):
            if call_sid == OWNER_SID and not muted:
                attempts.append((call_sid, muted))
                if len(attempts) == 1:
                    raise RuntimeError("Transient owner unmute failure")
            await original_mute(conference_sid, call_sid, muted)

        dialer.mute_participant = fail_once
        assert conference_join(client, call, OWNER, 3).status_code == 500
        assert call.native_owner_muted and dialer.actual_muted[OWNER_SID]
        assert conference_join(client, call, OWNER, 3).status_code == 204
        assert attempts == [(OWNER_SID, False), (OWNER_SID, False)]
        assert not call.native_owner_muted and not dialer.actual_muted[OWNER_SID]
        # A confirmed successful event is deduplicated normally.
        assert conference_join(client, call, OWNER, 3).status_code == 204
        assert len(attempts) == 2


def test_passive_observer_restarts_once_with_new_token_without_replacing_human_call(tmp_path):
    async def run():
        h = NativeHarness(tmp_path)
        call = await h.joined(observers=False)
        restarted = []
        recovered_stream = "MZ" + "9" * 32

        async def restart_observer(call_sid, session_id, role, generation, token):
            restarted.append((call_sid, session_id, role, generation, token))
            return recovered_stream

        # Only this fixture exposes the optional recovery operation. Other
        # fake dialers keep the baseline passive-disconnect behavior.
        h.dialer.restart_observer = restart_observer
        old_token = call.legs[REMOTE].token
        first = ReadSocket([start_event(call, REMOTE),
                            media_event(REMOTE_STREAM, "inbound", 1)])
        await h.router.serve_observer(first, REMOTE, h.store)
        await until(lambda: len(restarted) == 1)
        await h.store.wait_idle()
        leg = call.legs[REMOTE]
        assert leg.generation == 2 and leg.token != old_token
        assert restarted == [(REMOTE_SID, call.id, REMOTE, 2, leg.token)]
        assert REMOTE in h.state.observer_restarts
        assert call.active and call.canonical_call_sid == REMOTE_SID
        assert not h.dialer.created and not h.dialer.ended and not h.dialer.ended_conferences
        recovered_start = start_event(call, REMOTE)
        recovered_start["streamSid"] = recovered_stream
        recovered_start["start"]["streamSid"] = recovered_stream
        second = ReadSocket([recovered_start,
                             media_event(recovered_stream, "inbound", 2)])
        await h.router.serve_observer(second, REMOTE, h.store)
        await h.store.wait_idle()
        assert len(restarted) == 1 and leg.generation == 2
        assert [args[3][0] for args in h.observed] == [1, 2]
        assert h.observed[1][4] >= h.observed[0][4] + 20
        assert first.sent == second.sent == []
        assert call.active and h.ends == []
        assert not h.dialer.created and not h.dialer.ended and not h.dialer.ended_conferences
        await h.close()

    asyncio.run(run())
