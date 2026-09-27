"""Caller updates during an outage survive the next local voicemail reply."""

import asyncio
import json

import httpx
import pytest

from operator_service.sessions import REMOTE
from test_operator_keypad import REMOTE_SID, REMOTE_STREAM, until
from test_operator_voicemail_integration import Harness, Provider, quick_timers


class StalledGemini(Provider):
    def __init__(self, *, initial_readback=False):
        super().__init__()
        self.initial_readback = initial_readback

    def transport(self):
        good = super().transport()
        self.gemini_requests = []

        async def handle(request):
            if "generativelanguage" not in request.url.host:
                return await good.handle_async_request(request)
            self.gemini_requests.append(json.loads(request.content))
            if self.initial_readback and len(self.gemini_requests) == 1:
                data = {"candidates": [{"content": {"parts": [{"text":
                    "The amount is seven hundred dollars. Is that right?"}]}, "finishReason": "STOP"}]}
                return httpx.Response(200, text="data: " + json.dumps(data) + "\n\n")
            await asyncio.Future()

        return httpx.MockTransport(handle)


async def pending_reply(h, phase):
    s = await h.incoming()
    await h.controller.on_timeout(s, "owner-no-answer")
    await h.ready()
    await h.controller.transcript(s.id, REMOTE,
        "The amount is seven hundred dollars.", segment_id="message")
    await until(lambda: len(h.provider.gemini_requests) == 1)
    if phase in {"confirm", "followup"}:
        await h.ready()
        await h.controller.transcript(s.id, REMOTE, "Yes." if phase == "confirm" else "No.", segment_id="early-answer")
        await until(lambda: len(h.provider.gemini_requests) == 2)
    return s, h.controller.players[s.id]


@pytest.mark.parametrize("phase", ["readback", "confirm", "followup"])
def test_correction_during_stalled_generation_is_kept_before_completed_hangup(
        tmp_path, monkeypatch, phase):
    quick_timers(monkeypatch, pause_seconds=.01)
    monkeypatch.setattr("operator_service.runtime.VOICEMAIL_FIRST_TEXT_SECONDS", .08)

    async def run():
        h = Harness(tmp_path, StalledGemini(initial_readback=phase == "confirm"))
        try:
            s, old_run = await pending_reply(h, phase)
            delivered_before = len(h.delivered)
            requests_before = len(h.provider.gemini_requests)
            correction = "Actually, seven hundred fifty dollars."
            await h.controller.transcript(s.id, REMOTE, correction,
                segment_id="correction", speech_final=True)
            await old_run
            # The failed request must not speak its old readback or a farewell.
            assert len(h.delivered) == delivered_before and s.active
            assert not h.dialer.replacements

            await until(lambda: len(h.provider.gemini_requests) == requests_before + 1)
            await h.ready()
            spoken = " ".join(item[0][1] for item in h.delivered[delivered_before:])
            assert correction not in spoken and "anything else" in spoken
            assert s.voicemail_phase == "followup"
            assert any(turn["text"] == correction for turn in s.turns)
            assert "Goodbye" not in spoken
            assert correction in json.dumps(h.provider.gemini_requests[-1]["contents"])
            assert h.controller.router(s.id).attached(REMOTE)
            assert s.legs[REMOTE].stream_sid == REMOTE_STREAM
            assert not h.dialer.replacements and s.active

            # A subsequent, explicit confirmation still waits for played audio.
            h.remote.acknowledge = False
            marks_before, delivered_before = len(h.remote.marks()), len(h.delivered)
            await h.controller.transcript(s.id, REMOTE, "No, that's all.",
                segment_id="confirmed", speech_final=True)
            await until(lambda: len(h.remote.marks()) > marks_before)
            assert s.active and REMOTE_SID not in h.dialer.ended
            assert len(h.delivered) == delivered_before
            h.remote.acknowledge = True
            await h.controller.mark(s.id, REMOTE, h.remote.marks()[-1], "played")
            await until(lambda: not s.active)
            assert h.delivered[-1][0][1] == "Thank you for your message. Goodbye."
            assert h.delivered[-1][1]["delivery"] == "played"
            assert REMOTE_SID in h.dialer.ended and not h.dialer.replacements
            assert all(httpx.URL(url).path == "/v1/text-to-speech/owner-voice/stream"
                       for url, _ in h.provider.requests)
        finally:
            await h.close()

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["readback", "confirm", "followup"])
def test_open_correction_during_stall_waits_for_final_endpoint(tmp_path, monkeypatch, phase):
    quick_timers(monkeypatch, pause_seconds=.01)
    monkeypatch.setattr("operator_service.runtime.VOICEMAIL_FIRST_TEXT_SECONDS", .08)

    async def run():
        h = Harness(tmp_path, StalledGemini(initial_readback=phase == "confirm"))
        try:
            s, old_run = await pending_reply(h, phase)
            delivered_before = len(h.delivered)
            requests_before = len(h.provider.gemini_requests)
            await h.controller.transcript(s.id, REMOTE, "Actually", final=False,
                speech_started=True, speech_final=False)
            await old_run
            vm = h.controller._voicemail_agents[s.id]
            assert not vm.busy and vm.utterance_open
            await asyncio.sleep(.12)
            assert s.active and len(h.delivered) == delivered_before
            assert len(h.provider.gemini_requests) == requests_before

            correction = "Actually, seven hundred fifty dollars."
            await h.controller.transcript(s.id, REMOTE, correction,
                segment_id="correction", speech_final=False)
            await asyncio.sleep(.04)
            assert vm.pending_final and vm.utterance_open
            assert len(h.provider.gemini_requests) == requests_before
            await h.controller.transcript(s.id, REMOTE, "", speech_final=True)
            await until(lambda: len(h.provider.gemini_requests) == requests_before + 1)
            await h.ready()
            spoken = " ".join(item[0][1] for item in h.delivered[delivered_before:])
            assert correction not in spoken and "anything else" in spoken
            assert s.voicemail_phase == "followup"
            assert any(turn["text"] == correction for turn in s.turns)
            assert "Goodbye" not in spoken
            assert s.active and not h.dialer.replacements
        finally:
            await h.close()

    asyncio.run(run())
