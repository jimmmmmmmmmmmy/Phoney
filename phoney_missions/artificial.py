"""In-process phone mission. The dialer is a fake. Twilio is never constructed.

Scripted remote utterances stand in for Deepgram finals. Nemotron and Phonon
are httpx mocks. DTMF updates the fake remote call instead of placing another.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from config import Settings
from operator_service.routes import OperatorController
from operator_service.sessions import REMOTE, OperatorSessions
from voice_stack.settings import VoiceSettings

from .brevity import assert_spoken_budget
from .schema import load_pack

TMOBILE = "+18009378997"
REMOTE_SID = "CA" + "2" * 32
REMOTE_STREAM = "MZ" + "4" * 32

CANCEL_TURNS = (
    "Press 0 to speak to a representative.",
    "How can I help you?",
    "We can offer a one-time $50 credit.",
    "The Apple Watch SE line 773-988-2659 is cancelled. Your confirmation number is AB12 and the effective date is 2026-11-15.",
    "The BYOD line 312-545-2413 is cancelled. Confirmation number CD34 effective date 2026-11-15.",
)
CANCEL_REPLIES = (
    "I'd like to cancel the line ending in 2659.",
    "I'm not interested in offers.",
    "Confirmed. Line 773-988-2659, confirmation AB12, effective 2026-11-15.\n"
    "[/NOTE confirm:7739882659:AB12:2026-11-15]",
    "Confirmed. Line 312-545-2413, confirmation CD34, effective 2026-11-15.\n"
    "[/NOTE confirm:3125452413:CD34:2026-11-15]\nThank you, goodbye.\n[/END CALL]",
)
RETENTION_TURNS = (
    "Press 0 to speak to a representative.",
    "I can do $20 per month for 12 months if you keep the lines.",
)
RETENTION_REPLIES = (
    "I'll accept the monthly credit instead of canceling.\n"
    "[/NOTE retention:2000:12]\nThank you, goodbye.\n[/END CALL]",
)


def deepgram_final(payload: dict) -> str:
    """Read one Deepgram Results event the way the live client does."""
    if payload.get("type") != "Results" or payload.get("is_final") is not True:
        raise ValueError("STT adapter expected a final Deepgram result.")
    if payload.get("speech_final") is not True:
        raise ValueError("STT adapter expected speech_final.")
    alternatives = payload.get("channel", {}).get("alternatives")
    if not isinstance(alternatives, list) or not alternatives:
        raise ValueError("STT adapter expected alternatives.")
    text = str(alternatives[0].get("transcript", "")).strip()
    if not text:
        raise ValueError("STT adapter expected a transcript.")
    return text


def deepgram_payload(text: str) -> dict:
    return {
        "type": "Results",
        "is_final": True,
        "speech_final": True,
        "channel": {"alternatives": [{"transcript": text, "confidence": 0.99}]},
    }


class FakeDialer:
    """Records legs. This is not TwilioLegs and does not open a network call."""

    def __init__(self):
        self.created = []
        self.updated = []
        self.ended = []

    async def create_leg(self, *, to, twiml, status_callback, caller_number="", call_token=""):
        self.created.append({"to": to, "twiml": twiml})
        return "CA" + f"{len(self.created) % 10}" * 32

    async def replace_twiml(self, sid, twiml):
        self.updated.append((sid, twiml))

    async def end_call(self, sid):
        self.ended.append(sid)

    async def read_status(self, sid):
        return {"status": "in-progress", "duration_seconds": 1}


class ScriptedMedia:
    """Phonon and Nemotron over a mock transport. Hosts stay on loopback."""

    def __init__(self, replies: tuple[str, ...]):
        self.replies = list(replies)
        self.spoken: list[str] = []
        self.hosts: list[str] = []
        self.llm_calls = 0

    def transport(self):
        media = self

        def handle(request: httpx.Request):
            media.hosts.append(request.url.host)
            if request.url.path in {"/health", "/v1/models"}:
                return httpx.Response(200, json={"ok": True})
            if request.url.path == "/v1/chat/completions":
                if media.llm_calls >= len(media.replies):
                    return httpx.Response(500, text="no scripted reply")
                reply = media.replies[media.llm_calls]
                media.llm_calls += 1
                chunk = {"choices": [{"delta": {"content": reply}}]}
                body = "data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n"
                return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
            if request.url.path == "/v1/audio/speech":
                text = json.loads(request.content)["input"]
                media.spoken.append(text)
                return httpx.Response(200, content=b"\xff" * 160, headers={"content-type": "audio/basic"})
            return httpx.Response(404)

        return httpx.MockTransport(handle)


class MediaSocket:
    def __init__(self, controller, session_id, role):
        self.controller = controller
        self.session_id = session_id
        self.role = role
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)
        if message.get("event") == "mark":
            await self.controller.mark(
                self.session_id, self.role, message["mark"]["name"], "played")


@dataclass
class ArtificialResult:
    mission: str
    unattended: bool
    destination: str
    owner_dialed: bool
    press_1: bool
    dtmf_updates: int
    calls_created: int
    spoken: list[str] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)
    ended_reason: str = ""
    brief_has_cancel_facts: bool = False
    hosts: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return "\n".join([
            f"artificial {self.mission} (no Twilio call placed)",
            f"unattended: {str(self.unattended).lower()}",
            f"destination: {self.destination}",
            f"owner dialed: {str(self.owner_dialed).lower()}",
            f"press 1 in twiml: {str(self.press_1).lower()}",
            f"calls created: {self.calls_created}",
            f"dtmf updates: {self.dtmf_updates}",
            f"spoken turns: {len(self.spoken)}",
            f"notes: {len(self.notes)}",
            f"ended: {self.ended_reason}",
            f"cancel brief: {str(self.brief_has_cancel_facts).lower()}",
        ])


def _settings(tmp: Path, *, unattended: bool) -> Settings:
    return Settings(
        account_sid="AC" + "a" * 32,
        auth_token="test-token",
        public_base_url="https://operator.example",
        twilio_number="+12025550102",
        owner_number="+12025550101",
        allowed_destinations=(TMOBILE,),
        operator_admin_token="operator-admin-token-32-characters-long",
        voice_agent_enabled=True,
        phoney_unattended=unattended,
        phoney_allow_live_call=unattended,
    )


def _voice(tmp: Path) -> VoiceSettings:
    return VoiceSettings(
        enabled=True,
        output_dir=str(tmp),
        tts_provider="phonon",
        phonon_base_url="http://127.0.0.1:8091",
        phonon_model="phonon-40m",
        phonon_voice="phonon-40m",
        llm_provider="nemotron",
        nemotron_base_url="http://127.0.0.1:8088",
        nemotron_model="nemotron-en",
        elevenlabs_model="eleven_flash_v2_5",
    )


async def _until(check, timeout=8):
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


async def _dial(mission: str, tmp: Path, *, unattended: bool):
    pack = load_pack(mission)
    settings = _settings(tmp, unattended=unattended)
    store = OperatorSessions(settings)
    dialer = FakeDialer()
    media = ScriptedMedia(())
    controller = OperatorController(
        settings, store, dialer, voice=_voice(tmp), provider_transport=media.transport())
    await controller.start_outbound(pack.destination_e164, pack.goal_key, str(uuid.uuid4()))
    await store.wait_idle()
    return controller, store, dialer


async def run_artificial_call(mission: str = "tmobile-cancel", *, scenario: str = "cancel",
                              unattended: bool = True, output_dir: str | None = None) -> ArtificialResult:
    """Run one scripted mission. ``unattended`` is in-memory only; nothing dials out."""
    if scenario == "retention":
        turns, replies = RETENTION_TURNS, RETENTION_REPLIES
    else:
        turns, replies = CANCEL_TURNS, CANCEL_REPLIES
    tmp = Path(output_dir) if output_dir else Path(os.environ.get("TMPDIR", "/tmp")) / "phoney-artificial"
    tmp.mkdir(parents=True, exist_ok=True)
    saved = {name: os.environ.get(name) for name in ("CI", "GITHUB_ACTIONS")}
    if unattended:
        os.environ.pop("CI", None)
        os.environ.pop("GITHUB_ACTIONS", None)
    controller = store = None
    try:
        controller, store, dialer = await _dial(mission, tmp, unattended=unattended)
        session = next(iter(store.sessions.values()))
        press_1 = any("Press 1" in item["twiml"] for item in dialer.created)
        owner_dialed = any(item["to"] == "+12025550101" for item in dialer.created)
        if not unattended:
            return ArtificialResult(
                mission=mission, unattended=False, destination=session.to,
                owner_dialed=owner_dialed, press_1=press_1,
                dtmf_updates=len(dialer.updated), calls_created=len(dialer.created),
            )
        media = ScriptedMedia(replies)
        controller.provider_transport = media.transport()
        controller._provider_clients.pop(session.id, None)
        leg = session.legs[REMOTE]
        await store.bind_stream(session.id, REMOTE, {
            "accountSid": controller.settings.account_sid,
            "callSid": leg.call_sid,
            "streamSid": REMOTE_STREAM,
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
            "customParameters": {"generation": str(leg.generation), "token": leg.token},
        })
        socket = MediaSocket(controller, session.id, REMOTE)
        router = controller.router(session.id)
        router.channels[REMOTE].attach(socket, REMOTE_STREAM, leg.generation, leg.counters)
        router.generations[REMOTE] = leg.generation
        await controller.stream_started(session.id, REMOTE, REMOTE_STREAM)
        await _until(lambda: session.mode == "agent" and session.agent_snapshot is not None)
        for utterance in turns:
            heard = deepgram_final(deepgram_payload(utterance))
            await controller.transcript(session.id, REMOTE, heard, final=True, speech_final=True)
            await _until(lambda: not controller.playing(session.id) and session.id not in controller._reply_timers)
            await store.wait_idle()
            if not session.active:
                break
        brief = session.agent_snapshot.prompt if session.agent_snapshot else ""
        for spoken in media.spoken:
            assert_spoken_budget(spoken)
        facts = ("252027408" in brief and "773-988-2659" in brief and "312-545-2413" in brief
                 and "Beata" in brief and "No filler." in brief and "No empathy padding." in brief)
        return ArtificialResult(
            mission=mission,
            unattended=bool(session.unattended),
            destination=session.to,
            owner_dialed=owner_dialed,
            press_1=press_1,
            dtmf_updates=len(dialer.updated),
            calls_created=len(dialer.created),
            spoken=list(media.spoken),
            notes=list(session.mission_notes),
            ended_reason=session.ended_reason,
            brief_has_cancel_facts=facts,
            hosts=list(media.hosts),
        )
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if store is not None:
            await store.close()
