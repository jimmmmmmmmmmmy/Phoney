"""Focused product and boundary checks; test helpers live in support."""

from copy import deepcopy
import asyncio
import json

from operator_service.sessions import OWNER, REMOTE

from support.automatic_agents import PRIVATE_PROMPT, SNAPSHOT, detection, harness, settle


def test_live_detection_uses_private_prompt_current_context_and_one_takeover(tmp_path):
    async def run():
        h = harness(tmp_path)
        s = await h.joined()
        await h.controller.transcript(s.id, OWNER, "Please describe the car's service history.", segment_id="owner-1")
        await h.controller.transcript(s.id, REMOTE, "The transmission was repaired last year.", segment_id="caller-1")
        result = detection()
        for _ in range(8):
            h.controller.on_detection(s.id, deepcopy(result))
        await h.complete()
        first_epoch = s.reply_epoch
        h.controller.on_detection(s.id, detection(duration=8000))
        await settle(h)
        assert h.lookups == ["ai-detected"]
        assert s.profile == "aiwatch" and s.agent_name == SNAPSHOT.name
        assert s.reply_epoch == first_epoch
        requests = [body for url, body in h.provider.requests if "generativelanguage" in url]
        assert len(requests) == 1
        assert PRIVATE_PROMPT in json.dumps(requests[0]["systemInstruction"])
        assert "Selected trusted instructions" not in json.dumps(requests[0]["systemInstruction"])
        assert "service history" in json.dumps(requests[0]["contents"])
        assert "transmission was repaired" in json.dumps(requests[0]["contents"])
        assert "transmission was repaired" not in json.dumps(requests[0]["systemInstruction"])
        assert s.active and not h.dialer.ended
        await h.close()

    asyncio.run(run())
