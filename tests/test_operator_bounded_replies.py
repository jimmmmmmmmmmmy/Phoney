"""Count heard replies and independently authorize the bounded demo hangup."""
import asyncio
import json

from operator_service.sessions import REMOTE
from test_operator_keypad import Harness, Provider, until
from voice_stack.prompts import VOICE_CLONE_PROMPT


def test_bounded_personality_rejects_early_end_then_hangs_up_after_third_playback(tmp_path):
    async def run():
        h = Harness(tmp_path, provider=Provider(reply='Thanks.\n[/END CALL]'))
        h.registry.slots['1'].prompt = VOICE_CLONE_PROMPT
        s = await h.joined()
        await h.press('#1')
        await h.complete()
        assert s.active and h.controller._agent_reply_counts[s.id] == 1
        h.provider.reply = 'What else should I know?'
        await h.controller.transcript(s.id, REMOTE, 'The tires need replacing.', segment_id='r2')
        await until(lambda: h.controller._agent_reply_counts.get(s.id) == 2)
        assert s.active and h.dialer.ended == []
        # The runtime also enforces the upper bound if the model omits its marker.
        h.provider.reply = 'Thanks for the information. Goodbye!'
        await h.controller.transcript(s.id, REMOTE, 'That is everything.', segment_id='r3')
        await until(lambda: not s.active and len(h.dialer.ended) == 2)
        requests = [b for u,b in h.provider.requests if 'generativelanguage' in u]
        assert len(requests) == 3
        for n, body in enumerate(requests, 1):
            assert f'ACTIVE WORKFLOW STEP {n} OF 3' in json.dumps(body['systemInstruction'])
        assert len(h.delivered) >= 3
        assert all(meta['delivery'] == 'played' for _,meta in h.delivered)
        await h.close()
    asyncio.run(run())


def test_explicit_caller_goodbye_can_end_before_three_replies(tmp_path):
    async def run():
        h = Harness(tmp_path, provider=Provider(reply='Of course, goodbye.\n[/END CALL]'))
        h.registry.slots['1'].prompt = VOICE_CLONE_PROMPT
        s = await h.joined()
        await h.controller.transcript(s.id, REMOTE,
            'I have to go now. Please end the call. Goodbye.', segment_id='bye')
        await h.press('#1')
        await until(lambda: not s.active and len(h.dialer.ended) == 2)
        assert len([u for u,b in h.provider.requests if 'generativelanguage' in u]) == 1
        await h.close()
    asyncio.run(run())


def test_custom_personality_keeps_its_own_turn_policy(tmp_path):
    async def run():
        h = Harness(tmp_path, provider=Provider(reply='Goodbye.\n[/END CALL]'))
        s = await h.joined()
        await h.press('#1')
        await until(lambda: not s.active)
        body = next(b for u,b in h.provider.requests if 'generativelanguage' in u)
        assert 'ACTIVE WORKFLOW STEP' not in json.dumps(body['systemInstruction'])
        await h.close()
    asyncio.run(run())
