"""Voicemail is an unanswered-call transition, never a second dial or caller teardown."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from switchboard import Switchboard, SessionRejected
from switchboard.gateway import TwilioGateway

PARENT = "CA" + "1" * 32
OUTBOUND = "CA" + "2" * 32
OTHER = "CA" + "3" * 32
CONFERENCE = "CF" + "4" * 32


def settings(**changes):
    return SimpleNamespace(**(dict(account_sid="AC"+"a"*32,auth_token="fixture",api_key="",api_secret="",
        twilio_number="+12025550101",callee_number="+12025550102",public_base_url="https://operator.example",
        switchboard_setup_timeout=45,voicemail_enabled=True,voicemail_max_seconds=120)|changes))


def event(kind,sid=PARENT,label="caller",sequence="0"):
    return {"FriendlyName":f"operator-{PARENT}","ConferenceSid":CONFERENCE,"StatusCallbackEvent":kind,
            "CallSid":sid,"ParticipantLabel":label,"SequenceNumber":sequence}


class Gateway:
    def __init__(self):
        self.created=[];self.ended_calls=[];self.ended_conferences=[];self.redirects=[]
        self.dial_gate=None;self.redirect_gate=None;self.redirect_failure=False
        self.on_redirect=None;self.dial_failure=False
    async def create_participant(self,conference,parent):
        self.created.append((conference,parent))
        if self.dial_gate:await self.dial_gate.wait()
        if self.dial_failure:raise OSError("fixture")
        return OUTBOUND
    async def find_participant(self,*args):return None
    async def end_call(self,sid):self.ended_calls.append(sid)
    async def end_conference(self,sid):self.ended_conferences.append(sid)
    async def redirect_call(self,sid,url):
        self.redirects.append((sid,url))
        if self.redirect_gate:await self.redirect_gate.wait()
        if self.on_redirect:await self.on_redirect()
        if self.redirect_failure:raise OSError("fixture redirect")


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():await asyncio.sleep(.005)


async def ringing(board):
    s=await board.start(PARENT)
    await board.conference_event(PARENT,event("participant-join"))
    await board.wait_idle()
    assert s.outbound_sid==OUTBOUND
    return s


@pytest.mark.parametrize("status",["no-answer","busy","failed"])
def test_unanswered_call_redirects_once_and_keeps_parent_and_capture_alive(tmp_path,status):
    async def run():
        gateway=Gateway();ended=[]
        async def on_end(sid):ended.append(sid)
        board=Switchboard(settings(),gateway,on_end=on_end)
        s=await ringing(board)
        await board.call_status(PARENT,{"CallSid":OUTBOUND,"CallStatus":status})
        await board.call_status(PARENT,{"CallSid":OUTBOUND,"CallStatus":status})
        await board.wait_idle()
        assert s.phase=="voicemail" and s.voicemail_reason==status
        assert board.active_count==1 and ended==[]
        assert gateway.redirects==[(PARENT,f"https://operator.example/voicemail/{PARENT}")]
        assert gateway.ended_calls==gateway.ended_conferences==[]
        assert await board.voicemail_started(PARENT)
        assert await board.voicemail_started(PARENT)
        await board.wait_idle()
        assert gateway.ended_calls == gateway.ended_conferences == []
        await board.conference_event(PARENT, event("participant-leave", sequence="1"))
        await board.wait_idle()
        assert gateway.ended_calls==[OUTBOUND]
        assert gateway.ended_conferences==[CONFERENCE]
        assert PARENT not in gateway.ended_calls and ended==[]
        await board.voicemail_finished(PARENT)
        await board.voicemail_finished(PARENT)
        await board.wait_idle()
        assert s.phase=="ended" and ended==[PARENT]
        assert PARENT in gateway.ended_calls
        await board.close()
    asyncio.run(run())


def test_stale_conference_and_terminal_callbacks_cannot_end_voicemail():
    async def run():
        gateway=Gateway();board=Switchboard(settings(),gateway)
        s=await ringing(board)
        await board.call_status(PARENT,{"CallSid":OUTBOUND,"CallStatus":"no-answer"})
        for sequence,kind,sid,label in [("1","participant-leave",PARENT,"caller"),
              ("2","participant-leave",OUTBOUND,"callee"),("3","conference-end","",""),
              ("4","conference-start","",""),("5","participant-join",OUTBOUND,"callee")]:
            await board.conference_event(PARENT,event(kind,sid,label,sequence))
        await board.call_status(PARENT,{"CallSid":OUTBOUND,"CallStatus":"completed"})
        await board.finished(PARENT)  # Delayed old <Dial action>.
        await board.wait_idle()
        assert s.phase=="voicemail" and not s.connected
        assert gateway.ended_calls==[]
        assert await board.voicemail_started(PARENT)
        await board.wait_idle()
        assert OUTBOUND in gateway.ended_calls and PARENT not in gateway.ended_calls
        await board.close()
    asyncio.run(run())


@pytest.mark.parametrize("status", ["no-answer", "busy", "failed"])
def test_unanswered_callee_leave_callback_can_start_fallback_before_call_status(status):
    async def run():
        gateway = Gateway()
        board = Switchboard(settings(), gateway)
        session = await ringing(board)
        form = event("participant-leave", OUTBOUND, "callee", "1")
        form["ParticipantCallStatus"] = status
        await board.conference_event(PARENT, form)
        await board.wait_idle()
        assert session.phase == "voicemail" and session.voicemail_reason == status
        assert len(gateway.redirects) == 1 and gateway.ended_calls == []
        await board.close()
    asyncio.run(run())


def test_old_dial_action_proves_departure_without_ending_voicemail():
    async def run():
        gateway = Gateway()
        board = Switchboard(settings(), gateway)
        session = await ringing(board)
        await board.call_status(PARENT, {"CallSid": OUTBOUND, "CallStatus": "no-answer"})
        await board.wait_idle()
        await board.finished(PARENT)
        await board.wait_idle()
        assert session.voicemail_caller_left and not session.voicemail_confirmed
        assert gateway.ended_calls == gateway.ended_conferences == []
        assert await board.voicemail_started(PARENT)
        await board.wait_idle()
        assert gateway.ended_calls == [OUTBOUND]
        assert gateway.ended_conferences == [CONFERENCE]
        assert session.phase == "voicemail"
        await board.close()
    asyncio.run(run())


def test_connected_calls_and_caller_hangup_never_redirect():
    async def run():
        gateway=Gateway();board=Switchboard(settings(),gateway)
        s=await ringing(board)
        await board.conference_event(PARENT,event("conference-start","","","1"))
        await board.conference_event(PARENT,event("participant-join",OUTBOUND,"callee","2"))
        assert s.connected
        await board.call_status(PARENT,{"CallSid":OUTBOUND,"CallStatus":"no-answer"})
        await board.wait_idle()
        assert s.phase=="ended" and gateway.redirects==[]
        assert not await board.voicemail_started(PARENT)
        await board.close()
        gateway=Gateway();board=Switchboard(settings(),gateway)
        s=await ringing(board)
        await board.conference_event(PARENT,event("participant-leave",sequence="1"))
        await board.call_status(PARENT,{"CallSid":OUTBOUND,"CallStatus":"no-answer"})
        await board.wait_idle()
        assert s.phase=="ended" and gateway.redirects==[]
        await board.close()
    asyncio.run(run())


@pytest.mark.parametrize("answer_evidence", ["in-progress", "participant-join"])
def test_answered_callee_never_falls_back_when_conference_start_callback_is_late(answer_evidence):
    async def run():
        gateway = Gateway()
        board = Switchboard(settings(), gateway)
        session = await ringing(board)
        if answer_evidence == "in-progress":
            await board.call_status(PARENT, {"CallSid": OUTBOUND, "CallStatus": "in-progress"})
        else:
            await board.conference_event(PARENT, event("participant-join", OUTBOUND, "callee", "1"))
        assert session.callee_answered and not session.connected
        await board.call_status(PARENT, {"CallSid": OUTBOUND, "CallStatus": "no-answer"})
        await board.wait_idle()
        assert session.phase == "ended" and gateway.redirects == []
        await board.close()
    asyncio.run(run())


def test_pending_answer_status_is_not_lost_when_terminal_status_arrives_before_rest_response():
    async def run():
        gateway = Gateway()
        gateway.dial_gate = asyncio.Event()
        board = Switchboard(settings(), gateway)
        session = await board.start(PARENT)
        await board.conference_event(PARENT, event("participant-join"))
        await until(lambda: bool(gateway.created))
        await board.call_status(PARENT, {"CallSid": OUTBOUND, "CallStatus": "in-progress"})
        await board.call_status(PARENT, {"CallSid": OUTBOUND, "CallStatus": "no-answer"})
        gateway.dial_gate.set()
        await board.wait_idle()
        assert session.callee_answered and session.phase == "ended"
        assert gateway.redirects == [] and session.pending_answered == set()
        await board.close()
    asyncio.run(run())


def test_setup_timeout_after_answer_does_not_create_voicemail():
    async def run():
        gateway = Gateway()
        board = Switchboard(settings(switchboard_setup_timeout=.03), gateway)
        session = await ringing(board)
        await board.call_status(PARENT, {"CallSid": OUTBOUND, "CallStatus": "in-progress"})
        await until(lambda: session.phase == "ended")
        await board.wait_idle()
        assert session.reason == "setup_timeout" and gateway.redirects == []
        await board.close()
    asyncio.run(run())


def test_verified_terminal_callback_claims_pending_answer_before_fallback_decision():
    async def run():
        gateway = Gateway()
        gateway.dial_gate = asyncio.Event()
        board = Switchboard(settings(), gateway)
        session = await board.start(PARENT)
        await board.conference_event(PARENT, event("participant-join"))
        await until(lambda: bool(gateway.created))
        await board.call_status(PARENT, {"CallSid": OUTBOUND, "CallStatus": "in-progress"})
        await board.call_status(PARENT, {"CallSid": OUTBOUND, "CallStatus": "no-answer",
            "From": board.settings.twilio_number, "To": board.settings.callee_number,
            "Direction": "outbound-api"})
        gateway.dial_gate.set()
        await board.wait_idle()
        assert session.callee_answered and session.phase == "ended"
        assert gateway.redirects == []
        await board.close()
    asyncio.run(run())


def test_setup_deadline_replacement_is_retained_and_voicemail_deadline_cleans_up():
    async def run():
        gateway=Gateway()
        board=Switchboard(settings(switchboard_setup_timeout=.01,voicemail_max_seconds=-29.95),gateway)
        s=await board.start(PARENT)
        await until(lambda:s.phase=="voicemail")
        assert PARENT in board._deadlines
        await until(lambda:s.phase=="ended")
        await board.wait_idle()
        assert s.reason=="voicemail_timeout" and len(gateway.redirects)==1
        assert PARENT in gateway.ended_calls
        await board.close()
    asyncio.run(run())


@pytest.mark.parametrize("confirm_first",[False,True])
def test_late_dial_completion_is_terminated_after_redirect_confirmation(confirm_first):
    async def run():
        gateway=Gateway();gateway.dial_gate=asyncio.Event()
        board=Switchboard(settings(switchboard_setup_timeout=.01),gateway)
        s=await board.start(PARENT)
        await board.conference_event(PARENT,event("participant-join"))
        await until(lambda:s.phase=="voicemail")
        await board.conference_event(PARENT,event("participant-leave",sequence="1"))
        if confirm_first:assert await board.voicemail_started(PARENT)
        gateway.dial_gate.set()
        await board.wait_idle()
        assert s.outbound_sid==OUTBOUND and s.phase=="voicemail"
        if not confirm_first:
            assert OUTBOUND not in gateway.ended_calls
            assert await board.voicemail_started(PARENT)
            await board.wait_idle()
        assert OUTBOUND in gateway.ended_calls and PARENT not in gateway.ended_calls
        await board.close()
    asyncio.run(run())


def test_late_rest_identity_mismatch_is_cleaned_without_ending_redirected_parent():
    async def run():
        gateway = Gateway()
        gateway.dial_gate = asyncio.Event()
        board = Switchboard(settings(), gateway)
        session = await board.start(PARENT)
        await board.conference_event(PARENT, event("participant-join"))
        await until(lambda: bool(gateway.created))
        await board.call_status(PARENT, {"CallSid": OTHER, "CallStatus": "no-answer",
            "From": board.settings.twilio_number, "To": board.settings.callee_number,
            "Direction": "outbound-api"})
        gateway.dial_gate.set()
        await board.wait_idle()
        assert session.phase == "voicemail" and session.extra_outbound_sid == OUTBOUND
        assert gateway.ended_calls == []
        assert await board.voicemail_started(PARENT)
        await board.finished(PARENT)
        await board.wait_idle()
        assert set(gateway.ended_calls) == {OTHER, OUTBOUND}
        assert session.phase == "voicemail" and PARENT not in gateway.ended_calls
        await board.close()
    asyncio.run(run())


def test_redirect_failure_ends_normally_without_second_attempt():
    async def run():
        gateway=Gateway();gateway.redirect_failure=True
        board=Switchboard(settings(),gateway)
        s=await ringing(board)
        await board.call_status(PARENT,{"CallSid":OUTBOUND,"CallStatus":"busy"})
        await board.wait_idle()
        assert s.phase=="ended" and s.reason=="voicemail_redirect_failed"
        assert len(gateway.redirects)==1 and PARENT in gateway.ended_calls
        await board.close()
    asyncio.run(run())


def test_signed_voicemail_fetch_overrides_ambiguous_redirect_http_failure():
    async def run():
        gateway=Gateway();gateway.redirect_failure=True
        board=Switchboard(settings(),gateway)
        gateway.on_redirect=lambda:board.voicemail_started(PARENT)
        s=await ringing(board)
        await board.call_status(PARENT,{"CallSid":OUTBOUND,"CallStatus":"busy"})
        await board.wait_idle()
        assert s.phase=="voicemail" and s.voicemail_confirmed
        assert PARENT not in gateway.ended_calls
        await board.close()
    asyncio.run(run())


def test_disabled_setting_preserves_previous_no_answer_cleanup():
    async def run():
        gateway=Gateway();board=Switchboard(settings(voicemail_enabled=False),gateway)
        s=await ringing(board)
        await board.call_status(PARENT,{"CallSid":OUTBOUND,"CallStatus":"no-answer"})
        await board.wait_idle()
        assert s.phase=="ended" and gateway.redirects==[]
        await board.close()
    asyncio.run(run())


def test_draining_preserves_existing_voicemail_and_counts_pending_redirect():
    async def run():
        gateway=Gateway();board=Switchboard(settings(),gateway)
        await ringing(board)
        gateway.redirect_gate=asyncio.Event()
        await board.call_status(PARENT,{"CallSid":OUTBOUND,"CallStatus":"no-answer"})
        await until(lambda:bool(gateway.redirects))
        await board.set_draining(True)
        assert board.active_count==1 and board.pending_count==1
        with pytest.raises(SessionRejected):await board.start(OTHER)
        gateway.redirect_gate.set()
        await board.wait_idle()
        assert board.active_count==1
        await board.close()
    asyncio.run(run())


def test_gateway_redirect_uses_absolute_url_once_without_dialing():
    async def run():
        gateway=TwilioGateway(settings());client=Mock();gateway._client=lambda:client
        url=f"https://operator.example/voicemail/{PARENT}"
        await gateway.redirect_call(PARENT,url)
        client.calls.assert_called_once_with(PARENT)
        client.calls.return_value.update.assert_called_once_with(url=url,method="POST")
        client.conferences.assert_not_called()
    asyncio.run(run())


def test_voicemail_uses_shorter_callee_ring_timeout():
    async def run():
        gateway = TwilioGateway(settings())
        client = Mock()
        gateway._client = lambda: client
        client.conferences.return_value.participants.create.return_value.call_sid = OUTBOUND
        assert await gateway.create_participant(CONFERENCE, PARENT) == OUTBOUND
        assert client.conferences.return_value.participants.create.call_args.kwargs["timeout"] == 20
    asyncio.run(run())
