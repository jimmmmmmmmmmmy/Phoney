"""Count heard replies and independently authorize the bounded demo hangup."""
import asyncio
import json

from operator_service.sessions import REMOTE
from test_operator_keypad import Harness, Provider, until
from test_automatic_agents import harness as automatic_harness, detection
from voice_stack.prompts import VOICE_CLONE_PROMPT


def test_detection_agent_rejects_early_end_then_hangs_up_after_third_playback(tmp_path):
    async def run():
        h = automatic_harness(tmp_path, provider=Provider(reply='Thanks.\n[/END CALL]'))
        s = await h.joined()
        h.controller.on_detection(s.id, detection())
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
        h = automatic_harness(tmp_path, provider=Provider(reply='Of course, goodbye.\n[/END CALL]'))
        s = await h.joined()
        await h.controller.transcript(s.id, REMOTE,
            'I have to go now. Please end the call. Goodbye.', segment_id='bye')
        h.controller.on_detection(s.id, detection())
        await until(lambda: not s.active and len(h.dialer.ended) == 2)
        assert len([u for u,b in h.provider.requests if 'generativelanguage' in u]) == 1
        await h.close()
    asyncio.run(run())


def test_manual_voice_clone_is_not_forced_to_end_after_three_replies(tmp_path):
    async def run():
        h = Harness(tmp_path, provider=Provider(reply='Please continue.'))
        h.registry.slots['1'].prompt = VOICE_CLONE_PROMPT
        s = await h.joined()
        await h.press('#1')
        await h.complete()
        for reply in (2, 3, 4):
            await h.controller.transcript(s.id, REMOTE, f'More details {reply}.', segment_id=f'r{reply}')
            await until(lambda: h.controller._agent_reply_counts.get(s.id) == reply)
            assert s.active and not h.dialer.ended
        requests = [b for u,b in h.provider.requests if 'generativelanguage' in u]
        assert all('ACTIVE WORKFLOW STEP' not in json.dumps(b['systemInstruction']) for b in requests)
        # Manual agents still retain the explicit, playback-acknowledged end command.
        h.provider.reply = 'Thank you. Goodbye.\n[/END CALL]'
        await h.controller.transcript(s.id, REMOTE, 'That is everything. Goodbye.', segment_id='done')
        await until(lambda: not s.active)
        assert len(h.dialer.ended) == 2
        await h.close()
    asyncio.run(run())


def test_detection_third_reply_does_not_ignore_new_caller_context(tmp_path):
    async def run():
        h = automatic_harness(tmp_path, provider=Provider(delay=.08, reply='Please continue.'))
        s = await h.joined()
        h.controller.on_detection(s.id, detection())
        await h.complete()
        await h.controller.transcript(s.id, REMOTE, 'More details.', segment_id='r2')
        await until(lambda: h.controller._agent_reply_counts.get(s.id) == 2)
        h.provider.reply = 'Thank you. Goodbye.\n[/END CALL]'
        await h.controller.transcript(s.id, REMOTE, 'The mileage is ninety thousand.', segment_id='r3')
        await until(lambda: len([u for u,b in h.provider.requests if 'generativelanguage' in u]) == 3)
        await h.controller.transcript(s.id, REMOTE, 'I also need to explain the condition.', segment_id='new')
        await until(lambda: h.controller._agent_reply_counts.get(s.id) == 3)
        assert s.active and not h.dialer.ended
        await until(lambda: not s.active)
        requests = [b for u,b in h.provider.requests if 'generativelanguage' in u]
        assert len(requests) == 4
        assert 'explain the condition' in json.dumps(requests[-1]['contents'])
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
