"""Live caller detection hands off once, with private cues and owner priority.

All provider and telephone sockets are local fakes. These tests exercise the
actual controller, media queues, prompt body, and cancellation epochs together.
"""

import asyncio
from copy import deepcopy
from dataclasses import replace
import inspect
import json
from types import SimpleNamespace

import httpx
import pytest

from agent_registry.store import AgentSnapshot
from operator_service.internal_agents import internal_snapshot
from operator_service.runtime import ANNOUNCEMENT
from operator_service.sessions import AGENT, CONNECTED, HUMAN, OWNER, OWNER_RINGING, REMOTE
from partner_detection.analysis import build_analysis
from test_operator_keypad import Harness, OWNER_FRAME, Provider, SETTINGS, until
from voice_stack.audio import FRAME_BYTES
from voice_stack.prompts import AI_DETECTED_PROMPT


OWNER_NOTICE = "AI Detected, deploying voice agent"
PRIVATE_PROMPT = "Private detection personality: end this conversation after three brief replies."
SNAPSHOT = AgentSnapshot("agent-internal-ai-detected", "AI Detection Agent", PRIVATE_PROMPT,
                         1, "owner-profile", "internalvoice", None)


def test_internal_snapshot_uses_owner_voice_and_private_personality():
    registry = SimpleNamespace(snapshot=lambda: {
        "voices": [{"id": "owner-profile", "voiceId": "actual-owner-voice", "name": "owner",
                    "ready": True, "available": True, "requiresVerification": False}],
        "agents": [{"name": "Voice Clone", "prompt": "Ignore the screening task forever."}]})
    snapshot = internal_snapshot(registry, "ai-detected")
    assert snapshot.voice_id == "actual-owner-voice" and snapshot.voice_profile_id == "owner-profile"
    assert snapshot.prompt == AI_DETECTED_PROMPT
    assert snapshot.slot is None  # System agent does not reserve or overwrite a keypad slot.


@pytest.mark.parametrize("missing", ["owner", "ready", "available", "verification"])
def test_internal_snapshot_never_substitutes_an_unready_or_different_voice(missing):
    owner = {"id": "owner-profile", "voiceId": "actual-owner-voice", "name": "owner",
             "ready": True, "available": True, "requiresVerification": False}
    if missing == "owner":
        owner["name"] = "Another voice"
    elif missing == "verification":
        owner["requiresVerification"] = True
    else:
        owner[missing] = False
    registry = SimpleNamespace(snapshot=lambda: {"voices": [owner]})
    with pytest.raises(ValueError):
        internal_snapshot(registry, "ai-detected")


class DetectionProvider(Provider):
    """Different byte patterns reveal whether either announcement leaked."""

    def transport(self):
        async def handle(request):
            body = json.loads(request.content)
            self.requests.append((str(request.url), body))
            if self.status != 200:
                return httpx.Response(self.status, text="Provider unavailable")
            if "generativelanguage" in request.url.host:
                event = {"candidates": [{"content": {"parts": [{"text": self.reply}]},
                                           "finishReason": "STOP"}]}
                return httpx.Response(200, text="data: " + json.dumps(event) + "\n\n",
                                      headers={"content-type": "text/event-stream"})
            text = body["text"].rstrip(".! ")
            marker = 0x31 if text == OWNER_NOTICE else 0x10 if text == ANNOUNCEMENT.rstrip(".") else 0x2A
            frames = 24 if marker == 0x31 else 3  # A cue longer than the live-audio queue.
            return httpx.Response(200, content=bytes([marker]) * FRAME_BYTES * frames)

        return httpx.MockTransport(handle)


def detection(*, source="live", duration=4000, verdict="synthetic", confidence=.95):
    analysis = build_analysis([{"stream_id": "MZ" + "4" * 32, "start_ms": 0,
                                "end_ms": duration, "verdict": verdict,
                                "confidence": confidence}], source=source, complete=False)
    return {"provider": "modulate", "status": "analyzing", "analysis": analysis}


def harness(tmp_path, *, enabled=True, voice=True, provider=None):
    h = Harness(tmp_path, voice=voice, provider=provider or DetectionProvider())
    settings = replace(SETTINGS, automatic_takeover_enabled=enabled, operator_inbound_enabled=True,
                       agent_management_enabled=True, workspace_storage_dir=str(tmp_path / "workspace"),
                       media_capture_enabled=True, media_storage_dir=str(tmp_path / "captures"),
                       transcription_enabled=True, deepgram_api_key="test-deepgram",
                       transcript_storage_dir=str(tmp_path / "transcripts"),
                       modulate_detection_enabled=True, modulate_api_key="test-modulate",
                       detection_storage_dir=str(tmp_path / "detection"))
    h.controller.settings = h.store.settings = settings
    h.lookups = []

    async def snapshot(kind):
        h.lookups.append(kind)
        return SNAPSHOT

    h.controller._internal_snapshot = snapshot
    return h


async def settle(h):
    # Allow scheduled control work to start before checking its task count.
    await asyncio.sleep(0)
    await until(lambda: h.store.pending_count == 0 and not h.controller.playing(h.session.id))


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


def test_detection_notice_is_owner_only_and_caller_still_receives_disclosure(tmp_path):
    async def run():
        h = harness(tmp_path)
        s = await h.joined()
        h.controller.on_detection(s.id, detection())
        await h.complete()
        assert len(h.owner.frames(0x31)) == 24
        assert not h.remote.frames(0x31)
        assert len(h.remote.frames(0x10)) == 3
        assert not h.owner.frames(0x10)
        assert h.remote.frames(0x2A) and h.owner.frames(0x2A)
        assert not any(bytes(args[1])[0] == 0x31 for args in h.output)
        assert not any(OWNER_NOTICE in args[1] for args, _ in h.delivered)
        await h.close()

    asyncio.run(run())


def test_private_notice_acknowledgement_precedes_shared_agent_speech(tmp_path):
    async def run():
        h = harness(tmp_path)
        s = await h.joined()
        h.owner.acknowledge = False
        h.controller.on_detection(s.id, detection())
        await until(lambda: bool(h.owner.marks()))
        assert h.remote.frames(0x10) and h.owner.frames(0x31)
        assert not h.remote.frames(0x2A) and not h.owner.frames(0x2A)
        # Keep the private notice intelligible without dropping caller input
        # from recording/transcription/detection while its monitor is paused.
        inputs = []
        h.controller.audio_sink = lambda *args: inputs.append(args)
        assert not h.router.forward(REMOTE, OWNER_FRAME)
        assert inputs[-1][1] == REMOTE
        h.owner.acknowledge = True
        await h.controller.mark(s.id, OWNER, h.owner.marks()[0], "played")
        await h.complete()
        assert not h.router.owner_notice
        assert h.router.forward(REMOTE, OWNER_FRAME)
        await h.close()

    asyncio.run(run())


@pytest.mark.parametrize("override", ["release", "manual"])
def test_owner_override_during_private_notice_cancels_cue_and_restores_monitor(tmp_path, override):
    async def run():
        h = harness(tmp_path)
        s = await h.joined()
        h.owner.acknowledge = False
        h.controller.on_detection(s.id, detection())
        await until(lambda: bool(h.owner.marks()))
        old_notice = h.owner.marks()[0]
        await h.press("#0" if override == "release" else "#2")
        await h.controller.mark(s.id, OWNER, old_notice, "played")
        if override == "manual":
            await h.complete()
            assert s.profile == "2"
        else:
            await settle(h)
            assert s.mode == HUMAN and h.router.forward(OWNER, OWNER_FRAME)
        assert not h.router.owner_notice
        assert h.router.forward(REMOTE, OWNER_FRAME)
        assert s.active and not h.dialer.ended
        await h.close()

    asyncio.run(run())


@pytest.mark.parametrize("case", ["recording", "combined", "too-short", "low-confidence", "human", "silence",
                                 "forged-alert", "weaker-threshold", "legacy-version", "wrong-track",
                                 "wrong-provider", "missing", "not-a-dict"])
def test_only_qualified_live_caller_evidence_can_activate(tmp_path, case):
    async def run():
        h = harness(tmp_path)
        s = await h.joined()
        if case in {"recording", "combined"}:
            value = detection(source=case)
        elif case == "too-short":
            value = detection(duration=3999)
        elif case == "low-confidence":
            value = detection(confidence=.79)
        elif case in {"human", "silence"}:
            value = detection(verdict="non-synthetic" if case == "human" else "no-content")
        elif case == "forged-alert":
            value = detection(verdict="non-synthetic")
            value["analysis"]["alert"] = "ai_detected"
        elif case in {"weaker-threshold", "legacy-version"}:
            value = detection()
            value["analysis"] = build_analysis(value["analysis"]["windows"], source="live",
                                                 min_confidence=.5 if case == "weaker-threshold" else .8,
                                                 version=2 if case == "legacy-version" else 3)
        elif case == "wrong-track":
            value = detection()
            value["analysis"]["track"] = "outbound"
        elif case == "wrong-provider":
            value = detection()
            value["provider"] = "transcript"
        else:
            value = {} if case == "missing" else None
        h.controller.on_detection(s.id, value)
        await settle(h)
        assert s.mode == HUMAN
        assert h.lookups == [] and h.provider.requests == []
        assert h.router.forward(OWNER, OWNER_FRAME)
        await h.close()

    asyncio.run(run())


@pytest.mark.parametrize("unavailable", ["disabled", "voice", "ended", "unknown"])
def test_detection_cannot_activate_an_unavailable_call(tmp_path, unavailable):
    async def run():
        h = harness(tmp_path, enabled=unavailable != "disabled", voice=unavailable != "voice")
        s = await h.joined()
        if unavailable == "ended":
            await h.controller.end(s.id, "caller-ended")
        h.controller.on_detection("unknown-session" if unavailable == "unknown" else s.id, detection())
        await settle(h)
        assert h.lookups == [] and h.provider.requests == []
        await h.close()

    asyncio.run(run())


def test_detection_waits_for_owner_connection_before_automatic_handoff(tmp_path):
    async def run():
        h = harness(tmp_path)
        s = await h.joined()
        s.phase = OWNER_RINGING
        h.controller.on_detection(s.id, detection())
        await settle(h)
        assert s.mode == HUMAN and not h.provider.requests
        s.phase = CONNECTED
        value = h.controller._maybe_auto_takeover(s.id)
        if inspect.isawaitable(value):
            await value
        await h.complete()
        assert h.lookups == ["ai-detected"]
        await h.close()

    asyncio.run(run())


def test_withdrawn_live_evidence_does_not_activate_after_owner_connects(tmp_path):
    async def run():
        h = harness(tmp_path)
        s = await h.joined()
        s.phase = OWNER_RINGING
        h.controller.on_detection(s.id, detection())
        # The detector can retract provisional evidence when a stream fails
        # validation. A pending handoff must use the latest public analysis.
        result = {"provider": "modulate", "status": "unknown",
                  "analysis": build_analysis([], source="live", complete=False)}
        h.controller.on_detection(s.id, result)
        s.phase = CONNECTED
        value = h.controller._maybe_auto_takeover(s.id)
        if inspect.isawaitable(value):
            await value
        await settle(h)
        assert s.mode == HUMAN and not h.lookups and not h.provider.requests
        await h.close()

    asyncio.run(run())


@pytest.mark.parametrize("checkpoint", ["snapshot", "profile"])
def test_withdrawal_during_automatic_activation_keeps_human_relay(tmp_path, checkpoint):
    async def run():
        h = harness(tmp_path)
        s = await h.joined()
        started, release = asyncio.Event(), asyncio.Event()
        if checkpoint == "snapshot":
            async def blocked_snapshot(kind):
                started.set()
                await release.wait()
                return SNAPSHOT
            h.controller._internal_snapshot = blocked_snapshot
        else:
            select = h.store.select_profile
            async def blocked_profile(*args, **kwargs):
                result = await select(*args, **kwargs)
                started.set()
                await release.wait()
                return result
            h.store.select_profile = blocked_profile
        h.controller.on_detection(s.id, detection())
        await started.wait()
        h.controller.on_detection(s.id, {"provider": "modulate", "status": "unknown",
            "analysis": build_analysis([], source="live", complete=False)})
        release.set()
        await settle(h)
        assert s.mode == HUMAN and h.router.forward(OWNER, OWNER_FRAME)
        assert s.active and not h.provider.requests and not h.dialer.ended
        assert not h.owner.frames(0x31) and not h.remote.frames(0x10)
        await h.close()

    asyncio.run(run())


@pytest.mark.parametrize("override", ["#0", "#2"])
def test_withdrawn_evidence_cleanup_does_not_override_newer_manual_selection(tmp_path, override):
    async def run():
        h = harness(tmp_path)
        s = await h.joined()
        started, release = asyncio.Event(), asyncio.Event()
        select = h.store.select_profile

        async def blocked_profile(session_id, profile, **kwargs):
            result = await select(session_id, profile, **kwargs)
            if profile == "aiwatch":
                started.set()
                await release.wait()
            return result

        h.store.select_profile = blocked_profile
        h.controller.on_detection(s.id, detection())
        await started.wait()
        h.controller.on_detection(s.id, {"provider": "modulate", "status": "unknown",
            "analysis": build_analysis([], source="live", complete=False)})
        await h.press(override)
        if override == "#2":
            await h.complete()
        release.set()
        await settle(h)
        assert s.mode == (AGENT if override == "#2" else HUMAN)
        if override == "#2":
            assert s.profile == "2"
        assert s.active and not h.dialer.ended and not h.owner.frames(0x31)
        await h.close()

    asyncio.run(run())


def test_zero_before_detection_suppresses_auto_but_manual_agent_still_works(tmp_path):
    async def run():
        h = harness(tmp_path)
        s = await h.joined()
        await h.press("#0")
        h.controller.on_detection(s.id, detection())
        await settle(h)
        assert s.mode == HUMAN and h.lookups == [] and h.provider.requests == []
        await h.press("#1")
        await h.complete()
        assert s.profile == "1"
        await h.close()

    asyncio.run(run())


@pytest.mark.parametrize("override", ["release", "manual", "end"])
def test_owner_override_during_internal_lookup_revokes_automatic_authority(tmp_path, override):
    async def run():
        h = harness(tmp_path)
        s = await h.joined()
        started, release = asyncio.Event(), asyncio.Event()

        async def blocked(kind):
            h.lookups.append(kind)
            started.set()
            await release.wait()
            return SNAPSHOT

        h.controller._internal_snapshot = blocked
        h.controller.on_detection(s.id, detection())
        await started.wait()
        if override == "release":
            await h.press("#0")
        elif override == "manual":
            await h.press("#2")
            await h.complete()
        else:
            await h.controller.end(s.id, "caller-ended")
        release.set()
        await settle(h)
        request_count = len(h.provider.requests)
        h.controller.on_detection(s.id, detection(duration=8000))
        await settle(h)
        assert h.lookups == ["ai-detected"]
        assert len(h.provider.requests) == request_count
        assert not h.owner.frames(0x31)
        assert not any("internalvoice" in url for url, _ in h.provider.requests)
        if override == "manual":
            assert s.mode == AGENT and s.profile == "2"
        elif override == "release":
            assert s.mode == HUMAN and s.active and h.router.forward(OWNER, OWNER_FRAME)
        else:
            assert not s.active and s.ended_reason == "caller-ended"
        await h.close()

    asyncio.run(run())


def test_existing_manual_agent_cannot_be_replaced_by_detection(tmp_path):
    async def run():
        h = harness(tmp_path)
        s = await h.joined()
        await h.press("#2")
        await h.complete()
        epoch = s.reply_epoch
        h.controller.on_detection(s.id, detection())
        await settle(h)
        assert h.lookups == [] and s.profile == "2" and s.reply_epoch == epoch
        await h.press("#0")
        h.controller.on_detection(s.id, detection(duration=9000))
        await settle(h)
        assert h.lookups == [] and s.mode == HUMAN
        await h.close()

    asyncio.run(run())


def test_failed_automatic_provider_returns_to_human_without_retry_loop(tmp_path):
    async def run():
        h = harness(tmp_path, provider=DetectionProvider(status=503))
        s = await h.joined()
        h.controller.on_detection(s.id, detection())
        await until(lambda: bool(h.provider.requests))
        await settle(h)
        request_count = len(h.provider.requests)
        assert s.mode == HUMAN and h.router.forward(OWNER, OWNER_FRAME)
        assert not h.dialer.ended
        for _ in range(5):
            h.controller.on_detection(s.id, detection(duration=8000))
        await settle(h)
        assert h.lookups == ["ai-detected"] and len(h.provider.requests) == request_count
        await h.close()

    asyncio.run(run())
