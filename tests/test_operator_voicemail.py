"""Remote-only voicemail races and timed conversation flow without phone calls."""
import asyncio
from types import SimpleNamespace

import pytest

from operator_service.sessions import CONNECTED, OWNER, REMOTE, OperatorRejected, OperatorSessions
from operator_service.voicemail_agent import VoicemailAgent
from test_operator_sessions import SETTINGS, OWNER_SID, REMOTE_SID, DESTINATION, start_message


def settings(**changes):
    return SimpleNamespace(**(vars(SETTINGS) | {
        "operator_inbound_enabled": True, "voicemail_agent_enabled": True,
        "voicemail_agent_ring_seconds": 15,
    } | changes))


async def incoming(store):
    session, _ = await store.reserve_inbound(REMOTE_SID, DESTINATION)
    await store.begin_owner_dial(session.id)
    await store.bind_stream(session.id, REMOTE, start_message(session, REMOTE))
    return session


def test_voicemail_claim_retains_caller_and_retires_owner_once():
    async def run():
        store = OperatorSessions(settings())
        session = await incoming(store)
        await store.bind_call_sid(session.id, OWNER, OWNER_SID)
        token = session.legs[REMOTE].token
        assert await store.claim_voicemail(session.id)
        assert not await store.claim_voicemail(session.id)
        assert session.voicemail and session.phase == CONNECTED
        assert session.voicemail_phase == "greeting" and session.agent_kind == "voicemail"
        assert session.legs[REMOTE].attached and session.legs[REMOTE].token == token
        assert session.legs[OWNER].ended and not session.legs[OWNER].usable()
        assert (session.id, "owner_ring") not in store._timers
        assert (session.id, "setup") not in store._timers
        assert (session.id, "call") in store._timers
        await store.close()
    asyncio.run(run())


@pytest.mark.parametrize("answer", ["status", "stream"])
def test_authenticated_owner_answer_wins_before_voicemail_claim(answer):
    async def run():
        store = OperatorSessions(settings())
        session = await incoming(store)
        if answer == "status":
            await store.record_status(session.id, OWNER, OWNER_SID, "in-progress")
        else:
            await store.bind_stream(session.id, OWNER, start_message(session, OWNER))
        assert not await store.claim_voicemail(session.id)
        assert not session.voicemail and session.active
        await store.close()
    asyncio.run(run())


def test_voicemail_claim_wins_late_dial_callback_and_stream_race():
    async def run():
        store = OperatorSessions(settings())
        session = await incoming(store)
        assert await store.claim_voicemail(session.id)
        with pytest.raises(OperatorRejected) as retired:
            await store.bind_call_sid(session.id, OWNER, OWNER_SID)
        assert retired.value.reason == "voicemail-owner-retired"
        for status in ("ringing", "in-progress", "completed"):
            result = await store.record_status(session.id, OWNER, OWNER_SID, status)
            assert result["action"] == "retired" and result["call_sid"] == OWNER_SID
            assert session.active and session.legs[REMOTE].attached
        with pytest.raises(OperatorRejected):
            await store.bind_stream(session.id, OWNER, start_message(session, OWNER))
        assert session.active
        await store.close()
    asyncio.run(run())


def test_owner_terminal_status_can_claim_voicemail_but_caller_hangup_cannot():
    async def run():
        store = OperatorSessions(settings())
        session = await incoming(store)
        result = await store.record_status(session.id, OWNER, OWNER_SID, "no-answer")
        assert result["action"] == "terminal"
        assert await store.claim_voicemail(session.id, result["reason"])
        await store.close()
        store = OperatorSessions(settings())
        session = await incoming(store)
        await store.record_status(session.id, REMOTE, REMOTE_SID, "completed")
        assert not await store.claim_voicemail(session.id)
        await store.close()
    asyncio.run(run())


def test_voicemail_never_activates_for_outbound_or_disabled_sessions():
    async def run():
        store = OperatorSessions(settings(voicemail_agent_enabled=False))
        session = await incoming(store)
        assert not await store.claim_voicemail(session.id)
        await store.close()
        store = OperatorSessions(settings())
        session, _ = await store.reserve_outbound(DESTINATION, "", "8c1e96c3-7889-4ffd-bff3-48c9b9dc3f02")
        await store.begin_owner_dial(session.id)
        assert not await store.claim_voicemail(session.id)
        await store.close()
    asyncio.run(run())


def test_remote_stream_can_reconnect_in_voicemail_without_rejoining_owner():
    async def run():
        store = OperatorSessions(settings())
        session = await incoming(store)
        assert await store.claim_voicemail(session.id)
        await store.detach_socket(session.id, REMOTE, session.legs[REMOTE].generation)
        await store.rotate_token(session.id, REMOTE)
        await store.bind_stream(session.id, REMOTE, start_message(session, REMOTE))
        assert session.voicemail and session.legs[REMOTE].attached
        with pytest.raises(OperatorRejected):
            await store.rotate_token(session.id, OWNER)
        await store.close()
    asyncio.run(run())


def test_configured_voicemail_ring_deadline_applies_only_to_inbound_calls():
    async def run():
        store = OperatorSessions(settings(voicemail_agent_ring_seconds=.02))
        session = await incoming(store)
        await until(lambda: not session.active)
        assert session.ended_reason == "owner-no-answer"
        await store.close()
        store = OperatorSessions(settings(voicemail_agent_ring_seconds=.02))
        session, _ = await store.reserve_outbound(DESTINATION, "", "4e8769e2-91ed-49b0-981e-96e71a1c5b96")
        await store.begin_owner_dial(session.id)
        await asyncio.sleep(.04)
        assert session.active
        await store.close()
    asyncio.run(run())


async def until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(.001)


def policy(**changes):
    session = SimpleNamespace(active=True, voicemail=True, voicemail_phase="greeting")
    replies, ended = [], []
    vm = VoicemailAgent(session, on_reply=replies.append, on_end=ended.append,
                        **({"pause_seconds": .02, "initial_silence_seconds": .3,
                            "confirmation_silence_seconds": .3, "capture_seconds": .5,
                            "total_seconds": 1} | changes))
    vm.start()
    return vm, replies, ended


def test_message_waits_for_pause_after_latest_interim_before_readback():
    async def run():
        vm, replies, ended = policy()
        await vm.reply_completed("greeting")
        vm.transcript("Please call Alex about the meeting.")
        await asyncio.sleep(.012)
        vm.transcript("Tomorrow", final=False)
        await asyncio.sleep(.012)
        assert replies == []
        vm.transcript("Tomorrow at ten.")
        await until(lambda: replies)
        assert replies == ["readback"] and vm.busy and ended == []
        await vm.reply_completed("readback")
        vm.transcript("Yes, that is right.")
        await until(lambda: len(replies) == 2)
        assert replies == ["readback", "confirm"]
        vm.close()
    asyncio.run(run())


def test_interims_never_invent_a_final_message_and_no_message_waits_for_farewell():
    async def run():
        vm, replies, ended = policy(initial_silence_seconds=.04)
        await vm.reply_completed("greeting")
        vm.transcript("partial", final=False)
        await until(lambda: replies)
        assert replies == ["no_message"] and not vm.has_message
        assert ended == []
        await vm.reply_completed("no_message")
        assert ended == ["voicemail-no-message"] and vm.closed
    asyncio.run(run())


def test_readback_silence_does_not_claim_caller_confirmed_message():
    async def run():
        vm, replies, ended = policy(confirmation_silence_seconds=.03)
        vm.reply_started("readback")
        await vm.reply_completed("readback")
        await until(lambda: replies)
        assert replies == ["unconfirmed"] and ended == []
        await vm.reply_completed("unconfirmed")
        assert ended == ["voicemail-unconfirmed"]
    asyncio.run(run())


def test_new_caller_final_while_reply_generates_is_consumed_only_after_playback():
    async def run():
        vm, replies, ended = policy()
        vm.transcript("Please tell them I called.")
        await asyncio.sleep(.035)
        assert replies == []
        await vm.reply_completed("greeting")
        await until(lambda: replies)
        assert replies == ["readback"] and ended == []
        vm.close()
    asyncio.run(run())


def test_capture_and_total_deadlines_bound_continuous_speech_and_stalled_reply():
    async def run():
        vm, replies, ended = policy(capture_seconds=.035, total_seconds=.10)
        await vm.reply_completed("greeting")
        for _ in range(5):
            vm.transcript("More message details.")
            await asyncio.sleep(.009)
        assert replies == ["readback"]
        # Simulated generation/playback never completes: overall bound remains.
        await until(lambda: ended)
        assert ended == ["voicemail-time-limit"]
    asyncio.run(run())


def test_hangup_or_close_cancels_all_future_replies():
    async def run():
        vm, replies, ended = policy(initial_silence_seconds=.02)
        await vm.reply_completed("greeting")
        vm.transcript("Call me back.")
        vm.close()
        await asyncio.sleep(.04)
        assert replies == ended == []
        assert vm._timers == {}
    asyncio.run(run())


def test_speech_activity_extends_voicemail_pause_without_inventing_text():
    async def run():
        vm, replies, ended = policy()
        await vm.reply_completed('greeting')
        vm.transcript('I am selling a car.')
        await asyncio.sleep(.012)
        vm.transcript('', final=False, activity=True)
        await asyncio.sleep(.012)
        assert replies == []
        vm.transcript('A 2010 Honda Civic.')
        await until(lambda: replies)
        assert replies == ['readback'] and ended == []
        vm.close()
        vm, replies, ended = policy(initial_silence_seconds=.03)
        await vm.reply_completed('greeting')
        vm.transcript('', final=False, activity=True)
        assert not vm.pending_final
        await until(lambda: replies)
        assert replies == ['no_message']
        vm.close()
    asyncio.run(run())
