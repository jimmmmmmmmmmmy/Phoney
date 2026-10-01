"""Focused product and boundary checks; test helpers live in support."""

import asyncio
import json

from operator_service.sessions import OWNER, REMOTE
from voice_stack.prompts import VOICEMAIL_GREETING

from support.operator_keypad import OWNER_SID, REMOTE_SID, until
from support.operator_voicemail_integration import Harness, quick_timers


def test_unanswered_call_greets_reads_message_confirms_then_hangs_up(tmp_path, monkeypatch):
    quick_timers(monkeypatch)
    async def run():
        h = Harness(tmp_path)
        s = await h.incoming()
        await h.controller.on_timeout(s, "owner-no-answer")
        await h.ready()
        assert h.provider.phases == []
        assert h.delivered[0][0][1] == VOICEMAIL_GREETING
        assert h.delivered[0][1]['delivery'] == 'played'
        assert OWNER_SID in h.dialer.ended and REMOTE_SID not in h.dialer.ended
        assert not h.router.attached(OWNER) and s.voicemail
        await h.controller.transcript(s.id, REMOTE, "This is Alex. Tomorrow's meeting is at ten.", segment_id="m1")
        await until(lambda: h.provider.phases == ["readback"])
        await h.ready()
        req = [body for url, body in h.provider.requests if "generativelanguage" in url][-1]
        assert "Tomorrow's meeting is at ten" in json.dumps(req['contents'])
        assert VOICEMAIL_GREETING in json.dumps(req['contents'])
        assert s.active and h.delivered[-1][1]['delivery'] == 'played'
        await h.controller.transcript(s.id, REMOTE, "Yes, that's correct.", segment_id="m2")
        await until(lambda: not s.active)
        await h.store.wait_idle()
        assert h.provider.phases == ["readback", "confirm"]
        assert REMOTE_SID in h.dialer.ended
        assert h.delivered[-1][1]['delivery'] == 'played'
        assert all("[/END CALL]" not in body.get('text', '') for _, body in h.provider.requests)
        await h.close()
    asyncio.run(run())
