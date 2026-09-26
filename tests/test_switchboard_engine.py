"""Network-free lifecycle and SDK contract checks for Build 1."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from twilio.base.exceptions import TwilioRestException

from switchboard import SessionRejected, Switchboard
from switchboard.gateway import TwilioGateway

PARENT = "CA" + "1" * 32
OTHER = "CA" + "2" * 32
OUTBOUND = "CA" + "3" * 32
CONFERENCE = "CF" + "4" * 32


def settings(**overrides):
    values = dict(account_sid="AC" + "a" * 32, auth_token="unit-test-token",
                  api_key="", api_secret="", twilio_number="+12025550101",
                  callee_number="+12025550102", public_base_url="https://operator.example",
                  switchboard_setup_timeout=45)
    return SimpleNamespace(**(values | overrides))


def event(kind, *, sid=PARENT, label="caller", sequence="0", **extra):
    return dict(FriendlyName=f"operator-{PARENT}", ConferenceSid=CONFERENCE,
                StatusCallbackEvent=kind, CallSid=sid, ParticipantLabel=label,
                SequenceNumber=sequence) | extra


class Gateway:
    def __init__(self):
        self.created = []
        self.ended_calls = []
        self.ended_conferences = []
        self.lookups = []
        self.gate = None
        self.failure = False
        self.found = OUTBOUND

    async def create_participant(self, conference_sid, parent_sid):
        self.created.append((conference_sid, parent_sid))
        if self.gate:
            await self.gate.wait()
        if self.failure:
            raise TimeoutError("fixture timeout")
        return OUTBOUND

    async def find_participant(self, conference_sid, label="callee"):
        self.lookups.append((conference_sid, label))
        return self.found

    async def end_call(self, sid):
        self.ended_calls.append(sid)

    async def end_conference(self, sid):
        self.ended_conferences.append(sid)


def test_sdk_create_uses_fixed_destination_and_bounded_conference_options():
    async def run():
        gateway = TwilioGateway(settings())
        client = Mock()
        client.conferences.return_value.participants.create.return_value.call_sid = OUTBOUND
        gateway._client = lambda: client
        assert await gateway.create_participant(CONFERENCE, PARENT) == OUTBOUND
        client.conferences.assert_called_once_with(CONFERENCE)
        client.conferences.return_value.participants.create.assert_called_once_with(
            from_="+12025550101", to="+12025550102", label="callee", timeout=25,
            max_participants=2, beep="false", start_conference_on_enter=True,
            end_conference_on_exit=True, record=False, conference_record="do-not-record",
            status_callback=f"https://operator.example/calls/status/{PARENT}",
            status_callback_method="POST",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
        )

    asyncio.run(run())


def test_sdk_client_is_lazy_and_uses_optional_api_credentials(monkeypatch):
    client = Mock()
    monkeypatch.setattr("switchboard.gateway.Client", client)
    gateway = TwilioGateway(settings(api_key="SK" + "b" * 32, api_secret="secret"))
    client.assert_not_called()
    gateway._client()
    args, kwargs = client.call_args
    assert args == ("SK" + "b" * 32, "secret")
    assert kwargs["account_sid"] == "AC" + "a" * 32
    assert kwargs["http_client"].timeout == 10
    assert kwargs["http_client"].session.get_adapter("https://").max_retries.total == 0


@pytest.mark.parametrize("status,update", [("queued", "canceled"), ("ringing", "canceled"),
                                          ("in-progress", "completed"), ("completed", None)])
def test_sdk_ends_calls_in_their_current_state(status, update):
    async def run():
        gateway = TwilioGateway(settings())
        client = Mock()
        call = client.calls.return_value
        call.fetch.return_value.status = status
        gateway._client = lambda: client
        await gateway.end_call(OUTBOUND)
        if update:
            call.update.assert_called_once_with(status=update)
        else:
            call.update.assert_not_called()

    asyncio.run(run())


def test_sdk_hangup_covers_answer_between_fetch_and_cancel():
    async def run():
        gateway = TwilioGateway(settings())
        client = Mock()
        call = client.calls.return_value
        call.fetch.return_value.status = "ringing"
        call.update.side_effect = [TwilioRestException(400, "/fixture", code=21220), None]
        gateway._client = lambda: client
        await gateway.end_call(OUTBOUND)
        assert [c.kwargs for c in call.update.call_args_list] == [
            {"status": "canceled"}, {"status": "completed"}]

    asyncio.run(run())


def test_reordered_join_and_start_accumulate_without_dial_duplication():
    async def run():
        gateway = Gateway()
        board = Switchboard(settings(), gateway)
        s = await board.start(PARENT)
        await board.conference_event(PARENT, event("conference-start", sid="", label="", sequence="3"))
        await board.conference_event(PARENT, event("participant-join", sequence="0"))
        await board.conference_event(PARENT, event("participant-join", sequence="0"))
        await board.wait_idle()
        assert gateway.created == [(CONFERENCE, PARENT)]
        assert not s.connected
        await board.conference_event(PARENT, event("participant-join", sid=OUTBOUND,
                                                 label="callee", sequence="2"))
        assert s.phase == "connected"
        assert s.connected
        assert s.last_event_sequence == 3
        await board.close()

    asyncio.run(run())


def test_early_status_without_endpoint_evidence_waits_for_create_sid():
    async def run():
        gateway = Gateway()
        gateway.gate = asyncio.Event()
        board = Switchboard(settings(), gateway)
        s = await board.start(PARENT)
        await board.conference_event(PARENT, event("participant-join"))
        await asyncio.sleep(0)
        await board.call_status(PARENT, {"CallSid": OTHER, "CallStatus": "failed"})
        await board.call_status(PARENT, {"CallSid": OUTBOUND, "CallStatus": "busy"})
        assert not s.outbound_sid
        assert s.phase == "dialing"
        gateway.gate.set()
        await board.wait_idle()
        assert s.outbound_sid == OUTBOUND
        assert s.phase == "ended"
        assert s.reason == "busy"
        assert OTHER not in gateway.ended_calls
        await board.close()

    asyncio.run(run())


def test_early_status_with_matching_endpoints_binds_and_ends_safely():
    async def run():
        gateway = Gateway()
        gateway.gate = asyncio.Event()
        board = Switchboard(settings(), gateway)
        s = await board.start(PARENT)
        await board.conference_event(PARENT, event("participant-join"))
        await asyncio.sleep(0)
        await board.call_status(PARENT, dict(CallSid=OUTBOUND, CallStatus="no-answer",
            From="+12025550101", To="+12025550102", Direction="outbound-api"))
        assert s.outbound_sid == OUTBOUND and s.phase == "ended"
        gateway.gate.set()
        await board.wait_idle()
        assert OUTBOUND in gateway.ended_calls
        await board.close()

    asyncio.run(run())


def test_caller_abandonment_cleans_eventual_create_result():
    async def run():
        gateway = Gateway()
        gateway.gate = asyncio.Event()
        board = Switchboard(settings(), gateway)
        s = await board.start(PARENT)
        await board.conference_event(PARENT, event("participant-join"))
        await asyncio.sleep(0)
        await board.conference_event(PARENT, event("participant-leave", sequence="1"))
        assert s.phase == "ended"
        gateway.gate.set()
        await board.wait_idle()
        assert OUTBOUND in gateway.ended_calls
        assert CONFERENCE in gateway.ended_conferences
        assert len(gateway.created) == 1
        assert await board.start(PARENT) is s
        assert s.phase == "ended"
        await board.close()

    asyncio.run(run())


def test_uncertain_create_reconciles_once_then_ends_without_redial():
    async def run():
        gateway = Gateway()
        gateway.failure = True
        board = Switchboard(settings(), gateway)
        s = await board.start(PARENT)
        await board.conference_event(PARENT, event("participant-join"))
        await board.wait_idle()
        assert s.phase == "ended" and s.reason == "dial_failed"
        assert gateway.lookups == [(CONFERENCE, "callee")]
        assert OUTBOUND in gateway.ended_calls
        await board.conference_event(PARENT, event("participant-join", sequence="0"))
        await board.wait_idle()
        assert len(gateway.created) == 1
        await board.close()

    asyncio.run(run())


def test_late_join_after_uncertain_create_cleanup_does_not_revive_session():
    async def run():
        gateway = Gateway()
        gateway.failure = True
        gateway.found = None
        board = Switchboard(settings(), gateway)
        s = await board.start(PARENT)
        await board.conference_event(PARENT, event("participant-join"))
        await board.wait_idle()
        assert s.phase == "ended" and not s.outbound_sid
        await board.conference_event(PARENT, event("participant-join", sid=OUTBOUND,
                                                 label="callee", sequence="4"))
        await board.wait_idle()
        assert s.phase == "ended" and not s.connected
        assert OUTBOUND in gateway.ended_calls
        await board.close()

    asyncio.run(run())


def test_setup_timeout_releases_caller_even_without_conference_callbacks():
    async def run():
        gateway = Gateway()
        board = Switchboard(settings(switchboard_setup_timeout=0.01), gateway)
        s = await board.start(PARENT)
        await asyncio.sleep(0.03)
        await board.wait_idle()
        assert s.phase == "ended" and s.reason == "setup_timeout"
        assert gateway.ended_calls == [PARENT]
        assert not gateway.created
        await board.close()

    asyncio.run(run())


def test_capacity_preserves_recent_tombstones_then_expires_them():
    async def run():
        gateway = Gateway()
        board = Switchboard(settings(), gateway)
        board.MAX_SESSIONS = 1
        s = await board.start(PARENT)
        await board.finished(PARENT)
        await board.wait_idle()
        with pytest.raises(SessionRejected, match="capacity"):
            await board.start(OTHER)
        s.ended_at -= board.TOMBSTONE_SECONDS + 1
        await board.start(OTHER)
        assert PARENT not in board.sessions
        await board.close()

    asyncio.run(run())


@pytest.mark.parametrize("number", ["+12025550101", "+12025550102"])
def test_self_calls_do_not_create_sessions(number):
    async def run():
        board = Switchboard(settings(), Gateway())
        with pytest.raises(SessionRejected, match="self_call"):
            await board.start(PARENT, number)
        assert not board.sessions
        await board.close()

    asyncio.run(run())


def test_cleanup_retries_transient_failure_once_without_retrying_dial():
    async def run():
        gateway = Gateway()
        attempts = []

        async def end_call(sid):
            attempts.append(sid)
            if len(attempts) == 1:
                raise TimeoutError("temporary fixture failure")

        gateway.end_call = end_call
        board = Switchboard(settings(), gateway)
        await board.start(PARENT)
        await board.finished(PARENT)
        await board.wait_idle()
        assert attempts == [PARENT, PARENT]
        assert not gateway.created
        await board.close()

    asyncio.run(run())


def test_deployment_drain_preserves_existing_session_and_rejects_new_calls():
    async def run():
        board = Switchboard(settings(), Gateway())
        existing = await board.start(PARENT)
        await board.set_draining(True)
        assert board.draining
        assert await board.start(PARENT) is existing
        with pytest.raises(SessionRejected, match="draining"):
            await board.start(OTHER)
        await board.set_draining(False)
        await board.start(OTHER)
        await board.close()
        assert board.draining
        assert board.active_count == 0
        assert board.pending_count == 0

    asyncio.run(run())
