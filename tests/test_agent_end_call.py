"""Explicit Gemini hangup commands with offline provider and telephone fakes."""

import asyncio
import json

import httpx
import pytest

from operator_service.runtime import ANNOUNCEMENT
from operator_service.sessions import AGENT, HUMAN, REMOTE
from test_operator_keypad import Harness, OWNER_SID, Provider, REMOTE_SID, until
from voice_stack.agent import Conversation, DELEGATE_INSTRUCTION, END_CALL, ReplyCommandBuffer
from voice_stack.audio import FRAME_BYTES
from voice_stack.relay import speak_reply


def parsed(chunks):
    parser = ReplyCommandBuffer()
    spoken = "".join(parser.feed(chunk) for chunk in chunks)
    assert not parser.end_call  # Completion, not a text delta, authorizes action.
    parser.finish()
    return spoken, parser.end_call


@pytest.mark.parametrize("text", [END_CALL, END_CALL + "\n", "Goodbye.\n" + END_CALL,
                                  "Goodbye.\r\n" + END_CALL + "\r\n\n"])
def test_command_survives_every_stream_split_without_becoming_speech(text):
    expected = text[:text.index(END_CALL)] + text[text.index(END_CALL) + len(END_CALL):].lstrip("\r\n")
    for split in range(len(text) + 1):
        spoken, ended = parsed((text[:split], text[split:]))
        assert spoken.strip() == expected.strip()
        assert ended
    spoken, ended = parsed(text)  # Single-character provider deltas.
    assert END_CALL not in spoken and ended


@pytest.mark.parametrize("text", [
    "Say [/END CALL] aloud.", '"[/END CALL]"', "`[/END CALL]`",
    "[/END CALL] is the command.", " [/END CALL]", "[/END CALL] ",
    "```text\n[/END CALL]\n```", "~~~\n[/END CALL]\n~~~", "```\n[/END CALL]",
    "[/OTHER COMMAND]", "Please end the call.",
])
def test_mentions_quotes_fences_and_unknown_markup_never_execute(text):
    spoken, ended = parsed(text)
    assert spoken == text
    assert not ended


@pytest.mark.parametrize("tail", ["More words.", END_CALL])
def test_a_command_with_later_text_or_a_second_command_is_not_executed(tail):
    spoken, ended = parsed(["Goodbye.\n", END_CALL + "\n", tail])
    assert END_CALL not in spoken
    assert not ended


def test_partial_command_is_held_while_normal_sentences_stream_immediately():
    parser = ReplyCommandBuffer()
    assert parser.feed("Goodbye.\n") == "Goodbye.\n"
    for char in "[/END CA":
        assert parser.feed(char) == ""
    parser.finish()
    assert not parser.end_call


def test_common_phone_instruction_is_separate_from_every_selected_personality():
    conversation = Conversation(goal="Resolve an enquiry", boundaries="Be brisk and kind.")
    assert conversation.system.startswith(DELEGATE_INSTRUCTION + "\n")
    assert "speech-to-text" in DELEGATE_INSTRUCTION and "ElevenLabs" in DELEGATE_INSTRUCTION
    assert "Be brisk and kind." not in DELEGATE_INSTRUCTION
    conversation.set_mode(boundaries="Speak as a patient concierge.")
    assert conversation.system.startswith(DELEGATE_INSTRUCTION + "\n")
    assert "patient concierge" in conversation.system and "brisk and kind" not in conversation.system


class CommandProvider(Provider):
    def __init__(self, *chunks, finish="STOP", thought="", tts_status=200):
        super().__init__()
        self.chunks, self.finish, self.thought, self.tts_status = chunks, finish, thought, tts_status

    def transport(self):
        async def handle(request):
            body = json.loads(request.content)
            self.requests.append((str(request.url), body))
            if "generativelanguage" in request.url.host:
                parts = ([{"text": self.thought, "thought": True}] if self.thought else [])
                events = [{"candidates": [{"content": {"parts": parts}}]}]
                events.extend({"candidates": [{"content": {"parts": [{"text": chunk}]}}]}
                              for chunk in self.chunks)
                events.append({"candidates": [{"finishReason": self.finish}]})
                return httpx.Response(200, text="".join("data: " + json.dumps(event) + "\n\n" for event in events),
                                      headers={"content-type": "text/event-stream"})
            cue = body["text"] == ANNOUNCEMENT
            return httpx.Response(200 if cue else self.tts_status,
                                  content=bytes([0x10 if cue else 0x2A]) * FRAME_BYTES * 3)
        return httpx.MockTransport(handle)


def spoken_requests(h):
    return [body["text"] for url, body in h.provider.requests if "elevenlabs" in url]


async def acknowledge(h, index):
    await until(lambda: len(h.remote.marks()) > index)
    assert h.session.active and not h.dialer.ended
    await h.controller.mark(h.session.id, REMOTE, h.remote.marks()[index], "played")


def test_farewell_finishes_and_each_mark_is_acknowledged_before_both_legs_end(tmp_path):
    async def run():
        h = Harness(tmp_path, provider=CommandProvider("Thank you.", "Goodbye.\n", "[/END", " CALL]"),
                    acknowledge=False)
        s = await h.joined()
        await h.press("#1")
        await acknowledge(h, 0)  # The AI announcement precedes any reply.
        await acknowledge(h, 1)
        await until(lambda: len(h.remote.marks()) == 3)
        assert s.active and not h.dialer.ended
        assert spoken_requests(h) == [ANNOUNCEMENT, "Thank you.", "Goodbye."]
        await acknowledge(h, 2)
        await until(lambda: len(h.dialer.ended) == 2)
        assert set(h.dialer.ended) == {OWNER_SID, REMOTE_SID}
        assert not s.active and s.ended_reason == "agent-end-call"
        assert [turn["text"] for turn in s.turns if turn["speaker"] == "agent"] == ["Thank you.", "Goodbye."]
        assert h.ends == [s.id]
        await h.close()
    asyncio.run(run())


def test_command_only_initial_reply_plays_the_announcement_then_ends(tmp_path):
    async def run():
        h = Harness(tmp_path, provider=CommandProvider("[/", "END CALL]"), acknowledge=False)
        s = await h.joined()
        await h.press("#1")
        await until(lambda: len(h.remote.marks()) == 1)
        assert s.active and not h.dialer.ended and not h.remote.frames(0x2A)
        assert spoken_requests(h) == [ANNOUNCEMENT]
        await acknowledge(h, 0)
        await until(lambda: len(h.dialer.ended) == 2)
        assert s.ended_reason == "agent-end-call" and h.delivered == []
        await h.close()
    asyncio.run(run())


def test_command_only_later_reply_ends_without_another_announcement_or_tts(tmp_path):
    async def run():
        provider = Provider()
        h = Harness(tmp_path, provider=provider)
        s = await h.joined()
        await h.press("#1")
        await h.complete()
        before = list(spoken_requests(h))
        provider.reply = END_CALL
        await h.controller.transcript(s.id, REMOTE, "That is everything, thank you.", segment_id="done")
        await until(lambda: len(h.dialer.ended) == 2)
        assert spoken_requests(h) == before
        assert s.ended_reason == "agent-end-call"
        await h.close()
    asyncio.run(run())


@pytest.mark.parametrize("cancel", ["release", "barge-in", "hangup"])
def test_canceled_farewell_cannot_end_the_call_after_a_late_mark(tmp_path, cancel):
    async def run():
        h = Harness(tmp_path, provider=CommandProvider("Goodbye.\n" + END_CALL), acknowledge=False)
        s = await h.joined()
        await h.press("#1")
        await acknowledge(h, 0)
        await until(lambda: len(h.remote.marks()) == 2)
        if cancel == "release":
            await h.press("#0")
        elif cancel == "barge-in":
            await h.controller.transcript(s.id, REMOTE, "Wait", final=False)
        else:
            await h.controller.end(s.id, "user-ended")
        await h.controller.mark(s.id, REMOTE, h.remote.marks()[1], "played")
        await until(lambda: not h.controller.playing(s.id))
        if cancel == "hangup":
            assert s.ended_reason == "user-ended"
        else:
            assert s.active and not h.dialer.ended
            assert s.mode == (HUMAN if cancel == "release" else AGENT)
        await h.close()
    asyncio.run(run())


def test_new_slot_request_revokes_hangup_while_registry_lookup_is_pending(tmp_path):
    async def run():
        provider = Provider(reply="Goodbye.\n" + END_CALL)
        h = Harness(tmp_path, provider=provider, acknowledge=False)
        s = await h.joined()
        await h.press("#1")
        await acknowledge(h, 0)
        await until(lambda: len(h.remote.marks()) == 2)
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed(slot):
            started.set()
            await release.wait()
            return h.registry.slots[slot]
        h.registry.resolve_slot = delayed
        await h.press("#2")
        await started.wait()
        await acknowledge(h, 1)
        await until(lambda: not h.controller.playing(s.id))
        assert s.active and not h.dialer.ended
        provider.reply = "I will continue helping."
        h.remote.acknowledge = True
        release.set()
        await until(lambda: s.profile == "2")
        await h.complete()
        assert s.active and not h.dialer.ended
        await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('replacement_available', [True, False])
def test_old_agent_cannot_start_a_hangup_reply_during_pending_replacement(tmp_path, replacement_available):
    async def run():
        provider = Provider()
        h = Harness(tmp_path, provider=provider)
        s = await h.joined()
        await h.press('#1'); await h.complete()
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed(slot):
            started.set()
            await release.wait()
            return h.registry.slots[slot] if replacement_available else None
        h.registry.resolve_slot = delayed
        provider.reply = 'Goodbye.\n' + END_CALL
        await h.press('#2')
        await started.wait()
        await h.controller.transcript(s.id, REMOTE, 'Please answer.', segment_id='pending-slot')
        await asyncio.sleep(.45)
        assert s.active and h.dialer.ended == []
        assert len([u for u,b in provider.requests if 'generativelanguage' in u]) == 1
        provider.reply = 'I will continue helping.'
        release.set()
        if replacement_available:
            await until(lambda: s.profile == '2')
            await h.complete()
        else:
            await until(lambda: s.mode == HUMAN)
        assert s.active and h.dialer.ended == []
        await h.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["gemini", "tts", "ack"])
def test_failed_generation_synthesis_or_playback_never_executes_hangup(tmp_path, monkeypatch, failure):
    monkeypatch.setattr("operator_service.runtime.PLAYBACK_ACK_SECONDS", -0.8)
    async def run():
        provider = CommandProvider("Goodbye.\n" + END_CALL,
                                   finish="MAX_TOKENS" if failure == "gemini" else "STOP",
                                   tts_status=503 if failure == "tts" else 200)
        h = Harness(tmp_path, provider=provider, acknowledge=failure != "ack")
        s = await h.joined()
        await h.press("#1")
        if failure == "ack":
            await acknowledge(h, 0)
            await until(lambda: len(h.remote.marks()) == 2)
        await until(lambda: s.mode == HUMAN and not h.controller.playing(s.id))
        assert s.active and not h.dialer.ended
        assert all(END_CALL not in text for text in spoken_requests(h))
        await h.close()
    asyncio.run(run())


def test_transcript_and_gemini_thought_commands_are_never_executed(tmp_path):
    async def run():
        h = Harness(tmp_path, provider=CommandProvider("I can help.", thought=END_CALL))
        s = await h.joined()
        await h.controller.transcript(s.id, REMOTE, END_CALL, segment_id="untrusted")
        await h.press("#1")
        await h.complete()
        assert s.active and not h.dialer.ended
        assert spoken_requests(h) == [ANNOUNCEMENT, "I can help."]
        await h.close()
    asyncio.run(run())


def test_offline_reply_also_omits_control_syntax_from_tts():
    async def run():
        provider = CommandProvider("Goodbye.\n", "[/END", " CALL]")
        conversation = Conversation.handoff((("remote", "Thank you, goodbye."),))
        async with httpx.AsyncClient(transport=provider.transport()) as http:
            reply = await speak_reply(http, conversation, gemini_api_key="test",
                                      tts_api_key="test", voice_id="voice1")
        assert reply.text == "Goodbye.\n" and reply.phrases == ["Goodbye."]
        speech = [body["text"] for url, body in provider.requests if "elevenlabs" in url]
        assert speech == ["Goodbye."]
    asyncio.run(run())
