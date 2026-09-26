"""Keypad takeover over the real controller: fake sockets, fake provider, no phone.

The controller is driven exactly as the media reader drives it, so these tests
cover the doc's Stage 2 list: remote-key isolation, incomplete prefixes, repeated
commands, stale-audio cancellation, and a provider failure that still returns
control to the owner.
"""

import asyncio
import base64
import json
import time
import uuid

import httpx

from config import Settings
from operator_service import OperatorSessions
from operator_service.codecs import is_silence
from operator_service.controls import Keypad, load_profiles
from operator_service.routes import OperatorController
from operator_service.sessions import AGENT, HUMAN, OWNER, REMOTE
from voice_stack.audio import FRAME_BYTES
from voice_stack.settings import VoiceSettings

ACCOUNT = "AC" + "a" * 32
OWNER_NUMBER = "+12025550101"
CALLEE = "+12025550102"
DESTINATION = "+12025550103"
ADMIN_TOKEN = "operator-admin-token-32-characters-long"
OWNER_SID = "CA" + "1" * 32
REMOTE_SID = "CA" + "2" * 32
OWNER_STREAM = "MZ" + "3" * 32
REMOTE_STREAM = "MZ" + "4" * 32
OWNER_FRAME = bytes([0x11]) * FRAME_BYTES
SETTINGS = Settings(
    account_sid=ACCOUNT, auth_token="operator-keypad-test-token",
    public_base_url="https://operator.example", twilio_number=CALLEE,
    owner_number=OWNER_NUMBER, allowed_destinations=(DESTINATION,),
    operator_admin_token=ADMIN_TOKEN, max_call_seconds=1800,
)
PROFILES = load_profiles()
CLIP_FRAMES = 3
CLIP = bytes([0x2A]) * (FRAME_BYTES * CLIP_FRAMES)
OTHER_CLIP = bytes([0x5A]) * (FRAME_BYTES * CLIP_FRAMES)
# The documented preparation deadline is three seconds; tests shorten it so a
# stalled provider does not add three seconds of real sleeping to every run.
PREPARE_DEADLINE = 0.05


def frames(audio):
    return [audio[index:index + FRAME_BYTES] for index in range(0, len(audio), FRAME_BYTES)]


class Provider:
    """A fake ElevenLabs surface: one marker byte per phrase, or a failure."""

    def __init__(self, frames=CLIP_FRAMES, status=200, delay=0.0):
        self.frames = frames
        self.status = status
        self.delay = delay
        self.requests = []

    def wave(self, text):
        """Audio that identifies which phrase was asked for."""
        marker = 0x2A if text == PROFILES["1"].demo_phrase else 0x5A
        return bytes([marker]) * (FRAME_BYTES * self.frames)

    def transport(self):
        provider = self

        class Delayed(httpx.MockTransport):
            async def handle_async_request(self, request):
                if provider.delay:
                    await asyncio.sleep(provider.delay)
                return await super().handle_async_request(request)

        def handle(request):
            body = json.loads(request.content.decode("utf-8"))
            provider.requests.append(body)
            if provider.status != 200:
                return httpx.Response(provider.status, text="provider unavailable")
            return httpx.Response(200, content=provider.wave(body["text"]))

        return Delayed(handle)


class Dialer:
    """A fake Twilio REST surface: records hang-ups, never places a call."""

    def __init__(self):
        self.ended = []

    async def create_leg(self, *, to, twiml, status_callback):
        return "CA" + format(len(self.ended) + 8, "032x")

    async def end_call(self, call_sid):
        self.ended.append(call_sid)


class FakeSocket:
    """One output channel's sink: it only records what the writer sent."""

    def __init__(self):
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)


class BlockedSocket(FakeSocket):
    """A socket whose writes never land, so its writer cannot drain the queue."""

    async def send_json(self, message):
        await asyncio.Event().wait()


class Harness:
    """A store, a controller, and the fake provider one takeover needs."""

    def __init__(self, tmp_path, *, provider=None, voice=True):
        self.store = OperatorSessions(SETTINGS)
        self.provider = provider if provider is not None else Provider()
        self.voice = VoiceSettings(
            enabled=True, gemini_api_key="gemini-key", elevenlabs_api_key="elevenlabs-key",
            elevenlabs_voice_id="voiceid123", output_dir=str(tmp_path / "voice"),
        ) if voice else None
        self.controller = OperatorController(
            SETTINGS, self.store, Dialer(), voice=self.voice,
            keypad_factory=lambda: Keypad(prefix_seconds=0.05, coalesce_seconds=0.05))
        if self.controller.clips is not None:
            self.controller.clips.transport = self.provider.transport()
        self.digits = []
        self.owner = FakeSocket()
        self.remote = FakeSocket()

    async def joined(self):
        """A connected session: both legs bound, both output channels attached."""
        session, _ = await self.store.reserve_outbound(DESTINATION, "Ask for an itemised quote",
                                                       str(uuid.uuid4()))
        await self.store.bind_call_sid(session.id, OWNER, OWNER_SID)
        await self.store.bind_call_sid(session.id, REMOTE, REMOTE_SID)
        await self.store.mark_owner_prompt(session.id)
        await self.store.begin_remote_dial(session.id)
        await self.controller.stream_started(session.id, REMOTE, REMOTE_STREAM)
        router = self.controller.router(session.id)
        router.channels[OWNER].attach(self.owner, OWNER_STREAM, session.legs[OWNER].generation,
                                      session.legs[OWNER].counters)
        router.channels[REMOTE].attach(self.remote, REMOTE_STREAM, session.legs[REMOTE].generation,
                                       session.legs[REMOTE].counters)
        return session

    async def press(self, session_id, keys):
        """Send one key at a time, exactly as the media reader does."""
        for key in keys:
            await self.controller.dtmf(session_id, key)
            await asyncio.sleep(0)

    def record_digits(self):
        async def recorder(session_id, digits):
            self.digits.append(digits)

        self.controller.digit_sender = recorder

    @staticmethod
    def payloads(socket):
        return [base64.b64decode(message["media"]["payload"])
                for message in socket.sent if message["event"] == "media"]

    def voiced(self, socket):
        """Every frame that is not filler silence: the audio actually spoken."""
        return [frame for frame in self.payloads(socket) if not is_silence(frame)]

    @staticmethod
    def events(socket):
        return [message["event"] for message in socket.sent]

    @staticmethod
    def marks(socket):
        return [message["mark"]["name"] for message in socket.sent
                if message["event"] == "mark"]

    @staticmethod
    def timeline(socket):
        """Every message this socket received, in order, as (event, value)."""
        ordered = []
        for message in socket.sent:
            if message["event"] == "media":
                ordered.append(("media", base64.b64decode(message["media"]["payload"])))
            elif message["event"] == "mark":
                ordered.append(("mark", message["mark"]["name"]))
            else:
                ordered.append((message["event"], ""))
        return ordered

    async def settle(self, seconds=0.3):
        """Let the writer drain: its clock paces one frame every 20 ms."""
        await asyncio.sleep(seconds)


async def until(predicate, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


def run(scenario):
    return asyncio.run(scenario())


def dtmf_event(digit, *, role=OWNER, sequence="3"):
    stream = OWNER_STREAM if role == OWNER else REMOTE_STREAM
    return {"event": "dtmf", "sequenceNumber": sequence, "streamSid": stream,
            "dtmf": {"track": "inbound_track", "digit": digit}}


# ------------------------------------------------------------------- takeover


def test_hash_one_substitutes_the_cloned_phrase(tmp_path, monkeypatch):
    monkeypatch.setattr("operator_service.routes.PREPARE_SECONDS", PREPARE_DEADLINE)

    async def scenario():
        harness = Harness(tmp_path)
        session = await harness.joined()
        await harness.press(session.id, "#1")
        assert session.mode == AGENT and session.profile == "1"
        assert session.reply_epoch == 1
        await until(lambda: harness.marks(harness.remote) == ["clip-1-1"])
        assert harness.voiced(harness.remote) == frames(CLIP)
        # The owner hears the same phrase through the monitor mix.
        assert harness.voiced(harness.owner) == frames(CLIP)
        # The phrase was rendered once, from the profile's own text, in the
        # enrolled voice and at the Twilio-ready model.
        assert len(harness.provider.requests) == 1
        request = harness.provider.requests[0]
        assert request["text"] == PROFILES["1"].demo_phrase
        assert request["model_id"] == harness.voice.elevenlabs_model
        assert session.legs[REMOTE].marks["clip-1-1"] == "pending"
        await harness.settle(0.1)

    run(scenario)


def test_the_same_shortcut_twice_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr("operator_service.routes.PREPARE_SECONDS", PREPARE_DEADLINE)

    async def scenario():
        harness = Harness(tmp_path)
        session = await harness.joined()
        await harness.press(session.id, "#1")
        await until(lambda: harness.marks(harness.remote) == ["clip-1-1"])
        await harness.settle(0.1)
        spoken = len(harness.voiced(harness.remote))
        await harness.press(session.id, "#1")
        await harness.settle(0.1)
        assert session.reply_epoch == 1 and session.profile == "1"
        assert len(harness.voiced(harness.remote)) == spoken
        assert len(harness.provider.requests) == 1

    run(scenario)


def test_a_new_profile_cancels_the_phrase_already_queued(tmp_path, monkeypatch):
    monkeypatch.setattr("operator_service.routes.PREPARE_SECONDS", PREPARE_DEADLINE)

    async def scenario():
        harness = Harness(tmp_path, provider=Provider(frames=40))
        session = await harness.joined()
        await harness.press(session.id, "#1")
        await until(lambda: len(harness.voiced(harness.remote)) >= 3)
        await harness.press(session.id, "#2")
        assert session.profile == "2" and session.reply_epoch == 2
        # A clear is emitted before the replacement frames, so the old reply is
        # cut where it is and nothing stale reaches the remote leg afterwards.
        await until(lambda: OTHER_CLIP[:FRAME_BYTES] in harness.payloads(harness.remote))
        timeline = harness.timeline(harness.remote)
        cut = max(index for index, (event, _) in enumerate(timeline) if event == "clear")
        before = [value for event, value in timeline[:cut] if event == "media"]
        after = [value for event, value in timeline[cut:] if event == "media"]
        assert frames(CLIP)[0] in before
        assert OTHER_CLIP[:FRAME_BYTES] not in before
        assert OTHER_CLIP[:FRAME_BYTES] in after
        await harness.settle(0.2)
        assert not harness.controller.playing(session.id)
        assert {entry["text"] for entry in harness.provider.requests} == {
            PROFILES["1"].demo_phrase, PROFILES["2"].demo_phrase}

    run(scenario)


def test_zero_interrupts_the_phrase_and_restores_the_microphone(tmp_path, monkeypatch):
    monkeypatch.setattr("operator_service.routes.PREPARE_SECONDS", PREPARE_DEADLINE)

    async def scenario():
        harness = Harness(tmp_path, provider=Provider(frames=40))
        session = await harness.joined()
        await harness.press(session.id, "#1")
        await until(lambda: len(harness.voiced(harness.remote)) >= 3)
        await harness.press(session.id, "#0")
        assert session.mode == HUMAN and session.reply_epoch == 2
        assert harness.controller.playing(session.id) is False
        # The writer sends a clear when it next wakes, on each direction.
        await until(lambda: "clear" in harness.events(harness.remote))
        await until(lambda: "clear" in harness.events(harness.owner))
        spoken = len(harness.voiced(harness.remote))
        await harness.settle(0.3)
        assert len(harness.voiced(harness.remote)) == spoken
        # The owner's microphone is live again, with no provider involved.
        router = harness.controller.routers[session.id]
        assert router.forward(OWNER, OWNER_FRAME) is True
        await until(lambda: OWNER_FRAME in harness.voiced(harness.remote))
        assert len(harness.provider.requests) == 1

    run(scenario)


def test_zero_returns_control_even_when_nothing_was_ever_spoken(tmp_path, monkeypatch):
    monkeypatch.setattr("operator_service.routes.PREPARE_SECONDS", PREPARE_DEADLINE)

    async def scenario():
        harness = Harness(tmp_path, voice=False)
        session = await harness.joined()
        await harness.press(session.id, "#1")
        await harness.press(session.id, "#0")
        assert session.mode == HUMAN and session.reply_epoch == 0
        assert harness.digits == []
        # With the voice layer off, a remote menu key is still local menu input.
        harness.record_digits()
        await harness.press(session.id, "7")
        await until(lambda: harness.digits == ["7"])

    run(scenario)


def test_a_mark_returned_after_a_clear_does_not_confirm_the_phrase(tmp_path, monkeypatch):
    monkeypatch.setattr("operator_service.routes.PREPARE_SECONDS", PREPARE_DEADLINE)

    async def scenario():
        harness = Harness(tmp_path)
        session = await harness.joined()
        await harness.press(session.id, "#1")
        await until(lambda: harness.marks(harness.remote) == ["clip-1-1"])
        await harness.press(session.id, "#0")
        # The mark had been queued but not confirmed when the clear happened.
        assert session.legs[REMOTE].marks["clip-1-1"] == "cleared"
        await harness.controller.mark(session.id, REMOTE, "clip-1-1", "played")
        assert session.legs[REMOTE].marks["clip-1-1"] == "played-after-clear"

    run(scenario)


def test_an_unavailable_provider_returns_control_to_the_owner(tmp_path, monkeypatch):
    monkeypatch.setattr("operator_service.routes.PREPARE_SECONDS", PREPARE_DEADLINE)

    async def scenario():
        harness = Harness(tmp_path, provider=Provider(status=503))
        session = await harness.joined()
        await harness.press(session.id, "#1")
        await until(lambda: session.mode == HUMAN and not harness.controller.playing(session.id))
        assert session.reply_epoch == 2          # one takeover, one hand-back
        assert harness.voiced(harness.remote) == []
        await harness.press(session.id, "#0")
        assert session.mode == HUMAN and session.reply_epoch == 2
        assert len(harness.provider.requests) >= 1

    run(scenario)


def test_a_stalled_provider_misses_the_preparation_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr("operator_service.routes.PREPARE_SECONDS", PREPARE_DEADLINE)

    async def scenario():
        harness = Harness(tmp_path, provider=Provider(delay=0.4))
        session = await harness.joined()
        started = time.monotonic()
        await harness.press(session.id, "#1")
        await until(lambda: session.mode == HUMAN and not harness.controller.playing(session.id))
        assert time.monotonic() - started < 0.3
        assert harness.voiced(harness.remote) == []

    run(scenario)


def test_a_remote_leg_without_a_stream_hands_control_back(tmp_path, monkeypatch):
    monkeypatch.setattr("operator_service.routes.PREPARE_SECONDS", PREPARE_DEADLINE)

    async def scenario():
        harness = Harness(tmp_path)
        session = await harness.joined()
        harness.controller.routers[session.id].channels[REMOTE].detach()
        await harness.press(session.id, "#1")
        await until(lambda: session.mode == HUMAN)
        assert session.reply_epoch == 2

    run(scenario)


def test_a_takeover_without_an_enrolled_voice_stays_human(tmp_path):
    async def scenario():
        harness = Harness(tmp_path, voice=False)
        session = await harness.joined()
        await harness.press(session.id, "#1")
        assert session.mode == HUMAN and session.reply_epoch == 0
        assert harness.provider.requests == []
        assert harness.controller.clips is None

    run(scenario)


def test_the_default_phrase_is_rendered_before_the_first_press(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        session = await harness.joined()
        await until(lambda: len(harness.provider.requests) == 1)
        assert harness.provider.requests[0]["text"] == PROFILES["1"].demo_phrase
        await until(lambda: not harness.controller.prefetch)
        cached = harness.controller.clips.path("1", PROFILES["1"].demo_phrase)
        assert cached.read_bytes() == CLIP
        await harness.press(session.id, "#1")
        await until(lambda: harness.marks(harness.remote) == ["clip-1-1"])
        assert len(harness.provider.requests) == 1        # served from the cache
        await harness.settle(0.1)

    run(scenario)


# ------------------------------------------------------ prefixes and isolation


def test_an_incomplete_prefix_expires_without_a_takeover(tmp_path, monkeypatch):
    monkeypatch.setattr("operator_service.routes.PREPARE_SECONDS", PREPARE_DEADLINE)

    async def scenario():
        harness = Harness(tmp_path)
        harness.record_digits()
        session = await harness.joined()
        await harness.press(session.id, "#")
        await asyncio.sleep(0.15)                 # the short prefix window expires
        assert session.mode == HUMAN and session.reply_epoch == 0
        await until(lambda: not harness.controller.prefetch)
        assert len(harness.provider.requests) == 1      # only the one warm-up phrase
        assert harness.controller.playing(session.id) is False
        # A digit after an expired prefix is menu input, never a shortcut.
        await harness.press(session.id, "1")
        await until(lambda: harness.digits == ["1"])
        assert session.mode == HUMAN and session.reply_epoch == 0

    run(scenario)


def test_a_menu_key_during_agent_mode_is_consumed_locally(tmp_path, monkeypatch):
    monkeypatch.setattr("operator_service.routes.PREPARE_SECONDS", PREPARE_DEADLINE)

    async def scenario():
        harness = Harness(tmp_path)
        harness.record_digits()
        session = await harness.joined()
        await harness.press(session.id, "#1")
        await until(lambda: harness.marks(harness.remote) == ["clip-1-1"])
        await harness.press(session.id, "5")
        await asyncio.sleep(0.1)
        assert harness.digits == []
        assert session.mode == AGENT and session.reply_epoch == 1
        await harness.settle(0.1)

    run(scenario)


def test_the_remote_keypad_cannot_change_a_profile(tmp_path, monkeypatch):
    monkeypatch.setattr("operator_service.routes.PREPARE_SECONDS", PREPARE_DEADLINE)

    async def scenario():
        harness = Harness(tmp_path)
        session = await harness.joined()
        router = harness.controller.routers[session.id]
        router.generations[OWNER] = session.legs[OWNER].generation
        router.generations[REMOTE] = session.legs[REMOTE].generation
        for digit in ("#", "1"):
            await router._queue_dtmf(REMOTE, session.legs[REMOTE],
                                     dtmf_event(digit, role=REMOTE, sequence=digit))
        assert session.mode == HUMAN and session.reply_epoch == 0
        assert session.profile == "1"
        assert harness.controller.keypads.get(session.id) is None
        assert router.counters["dtmf_ignored"] == 2
        # The same keys from the owner leg do take over.
        for digit in ("#", "1"):
            await router._queue_dtmf(OWNER, session.legs[OWNER],
                                     dtmf_event(digit, role=OWNER, sequence=digit))
        assert session.mode == AGENT and session.reply_epoch == 1
        await until(lambda: harness.marks(harness.remote) == ["clip-1-1"])
        await harness.settle(0.1)

    run(scenario)


def test_an_old_transport_generation_cannot_send_keys(tmp_path, monkeypatch):
    monkeypatch.setattr("operator_service.routes.PREPARE_SECONDS", PREPARE_DEADLINE)

    async def scenario():
        harness = Harness(tmp_path)
        session = await harness.joined()
        router = harness.controller.routers[session.id]
        router.generations[OWNER] = session.legs[OWNER].generation + 1
        await router._queue_dtmf(OWNER, session.legs[OWNER], dtmf_event("1"))
        assert session.mode == HUMAN and session.reply_epoch == 0
        assert router.counters["dtmf_ignored"] == 1

    run(scenario)


def test_a_key_before_the_two_legs_are_joined_does_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("operator_service.routes.PREPARE_SECONDS", PREPARE_DEADLINE)

    async def scenario():
        harness = Harness(tmp_path)
        session, _ = await harness.store.reserve_outbound(DESTINATION, "goal", str(uuid.uuid4()))
        await harness.store.mark_owner_prompt(session.id)
        await harness.store.begin_remote_dial(session.id)
        await harness.press(session.id, "#1")
        assert session.mode == HUMAN and session.reply_epoch == 0
        assert harness.controller.keypads == {}

    run(scenario)


# ------------------------------------------------------------------ lifecycle


def test_ending_a_session_cancels_speech_and_keypad_state(tmp_path, monkeypatch):
    monkeypatch.setattr("operator_service.routes.PREPARE_SECONDS", PREPARE_DEADLINE)

    async def scenario():
        harness = Harness(tmp_path, provider=Provider(frames=40))
        session = await harness.joined()
        await harness.press(session.id, "#1#")
        await until(lambda: len(harness.voiced(harness.remote)) >= 3)
        await harness.controller.end(session.id, "test-end")
        assert harness.controller.playing(session.id) is False
        assert harness.controller.players == {} and harness.controller.keys == {}
        assert harness.controller.keypads == {}
        assert harness.controller.prefetch == {}

    run(scenario)


def test_an_admin_mode_change_interrupts_speech(tmp_path, monkeypatch):
    monkeypatch.setattr("operator_service.routes.PREPARE_SECONDS", PREPARE_DEADLINE)

    async def scenario():
        harness = Harness(tmp_path, provider=Provider(frames=40))
        session = await harness.joined()
        await harness.press(session.id, "#1")
        await until(lambda: len(harness.voiced(harness.remote)) >= 3)
        assert await harness.controller.set_mode(session.id, HUMAN) is True
        assert harness.controller.playing(session.id) is False
        assert session.mode == HUMAN and session.reply_epoch == 2
        spoken = len(harness.voiced(harness.remote))
        await harness.settle(0.2)
        assert len(harness.voiced(harness.remote)) == spoken

    run(scenario)


def test_a_menu_answer_is_sent_once_after_the_quiet_window(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        harness.record_digits()
        session = await harness.joined()
        await harness.press(session.id, "4")
        await asyncio.sleep(0.01)
        assert harness.digits == []               # still inside the quiet window
        await until(lambda: harness.digits == ["4"])
        await harness.press(session.id, "52")
        await until(lambda: harness.digits == ["4", "52"])
        assert session.mode == HUMAN

    run(scenario)


def test_a_stalled_writer_stops_a_queued_phrase(tmp_path, monkeypatch):
    monkeypatch.setattr("operator_service.routes.PREPARE_SECONDS", PREPARE_DEADLINE)
    monkeypatch.setattr("operator_service.routes.CLIP_STALL_SECONDS", 0.05)

    async def scenario():
        harness = Harness(tmp_path, provider=Provider(frames=20))
        session = await harness.joined()
        router = harness.controller.routers[session.id]
        channel = router.channels[REMOTE]
        channel.attach(BlockedSocket(), REMOTE_STREAM, session.legs[REMOTE].generation,
                       session.legs[REMOTE].counters)
        await harness.press(session.id, "#1")
        await until(lambda: not harness.controller.playing(session.id))
        # A stuck socket must not leave a phrase queued for later replay.
        assert router.pending_agent(REMOTE) <= 4
        assert session.mode == AGENT

    run(scenario)
