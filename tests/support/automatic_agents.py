"""Shared fixtures and fakes for focused integration checks."""

import asyncio


from copy import deepcopy


from dataclasses import replace


import json


import httpx


from agent_registry.store import AgentSnapshot


from operator_service.runtime import ANNOUNCEMENT


from operator_service.sessions import OWNER, REMOTE


from partner_detection.analysis import build_analysis


from support.operator_keypad import Harness, Provider, SETTINGS, until


from voice_stack.audio import FRAME_BYTES


OWNER_NOTICE = "AI Detected, deploying voice agent"


PRIVATE_PROMPT = "Private detection personality: end this conversation after three brief replies."


SNAPSHOT = AgentSnapshot("agent-internal-ai-detected", "AI Detection Agent", PRIVATE_PROMPT,
                         1, "owner-profile", "internalvoice", None)


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
