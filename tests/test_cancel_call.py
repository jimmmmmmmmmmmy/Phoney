"""T-Mobile cancel path: allowlist, DTMF on the existing call, local providers."""

import asyncio
import json
import uuid
import wave
from io import BytesIO

import httpx

from config import Settings
from operator_service.routes import OperatorController
from operator_service.sessions import AGENT, CONNECTED, HUMAN, REMOTE
from operator_service.sessions import OperatorSessions
from voice_stack.agent import ReplyCommandBuffer
from voice_stack.cancel_agent import (CANCEL_GOAL_KEY, TMOBILE_CS_E164, cancel_prompt)
from voice_stack.ivr import PlaybookEntry, default_playbook, suggest_dtmf, validate_dtmf
from voice_stack.local_providers import audio_to_ulaw, iter_reply_events, open_speech_session
from voice_stack.settings import VoiceSettings

from scripts.cancel_tmobile import describe_plan, live_call_block


REMOTE_SID = "CA" + "2" * 32


def _settings(**changes):
    base = dict(
        account_sid="AC" + "a" * 32, auth_token="test-token",
        public_base_url="https://operator.example", twilio_number="+12025550102",
        owner_number="+12025550101", allowed_destinations=(TMOBILE_CS_E164,),
        operator_admin_token="operator-admin-token-32-characters-long",
        voice_agent_enabled=True)
    base.update(changes)
    return Settings(**base)


class _Dialer:
    def __init__(self, fail=None):
        self.updated = []
        self.created = []
        self.fail = fail

    async def replace_twiml(self, sid, twiml):
        if self.fail:
            raise self.fail
        self.updated.append((sid, twiml))

    async def create_leg(self, **kwargs):
        self.created.append(kwargs)
        raise AssertionError("DTMF must not place a call")


def test_cancel_brief_allowlist_and_playbook():
    prompt = cancel_prompt(environ={})
    assert "252027408" in prompt
    assert "773-988-2659" in prompt and "312-545-2413" in prompt
    assert "Beata" in prompt
    assert "need the account holder" in prompt
    assert "PIN 1234" not in prompt and "SSN last four 1234" not in prompt
    assert len(CANCEL_GOAL_KEY) < 300
    store = OperatorSessions(_settings())
    assert store.destination_allowed(TMOBILE_CS_E164)
    blocked = OperatorSessions(_settings(allowed_destinations=()))
    assert not blocked.destination_allowed(TMOBILE_CS_E164)
    assert suggest_dtmf("Press 0 to speak to a representative.", default_playbook()) == "0"
    assert suggest_dtmf("Press zero for a representative.", default_playbook()) == "0"
    assert suggest_dtmf("Press 1 to cancel a line.", default_playbook()) is None
    assert suggest_dtmf("Press 2 for billing.", default_playbook()) is None
    override = (PlaybookEntry("known", ("to cancel one line press 4",), auto=True, digits="4", priority=0),)
    assert suggest_dtmf("To cancel one line press 4 now.", override) == "4"
    plan = describe_plan(environ={
        "ALLOWED_DESTINATIONS": TMOBILE_CS_E164,
        "VOICE_AGENT_ENABLED": "false",
    })
    assert "no call placed" in plan and TMOBILE_CS_E164 in plan
    assert live_call_block("http://127.0.0.1:8000", {"PHONEY_ALLOW_LIVE_CALL": "1"}) is None
    assert live_call_block("http://127.0.0.1:8000", {"CI": "1", "PHONEY_ALLOW_LIVE_CALL": "1"})
    assert live_call_block("https://example.com", {"PHONEY_ALLOW_LIVE_CALL": "1"})
    assert live_call_block("http://127.0.0.1:8000", {})


def test_dtmf_updates_the_existing_twilio_call():
    async def run():
        settings = _settings()
        store = OperatorSessions(settings)
        dialer = _Dialer()
        controller = OperatorController(settings, store, dialer)
        session, _ = await store.reserve_outbound(
            TMOBILE_CS_E164, CANCEL_GOAL_KEY, str(uuid.uuid4()))
        await store.mark_connected(session.id)
        leg = session.legs[REMOTE]
        leg.call_sid = REMOTE_SID
        leg.state = "started"
        leg.attached = True
        await controller.send_ivr(session.id, "1w0")
        assert dialer.created == []
        assert dialer.updated[0][0] == REMOTE_SID
        assert 'digits="1w0"' in dialer.updated[0][1]
        assert session.legs[REMOTE].call_sid == REMOTE_SID
        assert session.legs[REMOTE].generation == 2
        assert session.remote_dtmf_pending
        session.mode = AGENT
        await controller.stream_stopped(session.id, REMOTE, "socket-disconnected")
        assert session.mode == AGENT
        try:
            validate_dtmf("12x")
        except ValueError:
            pass
        else:
            raise AssertionError("invalid digits were accepted")
        await store.end(session.id, "test-end")

    asyncio.run(run())


def test_dtmf_failure_restores_the_remote_stream():
    async def run():
        settings = _settings()
        store = OperatorSessions(settings)
        dialer = _Dialer(fail=RuntimeError("rejected"))
        controller = OperatorController(settings, store, dialer)
        session, _ = await store.reserve_outbound(
            TMOBILE_CS_E164, CANCEL_GOAL_KEY, str(uuid.uuid4()))
        await store.mark_connected(session.id)
        leg = session.legs[REMOTE]
        leg.call_sid = REMOTE_SID
        leg.state = "started"
        leg.attached = True
        try:
            await controller.digit_sender(session.id, "0")
        except Exception as exc:
            assert type(exc).__name__ == "OperatorRejected"
        else:
            raise AssertionError("failed DTMF was treated as sent")
        assert dialer.created == [] and dialer.updated == []
        assert leg.generation == 1 and leg.attached and not session.remote_dtmf_pending
        assert session.phase == CONNECTED
        await store.end(session.id, "test-end")

    asyncio.run(run())


def test_reply_buffer_hides_dtmf_and_keeps_end_call():
    hidden = ReplyCommandBuffer()
    spoken = hidden.feed("OK.\n[/DTMF 1w0]\n")
    hidden.finish()
    assert "DTMF" not in spoken and hidden.dtmf == ["1w0"] and not hidden.end_call
    ending = ReplyCommandBuffer()
    farewell = ending.feed("Goodbye.\n[/END CALL]\n")
    ending.finish()
    assert ending.end_call and "[/END CALL]" not in farewell and ending.dtmf == []


def _voice(tmp_path, **changes):
    fields = dict(
        enabled=True, output_dir=str(tmp_path),
        tts_provider="phonon", phonon_base_url="http://127.0.0.1:8091",
        phonon_model="phonon-40m", phonon_voice="phonon-40m",
        llm_provider="nemotron", nemotron_base_url="http://127.0.0.1:8088",
        nemotron_model="nemotron-en")
    fields.update(changes)
    return VoiceSettings(**fields)


def test_phonon_and_nemotron_are_primary_when_enabled(tmp_path):
    voice = _voice(tmp_path)
    assert voice.llm_configured and voice.tts_configured
    assert not voice.gemini_api_key and not voice.elevenlabs_api_key
    ulaw = bytes([0xFF, 0x7F]) * 80
    seen = []

    def handler(request: httpx.Request):
        seen.append(request.url.path)
        if request.url.path in {"/health", "/v1/models"}:
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/v1/audio/speech":
            body = json.loads(request.content)
            assert body["response_format"] == "ulaw_8000"
            assert body["model"] == "phonon-40m"
            return httpx.Response(200, content=ulaw, headers={"content-type": "audio/basic"})
        if request.url.path == "/v1/chat/completions":
            chunk = {"choices": [{"delta": {"content": "Two lines only."}}]}
            payload = "data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n"
            return httpx.Response(200, content=payload, headers={"content-type": "text/event-stream"})
        return httpx.Response(404)

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            async with open_speech_session(http, voice, "phonon-40m") as session:
                audio = b"".join([chunk async for chunk in session.speech_bytes("Hello.")])
            events = [event async for event in iter_reply_events(
                http, voice, "system", [{"role": "user", "parts": [{"text": "menu"}]}],
                model="gemini-3.5-flash-lite", max_output_tokens=256, timeout=5,
                first_text_timeout=2, retry_first_text_timeout=False, trace=False)]
        assert audio == ulaw
        assert events == [{"kind": "text", "text": "Two lines only."}]
        assert not any("generativelanguage" in path or "elevenlabs" in path for path in seen)

    asyncio.run(run())
    pcm = BytesIO()
    with wave.open(pcm, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 160)
    converted = audio_to_ulaw(pcm.getvalue(), "audio/wav")
    assert converted and len(converted) == 160


def test_unhealthy_local_providers_fall_back_to_cloud(tmp_path):
    voice = _voice(
        tmp_path, gemini_api_key="gemini-test", elevenlabs_api_key="eleven-test",
        elevenlabs_voice_id="ownerVoice1", elevenlabs_model="eleven_flash_v2_5")
    hosts = []

    def handler(request: httpx.Request):
        hosts.append(request.url.host)
        if request.url.host == "127.0.0.1":
            return httpx.Response(503, text="down")
        if "elevenlabs" in request.url.host:
            return httpx.Response(200, content=b"\xff" * 160)
        if "generativelanguage" in request.url.host:
            event = {"candidates": [{"content": {"parts": [{"text": "Cloud fallback."}]},
                                     "finishReason": "STOP"}]}
            return httpx.Response(200, content="data: " + json.dumps(event) + "\n\n")
        return httpx.Response(404)

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            async with open_speech_session(http, voice, "phonon-40m") as session:
                audio = b"".join([chunk async for chunk in session.speech_bytes("Hello.")])
            events = [event async for event in iter_reply_events(
                http, voice, "system", [{"role": "user", "parts": [{"text": "hello"}]}],
                model="gemini-3.5-flash-lite", max_output_tokens=256, timeout=5,
                first_text_timeout=2, retry_first_text_timeout=False, trace=False)]
        assert audio == b"\xff" * 160
        assert any(event.get("text") == "Cloud fallback." for event in events)
        assert "api.elevenlabs.io" in hosts and "generativelanguage.googleapis.com" in hosts

    asyncio.run(run())
