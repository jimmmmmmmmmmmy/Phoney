"""Focused product and boundary checks; test helpers live in support."""

import asyncio

import pytest

from switchboard import SessionRejected, Switchboard

from support.voicemail_engine import Gateway, OTHER, OUTBOUND, PARENT, ringing, settings, until


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
