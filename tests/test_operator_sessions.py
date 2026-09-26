"""Network-free reservations, stream binding, and deadlines for the operator.

Nothing here dials, opens a socket, or talks to a provider: the store decides
whether a fact may be bound, and these tests pin every refusal.
"""

import asyncio
from dataclasses import replace
import json
import uuid

import pytest

from config import Settings
from operator_service import OperatorRejected, OperatorSessions
from operator_service.sessions import (AGENT, CONNECTED, ENDED, HUMAN, OWNER, OWNER_PROMPT,
                                       REMOTE, TOKEN_SECONDS)

ACCOUNT = "AC" + "a" * 32
OWNER_NUMBER = "+12025550101"
CALLEE = "+12025550102"
DESTINATION = "+12025550103"
OTHER_DESTINATION = "+12025550104"
OWNER_SID = "CA" + "1" * 32
REMOTE_SID = "CA" + "2" * 32
OWNER_STREAM = "MZ" + "3" * 32
REMOTE_STREAM = "MZ" + "4" * 32
SETTINGS = Settings(
    account_sid=ACCOUNT, auth_token="operator-test-auth-token",
    public_base_url="https://operator.example", twilio_number=CALLEE,
    owner_number=OWNER_NUMBER, allowed_destinations=(DESTINATION, OTHER_DESTINATION),
    operator_admin_token="operator-admin-token-32-characters-long",
    max_call_seconds=1800,
)


def key():
    return str(uuid.uuid4())


class Harness:
    """A store whose deadlines end their session and record the reason."""

    def __init__(self, settings=None, **kwargs):
        self.store = OperatorSessions(settings or SETTINGS, **kwargs)
        self.reasons = []

        async def on_timeout(session, reason):
            self.reasons.append(reason)
            await self.store.end(session.id, reason)

        self.store.on_timeout = on_timeout


async def reserved(store, to=DESTINATION, goal="Ask for an itemized out-the-door quote"):
    session, reused = await store.reserve_outbound(to, goal, key())
    assert not reused
    return session


def start_message(session, role, **changes):
    leg = session.legs[role]
    message = {"accountSid": ACCOUNT,
               "callSid": OWNER_SID if role == OWNER else REMOTE_SID,
               "streamSid": OWNER_STREAM if role == OWNER else REMOTE_STREAM,
               "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
               "customParameters": {"generation": str(leg.generation), "token": leg.token}}
    message.update(changes)
    return message


def test_reserve_checks_key_goal_and_destination():
    async def run():
        store = Harness().store
        with pytest.raises(OperatorRejected) as missing:
            await store.reserve_outbound(DESTINATION, "goal", "")
        assert missing.value.reason == "invalid-idempotency-key"
        with pytest.raises(OperatorRejected) as not_uuid:
            await store.reserve_outbound(DESTINATION, "goal", "retry-1")
        assert not_uuid.value.reason == "invalid-idempotency-key"
        with pytest.raises(OperatorRejected) as long_goal:
            await store.reserve_outbound(DESTINATION, "x" * 301, key())
        assert long_goal.value.reason == "goal-too-long"
        for destination in ("+12025559999", OWNER_NUMBER, CALLEE, "12025550103"):
            with pytest.raises(OperatorRejected) as not_allowed:
                await store.reserve_outbound(destination, "goal", key())
            assert not_allowed.value.reason == "destination-not-allowed"
        assert store.sessions == {}

    asyncio.run(run())


def test_reserve_is_idempotent_per_key_and_caps_active_sessions():
    async def run():
        harness = Harness()
        store = harness.store
        first = key()
        session, reused = await store.reserve_outbound(DESTINATION, "goal", first)
        again, reused_again = await store.reserve_outbound(DESTINATION, "goal", first)
        assert (again.id, reused_again) == (session.id, True)
        assert store.active_count == 1
        # The first demo is one active session at a time.
        with pytest.raises(OperatorRejected) as capacity:
            await store.reserve_outbound(DESTINATION, "goal", key())
        assert capacity.value.reason == "capacity"
        await store.set_draining(True)
        with pytest.raises(OperatorRejected) as draining:
            await store.reserve_outbound(DESTINATION, "goal", key())
        assert draining.value.reason == "draining"
        await store.set_draining(False)
        await store.end(session.id, "admin-end")
        await store.set_draining(True)
        with pytest.raises(OperatorRejected) as again_draining:
            await store.reserve_outbound(DESTINATION, "goal", key())
        assert again_draining.value.reason == "draining"
        await store.set_draining(False)
        second = await reserved(store)
        assert second.id != session.id
        assert store.active_count == 1

    asyncio.run(run())


def test_both_legs_are_reserved_with_distinct_one_use_tokens():
    async def run():
        store = Harness().store
        session = await reserved(store)
        owner, remote = session.legs[OWNER], session.legs[REMOTE]
        assert session.phase == "reserved" and session.mode == HUMAN
        assert (owner.destination, remote.destination) == (OWNER_NUMBER, DESTINATION)
        assert owner.token != remote.token and len(owner.token) >= 32
        assert owner.generation == remote.generation == 1
        assert owner.usable() and remote.usable()
        assert session.deadline is not None

    asyncio.run(run())


def test_status_snapshot_hides_tokens_and_full_numbers():
    async def run():
        store = Harness().store
        session = await reserved(store)
        await store.bind_call_sid(session.id, OWNER, OWNER_SID)
        await store.bind_stream(session.id, OWNER, start_message(session, OWNER))
        await store.add_turn(session.id, "owner", "I need a quote for a Corolla.")
        snapshot = json.dumps(session.to_status())
        for secret in (session.legs[OWNER].token, session.legs[REMOTE].token,
                       OWNER_NUMBER, DESTINATION, SETTINGS.operator_admin_token, OWNER_SID):
            assert secret not in snapshot
        status = session.to_status()
        assert status["legs"][OWNER]["call_bound"] is True
        assert status["legs"][OWNER]["stream_bound"] is True
        assert status["turns"][0]["speaker"] == "owner"
        assert status["to"].endswith(DESTINATION[-4:])

    asyncio.run(run())


def test_a_signed_start_can_bind_before_the_rest_result_returns():
    async def run():
        store = Harness().store
        session = await reserved(store)
        leg = await store.bind_stream(session.id, OWNER, start_message(session, OWNER))
        assert (leg.call_sid, leg.state, leg.attached) == (OWNER_SID, "started", True)
        # The eventual REST result must agree with the SID that already arrived.
        assert await store.bind_call_sid(session.id, OWNER, OWNER_SID) == "matched"
        with pytest.raises(OperatorRejected) as mismatch:
            await store.bind_call_sid(session.id, OWNER, REMOTE_SID)
        assert mismatch.value.reason == "call-sid-mismatch"

    asyncio.run(run())


def test_the_rest_result_can_bind_before_the_signed_start():
    async def run():
        store = Harness().store
        session = await reserved(store)
        assert await store.bind_call_sid(session.id, OWNER, OWNER_SID) == "bound"
        assert session.legs[OWNER].state == "dialing"
        await store.bind_stream(session.id, OWNER, start_message(session, OWNER))
        await store.bind_call_sid(session.id, REMOTE, REMOTE_SID)
        with pytest.raises(OperatorRejected) as mismatch:
            await store.bind_stream(session.id, REMOTE, start_message(session, REMOTE, callSid=OWNER_SID))
        assert mismatch.value.reason == "call-sid-mismatch"

    asyncio.run(run())


@pytest.mark.parametrize("changes,reason", [
    ({"accountSid": "AC" + "b" * 32}, "account-mismatch"),
    ({"callSid": "not-a-call"}, "invalid-call-sid"),
    ({"streamSid": "XX" + "3" * 32}, "invalid-stream-sid"),
    ({"mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 16000, "channels": 1}},
     "unsupported-media-format"),
    ({"mediaFormat": {"encoding": "audio/pcm", "sampleRate": 8000, "channels": 1}},
     "unsupported-media-format"),
    ({"mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 2}},
     "unsupported-media-format"),
    ({"customParameters": {"generation": "9", "token": "stale"}}, "token-mismatch"),
])
def test_stream_binding_rejects_untrusted_starts(changes, reason):
    async def run():
        store = Harness().store
        session = await reserved(store)
        with pytest.raises(OperatorRejected) as rejected:
            await store.bind_stream(session.id, OWNER, start_message(session, OWNER, **changes))
        assert rejected.value.reason == reason
        assert session.legs[OWNER].attached is False

    asyncio.run(run())


def test_stream_binding_rejects_stale_generations_duplicates_and_stale_tokens():
    async def run():
        store = Harness().store
        session = await reserved(store)
        leg = session.legs[OWNER]
        with pytest.raises(OperatorRejected) as stale:
            await store.bind_stream(session.id, OWNER, start_message(
                session, OWNER, customParameters={"generation": "2", "token": leg.token}))
        assert stale.value.reason == "stale-generation"
        leg.token_issued -= TOKEN_SECONDS + 1
        with pytest.raises(OperatorRejected) as expired:
            await store.bind_stream(session.id, OWNER, start_message(session, OWNER))
        assert expired.value.reason == "expired-token"
        leg.token_issued += TOKEN_SECONDS + 1
        await store.bind_stream(session.id, OWNER, start_message(session, OWNER))
        with pytest.raises(OperatorRejected) as duplicate:
            await store.bind_stream(session.id, OWNER, start_message(session, OWNER))
        assert duplicate.value.reason == "duplicate-binding"
        assert await store.detach_socket(session.id, OWNER, leg.generation) is True
        assert await store.detach_socket(session.id, OWNER, leg.generation) is False

    asyncio.run(run())


def test_reconnect_rotates_a_generation_twice_within_ten_seconds():
    async def run():
        store = Harness().store
        session = await reserved(store)
        await store.bind_stream(session.id, REMOTE, start_message(session, REMOTE))
        original = session.legs[REMOTE].token
        leg = await store.rotate_token(session.id, REMOTE)
        assert leg.generation == 2 and leg.token != original and not leg.attached
        assert leg.state == "reconnecting"
        await store.bind_stream(session.id, REMOTE, start_message(session, REMOTE))
        await store.rotate_token(session.id, REMOTE)
        with pytest.raises(OperatorRejected) as limited:
            await store.rotate_token(session.id, REMOTE)
        assert limited.value.reason == "reconnect-limit"
        assert session.legs[REMOTE].generation == 3

    asyncio.run(run())


def test_call_status_callbacks_bind_ignore_and_end():
    async def run():
        store = Harness().store
        session = await reserved(store)
        with pytest.raises(OperatorRejected) as unknown:
            await store.record_status("0" * 32, OWNER, OWNER_SID, "ringing")
        assert unknown.value.reason == "unknown-session"
        with pytest.raises(OperatorRejected) as bad_role:
            await store.record_status(session.id, "caller", OWNER_SID, "ringing")
        assert bad_role.value.reason == "unknown-role"
        with pytest.raises(OperatorRejected) as bad_status:
            await store.record_status(session.id, OWNER, OWNER_SID, "answered")
        assert bad_status.value.reason == "invalid-status"
        await store.record_status(session.id, OWNER, OWNER_SID, "ringing")
        assert session.legs[OWNER].call_sid == OWNER_SID
        assert (await store.record_status(session.id, OWNER, OWNER_SID, "in-progress"))["action"] == "accepted"
        out_of_order = await store.record_status(session.id, OWNER, OWNER_SID, "ringing")
        assert out_of_order == {"action": "ignored", "reason": "out-of-order"}
        with pytest.raises(OperatorRejected) as mismatch:
            await store.record_status(session.id, OWNER, REMOTE_SID, "completed")
        assert mismatch.value.reason == "call-sid-mismatch"
        ended = await store.record_status(session.id, OWNER, OWNER_SID, "completed", duration=61)
        assert ended == {"action": "terminal", "reason": "completed", "duration_seconds": 61}
        assert session.legs[OWNER].ended and session.legs[OWNER].state == "ended"
        assert (await store.record_status(session.id, OWNER, OWNER_SID, "completed"))["reason"] == "leg-ended"
        await store.end(session.id, "owner-completed")
        closed = await store.record_status(session.id, REMOTE, REMOTE_SID, "completed")
        assert closed == {"action": "ignored", "reason": "owner-completed"}

    asyncio.run(run())


def test_an_uncertain_dial_reconciles_from_a_callback_before_ending():
    async def run():
        harness = Harness(deadlines={"reconcile": 0.05})
        store = harness.store
        session = await reserved(store)
        assert await store.dial_failed(session.id, OWNER) is True
        assert session.legs[OWNER].uncertain is True
        # A signed callback that binds the leg proves the dial did happen.
        await store.record_status(session.id, OWNER, OWNER_SID, "ringing")
        assert session.legs[OWNER].uncertain is False
        await asyncio.sleep(0.15)
        assert harness.reasons == [] and session.active
        # With no callback at all the session ends instead of dialing twice.
        assert await store.dial_failed(session.id, REMOTE) is True
        await asyncio.sleep(0.15)
        assert harness.reasons == ["remote-dial-failed"]
        assert session.phase == ENDED

    asyncio.run(run())


def test_phase_deadlines_expire_only_their_own_phase():
    async def run():
        harness = Harness(deadlines={"owner_accept": 0.05, "owner_ring": 0.05})
        store = harness.store
        session = await reserved(store)
        assert await store.begin_owner_dial(session.id) is True
        assert await store.begin_owner_dial(session.id) is False
        await store.mark_owner_prompt(session.id)
        assert session.phase == OWNER_PROMPT
        await asyncio.sleep(0.15)
        assert harness.reasons == ["owner-no-acceptance"]
        assert session.phase == ENDED

    asyncio.run(run())


def test_acceptance_stops_the_owner_deadline_and_connect_stops_setup():
    async def run():
        harness = Harness(deadlines={"owner_accept": 0.2, "setup": 0.2, "call": 0.2})
        store = harness.store
        session = await reserved(store)
        await store.begin_owner_dial(session.id)
        await store.mark_owner_prompt(session.id)
        assert await store.begin_remote_dial(session.id) is True
        assert await store.begin_remote_dial(session.id) is False
        assert session.phase == "remote_setup"
        assert await store.mark_connected(session.id) is True
        assert session.phase == CONNECTED
        await asyncio.sleep(0.3)
        # Only the call deadline survives connecting, and it now ends the session.
        assert harness.reasons == ["call-time-limit"]
        assert session.phase == ENDED

    asyncio.run(run())


def test_end_is_idempotent_and_returns_unfinished_legs_only():
    async def run():
        store = Harness().store
        session = await reserved(store)
        await store.bind_call_sid(session.id, OWNER, OWNER_SID)
        await store.bind_call_sid(session.id, REMOTE, REMOTE_SID)
        assert await store.end(session.id, "admin-end") == [(OWNER, OWNER_SID), (REMOTE, REMOTE_SID)]
        assert await store.end(session.id, "admin-end") == []
        assert session.phase == ENDED and session.ended_reason == "admin-end"
        assert store.active_count == 0
        for coro, reason in ((store.bind_stream(session.id, OWNER, start_message(session, OWNER)),
                              "session-ended"),
                             (store.set_mode(session.id, AGENT), "session-ended"),
                             (store.rotate_token(session.id, OWNER), "reconnect-unavailable")):
            with pytest.raises(OperatorRejected) as rejected:
                await coro
            assert rejected.value.reason == reason

    asyncio.run(run())


def test_mode_switches_once_and_increments_the_reply_epoch():
    async def run():
        store = Harness().store
        session = await reserved(store)
        with pytest.raises(OperatorRejected) as invalid:
            await store.set_mode(session.id, "1")
        assert invalid.value.reason == "invalid-mode"
        assert await store.set_mode(session.id, AGENT) is True
        assert (session.mode, session.reply_epoch) == (AGENT, 1)
        assert await store.set_mode(session.id, AGENT) is False
        assert session.reply_epoch == 1
        assert await store.set_mode(session.id, HUMAN) is True
        assert session.reply_epoch == 2

    asyncio.run(run())


def test_selecting_a_profile_delegates_once_and_keeps_the_history():
    async def run():
        store = Harness().store
        session = await reserved(store)
        await store.add_turn(session.id, "owner", "I need a quote for a RAV4.")
        with pytest.raises(OperatorRejected) as invalid:
            await store.select_profile(session.id, "")
        assert invalid.value.reason == "invalid-profile"
        # The first shortcut delegates and keeps the conversation so far.
        assert await store.select_profile(session.id, "1") is True
        assert (session.profile, session.mode, session.reply_epoch) == ("1", AGENT, 1)
        assert [turn["text"] for turn in session.turns] == ["I need a quote for a RAV4."]
        # An already-active profile is a no-op: nothing restarts, nothing changes.
        assert await store.select_profile(session.id, "1") is False
        assert session.reply_epoch == 1
        # A different profile cancels the previous reply exactly once, even
        # though the session stays in agent mode.
        assert await store.select_profile(session.id, "3") is True
        assert (session.profile, session.mode, session.reply_epoch) == ("3", AGENT, 2)
        # Returning to human bumps the epoch again, so stale audio stays dead.
        assert await store.set_mode(session.id, HUMAN) is True
        assert (session.profile, session.reply_epoch) == ("3", 3)
        assert await store.select_profile(session.id, "3") is True
        assert (session.mode, session.reply_epoch) == (AGENT, 4)

    asyncio.run(run())


def test_an_ended_session_refuses_a_profile_and_announces_its_end():
    async def run():
        harness = Harness()
        store = harness.store
        ended = []

        async def on_end(session_id):
            ended.append(session_id)

        store.on_end = on_end
        session = await reserved(store)
        await store.end(session.id, "remote-completed")
        assert ended == [session.id]
        # The observer runs once, and only for a session that really ended.
        await store.end(session.id, "remote-completed")
        assert ended == [session.id]
        with pytest.raises(OperatorRejected) as rejected:
            await store.select_profile(session.id, "2")
        assert rejected.value.reason == "session-ended"

    asyncio.run(run())


def test_a_failing_end_observer_is_reported_not_raised():
    async def run():
        store = Harness().store
        session = await reserved(store)

        async def on_end(session_id):
            raise RuntimeError("observer failed")

        store.on_end = on_end
        assert await store.end(session.id, "admin-end") == []    # no legs were bound
        assert session.phase == ENDED and store.active_count == 0

    asyncio.run(run())


def test_turns_and_marks_stay_bounded_and_attributed():
    async def run():
        store = Harness().store
        session = await reserved(store)
        with pytest.raises(OperatorRejected) as speaker:
            await store.add_turn(session.id, "caller", "hello")
        assert speaker.value.reason == "unknown-speaker"
        for index in range(60):
            await store.add_turn(session.id, "remote", str(index))
        assert len(session.turns) == 60
        turn = await store.add_turn(session.id, "agent", "x" * 900)
        assert len(turn["text"]) == 500
        assert len(session.to_status()["turns"]) == 40
        assert store.note_mark(session.id, OWNER, "reply-1", "pending") is True
        assert store.mark_state(session.id, OWNER, "reply-1") == "pending"
        assert store.note_mark(session.id, "caller", "reply-1", "pending") is False
        for index in range(80):
            store.note_mark(session.id, OWNER, f"reply-{index}", "played")
        assert len(session.legs[OWNER].marks) == 64
        assert "reply-0" not in session.legs[OWNER].marks
        assert store.mark_state(session.id, OWNER, "missing") == ""

    asyncio.run(run())


def test_close_ends_sessions_and_waits_for_tracked_work():
    async def run():
        store = Harness().store
        session = await reserved(store)
        finished = []

        async def work():
            await asyncio.sleep(0.02)
            finished.append(session.id)

        store.spawn(work())
        assert store.pending_count == 1
        await store.close()
        assert finished == [session.id]
        assert store.closed and store.draining and store.pending_count == 0
        assert session.phase == ENDED and session.ended_reason == "server-shutdown"
        with pytest.raises(OperatorRejected) as closed:
            await store.reserve_outbound(DESTINATION, "goal", key())
        assert closed.value.reason == "shutting-down"

    asyncio.run(run())


def test_readiness_requires_owner_number_admin_token_and_allowlist():
    async def run():
        ready = OperatorSessions(SETTINGS)
        assert ready.ready is True
        assert ready.allowed == frozenset({DESTINATION, OTHER_DESTINATION})
        for change in ({"owner_number": ""}, {"operator_admin_token": ""},
                       {"allowed_destinations": ()}, {"twilio_number": ""}):
            store = OperatorSessions(replace(SETTINGS, **change))
            assert store.ready is False
            with pytest.raises(OperatorRejected) as rejected:
                await store.reserve_outbound(DESTINATION, "goal", key())
            assert rejected.value.reason == "not-configured"

    asyncio.run(run())
