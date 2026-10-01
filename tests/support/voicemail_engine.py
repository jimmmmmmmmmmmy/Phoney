"""Shared fixtures and fakes for focused integration checks."""

import asyncio


from types import SimpleNamespace


import pytest


from switchboard import Switchboard, SessionRejected


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
