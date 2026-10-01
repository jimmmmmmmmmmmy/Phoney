"""Focused product and boundary checks; test helpers live in support."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from operator_service.native_conference import NativeConferenceRouter
from operator_service.conference_gateway import NativeConferenceGatewayMixin, native_leg_twiml
from operator_service.sessions import AGENT, ANNOUNCING, CONNECTED, HUMAN, OWNER, PREPARING, REMOTE

from support.native_conference import BOT_SID, NATIVE_SETTINGS, NativeHarness, ReadSocket, media_event, start_event
from support.operator_keypad import OWNER_SID, REMOTE_STREAM, until


def test_native_takeover_mutes_owner_before_bot_and_release_reverses_safely(tmp_path):
    async def run():
        h = NativeHarness(tmp_path)
        call = await h.joined()
        assert NATIVE_SETTINGS.native_conference_jitter_buffer == "medium"
        for jitter in ("medium", "small", "large", "off"):
            settings = replace(NATIVE_SETTINGS, native_conference_jitter_buffer=jitter)
            for role in (OWNER, REMOTE):
                for rejoin in (False, True):
                    conference = ElementTree.fromstring(native_leg_twiml(
                        settings, call, role, rejoin=rejoin)).find("./Dial/Conference")
                    assert conference.attrib["jitterBufferSize"] == jitter
            options = []
            gateway = NativeConferenceGatewayMixin()
            gateway.settings = settings
            gateway._client = lambda: SimpleNamespace(conferences=lambda _: SimpleNamespace(
                participants=SimpleNamespace(create=lambda **kwargs:
                    options.append(kwargs) or SimpleNamespace(call_sid=BOT_SID))))
            assert await gateway.create_agent_participant(h.state.sid, call.id, 1, "offline-token") == BOT_SID
            assert options[0]["jitter_buffer_size"] == jitter
        for invalid in ("tiny", "", None, []):
            with pytest.raises(ValueError, match="NATIVE_CONFERENCE_JITTER_BUFFER"):
                replace(NATIVE_SETTINGS, native_conference_jitter_buffer=invalid)
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
