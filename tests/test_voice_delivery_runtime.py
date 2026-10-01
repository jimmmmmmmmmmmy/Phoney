"""The next sentence may prepare early, but unplayed speech never survives cancellation."""

import asyncio

from operator_service.sessions import AGENT, HUMAN, REMOTE
from support.operator_keypad import Harness, Provider, until
from voice_stack.audio import FRAME_BYTES


def test_one_phrase_prefetch_waits_for_playback_and_barge_in_closes_unheard_audio(tmp_path, monkeypatch):
    async def run():
        phrases = [
            "I can help you understand the available options and choose what works for your schedule.",
            "We can take one detail at a time so the conversation stays clear and comfortable.",
            "There is no need to rush, and we can adjust the next step once you have the information.",
        ]
        requested, closed, sessions = [], [], []

        class Synthesis:
            def __init__(self, http, key, voice, **options):
                self.options = options
                self.closed = False
                sessions.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                self.closed = True

            async def speech_bytes(self, text, *, previous_text=None):
                requested.append((text, previous_text))
                try:
                    # One small provider chunk is enough to prepare playback.
                    yield bytes([0x31 + phrases.index(text)]) * FRAME_BYTES * 3
                    if text == phrases[2]:
                        await asyncio.Event().wait()
                finally:
                    closed.append(text)

        monkeypatch.setattr("operator_service.runtime.SpeechSession", Synthesis)
        h = Harness(tmp_path, provider=Provider(reply=" ".join(phrases)), acknowledge=False)
        try:
            call = await h.joined()
            call.agent_snapshot = h.registry.resolve_slot("1")
            call.agent_name = call.agent_snapshot.name
            await h.store.set_mode(call.id, AGENT)
            h.controller._start_dialogue(call)
            await until(lambda: bool(h.remote.marks()) and len(requested) == 2)
            assert requested == [(phrases[0], None), (phrases[1], phrases[0])]
            assert sessions[0].options["delivery"] is h.voice.delivery
            assert h.remote.frames(0x31) and not h.remote.frames(0x32)
            assert not h.delivered  # The first phrase's playback mark is still pending.
            assert phrases[1] not in closed

            await h.controller.mark(call.id, REMOTE, h.remote.marks()[0], "played")
            await until(lambda: len(h.remote.marks()) == 2 and len(requested) == 3)
            assert requested[-1] == (phrases[2], " ".join(phrases[:2]))
            assert h.remote.frames(0x32) and not h.remote.frames(0x33)
            assert h.delivered[0][1]["delivery"] == "played"

            # Actual caller speech revokes the run while its one next phrase is ready.
            await h.controller.transcript(call.id, REMOTE, "Actually, I have a correction.", final=False)
            await until(lambda: not h.controller.playing(call.id) and sessions[0].closed)
            assert phrases[2] in closed
            assert len(requested) == 3
            assert not h.remote.frames(0x33)
            assert h.delivered[-1][1]["delivery"] == "interrupted"
            assert [turn["text"] for turn in call.turns if turn["speaker"] == "agent"] == phrases[:1]
            await h.controller.set_mode(call.id, HUMAN)
            assert call.active and call.mode == HUMAN
        finally:
            await h.close()

    asyncio.run(run())
