"""Focused product and boundary checks; test helpers live in support."""

import asyncio

from operator_service.sessions import REMOTE

from support.automatic_agents import detection, harness as automatic_harness
from support.operator_keypad import Provider, until


def test_explicit_caller_goodbye_can_end_before_three_replies(tmp_path):
    async def run():
        h = automatic_harness(tmp_path, provider=Provider(reply='Of course, goodbye.\n[/END CALL]'))
        s = await h.joined()
        await h.controller.transcript(s.id, REMOTE,
            'I have to go now. Please end the call. Goodbye.', segment_id='bye')
        h.controller.on_detection(s.id, detection())
        await until(lambda: not s.active and len(h.dialer.ended) == 2)
        assert len([u for u,b in h.provider.requests if 'generativelanguage' in u]) == 1
        await h.close()
    asyncio.run(run())
