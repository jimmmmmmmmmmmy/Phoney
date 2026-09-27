"""Framing, mixing, and routing checks with fake sockets; no Twilio, no phone."""

import asyncio
import base64
from collections import deque
import json
import uuid

import pytest

from config import Settings
from operator_service import OperatorSessions
from operator_service.audio import (AGENT_FRAMES, LIVE_FRAMES, UNDERFLOW_FRAMES,
                                   CallRouter, OutputChannel)
from operator_service.codecs import (SILENCE_FRAME, clear_message, decode_payload,
                                     inbound_frames, is_silence, mark_message, media_message,
                                     mix_ulaw, ringback_pattern, silence_ulaw, tone_ulaw,
                                     valid_media_format)
from operator_service.sessions import (AGENT, FRAME_COUNTERS, HUMAN, OWNER, REMOTE)

ACCOUNT = "AC" + "a" * 32
DESTINATION = "+12025550103"
OWNER_STREAM = "MZ" + "3" * 32
REMOTE_STREAM = "MZ" + "4" * 32
SETTINGS = Settings(
    account_sid=ACCOUNT, auth_token="operator-test-auth-token",
    public_base_url="https://operator.example", twilio_number="+12025550102",
    owner_number="+12025550101", allowed_destinations=(DESTINATION,),
    operator_admin_token="operator-admin-token-32-characters-long",
)
OWNER_FRAME = bytes([0x11]) * 160
REMOTE_FRAME = bytes([0x22]) * 160
AGENT_FRAME = bytes([0x33]) * 160


class FakeSocket:
    """A scripted socket: each step waits for its gate, then yields one message."""

    def __init__(self, script=()):
        self.script = deque(script)          # (gate or None, message)
        self.sent = []
        self.accepted = False
        self.closed = None
        self._hold = asyncio.Event()

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000):
        self.closed = code
        self._hold.set()

    async def send_json(self, message):
        self.sent.append(message)

    async def receive(self):
        if not self.script:
            await self._hold.wait()
            return {"type": "websocket.disconnect"}
        gate, message = self.script.popleft()
        if gate is not None:
            await gate.wait()
        return self._asgi(message)

    @staticmethod
    def _asgi(message):
        """Twilio's own JSON arrives inside an ASGI receive message."""
        if isinstance(message, dict) and "type" in message:
            return message
        text = message if isinstance(message, str) else json.dumps(message)
        return {"type": "websocket.receive", "text": text}


class StalledSocket(FakeSocket):
    """A socket whose writes never land, so a buffer must decide what to drop."""

    def __init__(self, script=()):
        super().__init__(script)
        self.gate = asyncio.Event()

    async def send_json(self, message):
        await self.gate.wait()
        await super().send_json(message)


class Controller:
    """Records control callbacks; the router must never wait on a provider."""

    def __init__(self):
        self.started = []
        self.stopped = []
        self.digits = []
        self.marks = []
        self.audio = []

    async def stream_started(self, session_id, role, stream_sid):
        self.started.append((role, stream_sid))

    async def stream_stopped(self, session_id, role, reason):
        self.stopped.append((role, reason))

    async def dtmf(self, session_id, digit):
        self.digits.append(digit)

    async def mark(self, session_id, role, name, state):
        self.marks.append((role, name, state))

    def on_audio(self, session_id, role, frame):
        self.audio.append((role, frame))

    def relay_ready(self, session_id):
        return True


async def until(predicate, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def counters():
    return dict.fromkeys(FRAME_COUNTERS, 0)


def events(socket):
    return [message["event"] for message in socket.sent]


def payloads(socket):
    return [base64.b64decode(message["media"]["payload"])
            for message in socket.sent if message["event"] == "media"]


def connected():
    return {"event": "connected", "protocol": "Call", "version": "1.0.0"}


def start(session, role):
    leg = session.legs[role]
    return {"event": "start", "sequenceNumber": "1", "streamSid": leg.stream_sid or "",
            "start": {"accountSid": ACCOUNT, "callSid": leg.call_sid,
                      "streamSid": OWNER_STREAM if role == OWNER else REMOTE_STREAM,
                      "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000,
                                      "channels": 1},
                      "customParameters": {"generation": str(leg.generation),
                                           "token": leg.token}}}


def media(frame, *, sequence="2", track="inbound", role=OWNER):
    return {"event": "media", "sequenceNumber": sequence,
            "streamSid": OWNER_STREAM if role == OWNER else REMOTE_STREAM,
            "media": {"track": track,
                      "payload": base64.b64encode(frame).decode("ascii")}}


def dtmf(digit, *, sequence="3"):
    return {"event": "dtmf", "sequenceNumber": sequence, "streamSid": OWNER_STREAM,
            "dtmf": {"track": "inbound_track", "digit": digit}}


def disconnect():
    return {"type": "websocket.disconnect"}


async def session_ready(store):
    session, _ = await store.reserve_outbound(DESTINATION, "goal", str(uuid.uuid4()))
    return session


# --------------------------------------------------------------------- codecs


def test_media_format_requires_mono_8k_mulaw():
    assert valid_media_format({"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1})
    for media_format in ({}, None, {"encoding": "audio/x-mulaw", "sampleRate": 16000, "channels": 1},
                         {"encoding": "audio/pcm", "sampleRate": 8000, "channels": 1},
                         {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 2},
                         {"encoding": "audio/x-mulaw", "sampleRate": True, "channels": 1}):
        assert valid_media_format(media_format) is False


def test_payloads_round_trip_and_reject_junk():
    message = media_message(OWNER_STREAM, OWNER_FRAME)
    assert message["event"] == "media" and message["streamSid"] == OWNER_STREAM
    assert decode_payload(message["media"]["payload"]) == OWNER_FRAME
    assert decode_payload("") == b""      # Twilio's first packet can be empty.
    for payload in (b"bytes", "!!!!", base64.b64encode(b"x" * 160).decode().rstrip("="),
                    "A" * 20_000):
        with pytest.raises(ValueError):
            decode_payload(payload)


def test_inbound_frames_normalize_and_ignore_the_far_end_track():
    assert inbound_frames(media(b"")) == ()
    assert inbound_frames(media(OWNER_FRAME)) == (OWNER_FRAME,)
    assert inbound_frames(media(OWNER_FRAME * 2)) == (OWNER_FRAME, OWNER_FRAME)
    short = inbound_frames(media(bytes([0x11]) * 100))
    assert len(short) == 1 and len(short[0]) == 160 and short[0][100:] == bytes([0xFF]) * 60
    for message in ({}, {"media": {}}, {"media": {"track": "outbound", "payload": "AAAA"}},
                    {"media": {"track": "outbound_track", "payload": "AAAA"}},
                    {"media": {"track": "inbound", "payload": "not-base64"}}):
        with pytest.raises(ValueError):
            inbound_frames(message)


def test_message_shapes_carry_only_the_destination_stream():
    assert clear_message(OWNER_STREAM) == {"event": "clear", "streamSid": OWNER_STREAM}
    assert mark_message(OWNER_STREAM, "reply-1") == {
        "event": "mark", "streamSid": OWNER_STREAM, "mark": {"name": "reply-1"}}
    assert is_silence(SILENCE_FRAME) and is_silence(bytes([0x7F]) * 160)
    assert is_silence(OWNER_FRAME) is False and is_silence(b"") is False


def test_mixing_halves_only_when_both_sides_speak():
    import struct

    from voice_stack.audio import ulaw_to_pcm16

    loud = bytes([0x00]) * 160
    opposite = bytes([0x80]) * 160
    assert mix_ulaw(SILENCE_FRAME, OWNER_FRAME) == OWNER_FRAME
    assert mix_ulaw(OWNER_FRAME, SILENCE_FRAME) == OWNER_FRAME
    mixed = mix_ulaw(loud, loud)
    assert len(mixed) == 160
    # Halving keeps the sum of two loud frames inside range: no sign wraparound.
    assert max(abs(sample) for (sample,) in struct.iter_unpack("<h", ulaw_to_pcm16(mixed))) < 32_767
    # Two opposite extremes cancel instead of clipping into loud noise.
    cancelled = mix_ulaw(loud, opposite)
    assert max(abs(sample) for (sample,) in struct.iter_unpack("<h", ulaw_to_pcm16(cancelled))) < 500
    with pytest.raises(ValueError):
        mix_ulaw(OWNER_FRAME, REMOTE_FRAME[:100])


def test_cue_tones_are_framed_and_validated():
    pattern = ringback_pattern()
    assert len(pattern) == 33_600 and len(pattern) % 160 == 0     # 4.2 seconds at 8 kHz
    assert is_silence(pattern[2400:4000])
    assert is_silence(silence_ulaw(20)) and len(silence_ulaw(20)) == 160
    assert is_silence(tone_ulaw(440, 20, amplitude=0))
    assert tone_ulaw(1000, 20) != SILENCE_FRAME
    for arguments in ((50, 200, 8000), (4000, 200, 8000), (440, 0, 8000), (440, 6000, 8000),
                      (440, 20, 20000)):
        with pytest.raises(ValueError):
            tone_ulaw(*arguments)
    with pytest.raises(ValueError):
        silence_ulaw(6000)


# --------------------------------------------------------------------- writer


def test_writer_paces_audio_marks_and_gives_clear_priority():
    async def run():
        socket = FakeSocket()
        channel = OutputChannel(OWNER)
        tally = counters()
        channel.attach(socket, OWNER_STREAM, 1, tally)
        assert channel.send(OWNER_FRAME) is True
        assert channel.mark("reply-1") is True
        await until(lambda: "mark" in events(socket))
        assert payloads(socket)[0] == OWNER_FRAME
        assert events(socket).index("mark") > events(socket).index("media")
        assert tally["frames_out"] >= 1
        for _ in range(5):
            channel.send(OWNER_FRAME)
        before = len(socket.sent)
        # The mark was already sent, so only the five queued frames are cleared.
        assert channel.clear() == (5, [])
        await until(lambda: "clear" in events(socket))
        assert events(socket)[before] == "clear"
        assert tally["cleared"] == 5 and tally["dropped"] == 0
        channel.detach()

    asyncio.run(run())


def test_live_audio_is_capped_and_the_oldest_frames_go_first():
    async def run():
        socket = StalledSocket()
        channel = OutputChannel(OWNER)
        tally = counters()
        channel.attach(socket, OWNER_STREAM, 1, tally)
        for index in range(LIVE_FRAMES + 5):
            channel.send(bytes([index]) * 160)
        assert len(channel.media) == LIVE_FRAMES
        assert tally["dropped"] == 5 and tally["gaps"] == 5
        assert channel.media[0] == bytes([5]) * 160
        # Agent speech gets the longer one-second budget instead.
        for index in range(AGENT_FRAMES + 2):
            channel.send(bytes([index]) * 160, kind="agent")
        assert len(channel.media) == AGENT_FRAMES
        channel.detach()

    asyncio.run(run())


def test_a_short_underflow_sends_silence_then_the_channel_goes_quiet():
    async def run():
        socket = FakeSocket()
        channel = OutputChannel(OWNER)
        tally = counters()
        channel.attach(socket, OWNER_STREAM, 1, tally)
        await asyncio.sleep(0.3)
        assert len(socket.sent) == UNDERFLOW_FRAMES
        assert all(payload == SILENCE_FRAME for payload in payloads(socket))
        await asyncio.sleep(0.1)
        assert len(socket.sent) == UNDERFLOW_FRAMES
        channel.detach()

    asyncio.run(run())


def test_detaching_a_channel_stops_its_writer_and_drops_queued_audio():
    async def run():
        socket = StalledSocket()
        channel = OutputChannel(OWNER)
        channel.attach(socket, OWNER_STREAM, 1, counters())
        channel.send(OWNER_FRAME)
        assert channel.attached
        channel.detach()
        assert channel.attached is False and not list(channel.media)
        await asyncio.sleep(0.05)
        assert socket.sent == []

    asyncio.run(run())


# --------------------------------------------------------------------- router


def test_agent_speech_reaches_the_remote_and_the_owner_monitor_mix():
    async def run():
        router = CallRouter("session", Controller())
        owner_socket, remote_socket = FakeSocket(), FakeSocket()
        router.channels[OWNER].attach(owner_socket, OWNER_STREAM, 1, counters())
        router.channels[REMOTE].attach(remote_socket, REMOTE_STREAM, 1, counters())
        assert router.send_agent((AGENT_FRAME,)) == 1
        assert router.forward(REMOTE, REMOTE_FRAME) is True
        await until(lambda: payloads(owner_socket) and payloads(remote_socket))
        assert payloads(remote_socket)[0] == AGENT_FRAME
        assert payloads(owner_socket)[0] == mix_ulaw(REMOTE_FRAME, AGENT_FRAME)
        with pytest.raises(ValueError):
            router.send_agent((b"short",))
        router.close()

    asyncio.run(run())


def test_delegation_mutes_the_owner_microphone_and_clears_buffered_speech():
    async def run():
        controller = Controller()
        router = CallRouter("session", controller)
        remote_socket, owner_socket = FakeSocket(), FakeSocket()
        router.channels[REMOTE].attach(remote_socket, REMOTE_STREAM, 1, counters())
        router.channels[OWNER].attach(owner_socket, OWNER_STREAM, 1, counters())
        router.channels[REMOTE].send(OWNER_FRAME)
        result = router.set_mode(AGENT)
        assert result["dropped"] == 1
        assert router.forward(OWNER, OWNER_FRAME) is False
        assert router.counters["owner_muted"] == 1
        assert controller.audio == []
        assert router.forward(REMOTE, REMOTE_FRAME) is True
        assert controller.audio == [(REMOTE, REMOTE_FRAME)]
        assert router.set_mode(AGENT) is None
        # Returning restores the owner's microphone and clears both outputs,
        # including audio they have not heard yet.
        assert router.set_mode(HUMAN)["dropped"] == 1
        assert router.forward(OWNER, OWNER_FRAME) is True
        assert controller.audio[-1] == (OWNER, OWNER_FRAME)
        router.close()

    asyncio.run(run())


def test_dropped_audio_is_counted_when_a_leg_has_no_stream_yet():
    async def run():
        controller = Controller()
        router = CallRouter("session", controller)
        assert router.forward(REMOTE, REMOTE_FRAME) is False
        assert router.counters["routed"] == 0
        # Audio is still offered to the transcription sink; only the missing
        # peer leg loses it, and the loss is what "dropped" measures.
        assert controller.audio == [(REMOTE, REMOTE_FRAME)]

    asyncio.run(run())


def test_waiting_caller_audio_is_captured_without_relaying_either_microphone():
    async def run():
        controller = Controller()
        controller.relay_ready = lambda session_id: False
        router = CallRouter("session", controller)
        owner, remote = FakeSocket(), FakeSocket()
        router.channels[OWNER].attach(owner, OWNER_STREAM, 1, counters())
        router.channels[REMOTE].attach(remote, REMOTE_STREAM, 1, counters())
        assert router.forward(OWNER, OWNER_FRAME) is False
        assert router.forward(REMOTE, REMOTE_FRAME) is False
        assert controller.audio == [(REMOTE, REMOTE_FRAME)]
        assert not router.channels[OWNER].media and not router.channels[REMOTE].media
        controller.relay_ready = lambda session_id: True
        assert router.forward(OWNER, OWNER_FRAME) is True
        assert router.forward(REMOTE, REMOTE_FRAME) is True
        router.close()
    asyncio.run(run())


def test_only_the_owner_leg_may_send_keypad_commands_once():
    async def run():
        store = OperatorSessions(SETTINGS)
        session = await session_ready(store)
        controller = Controller()
        router = CallRouter(session.id, controller)
        owner, remote = session.legs[OWNER], session.legs[REMOTE]
        router.generations[OWNER] = owner.generation
        await router._queue_dtmf(OWNER, owner, dtmf("1", sequence="7"))
        await router._queue_dtmf(OWNER, owner, dtmf("1", sequence="7"))
        await router._queue_dtmf(OWNER, owner, dtmf("2", sequence="8"))
        await router._queue_dtmf(OWNER, owner, dtmf("9", sequence="9"))
        await router._queue_dtmf(OWNER, owner, dtmf("!", sequence="10"))
        assert controller.digits == ["1", "2", "9"]
        assert owner.counters["dtmf"] == 3
        assert router.counters["dtmf_ignored"] == 1
        assert owner.counters["rejected"] == 1
        # A remote keypad cannot reach the controller at all.
        await router._queue_dtmf(REMOTE, remote, dtmf("#0"))
        assert controller.digits == ["1", "2", "9"]
        assert router.counters["dtmf_ignored"] == 2
        # Key events from an old transport generation are ignored too.
        router.generations[OWNER] = owner.generation + 1
        await router._queue_dtmf(OWNER, owner, dtmf("3", sequence="11"))
        assert controller.digits == ["1", "2", "9"]
        assert router.counters["dtmf_ignored"] == 3

    asyncio.run(run())


def test_marks_are_dispatched_and_cleared_ones_are_reported():
    async def run():
        store = OperatorSessions(SETTINGS)
        session = await session_ready(store)
        controller = Controller()
        router = CallRouter(session.id, controller)
        router.channels[OWNER].attach(FakeSocket(), OWNER_STREAM, 1, session.legs[OWNER].counters)
        assert await router.mark(OWNER, "reply-1") is True
        assert controller.marks == [(OWNER, "reply-1", "pending")]
        await router._mark_played(OWNER, {"mark": {"name": "reply-1"}})
        assert controller.marks[-1] == (OWNER, "reply-1", "played")
        await router._mark_played(OWNER, {"mark": {}})
        assert router.counters["bad_marks"] == 1
        assert await router.mark(OWNER, "reply-2") is True
        cleared = router.clear(OWNER)["marks"]
        assert sorted(cleared) == [(OWNER, "reply-1"), (OWNER, "reply-2")]
        router.close()

    asyncio.run(run())


def test_the_ring_cue_repeats_until_it_is_stopped():
    async def run():
        router = CallRouter("session", Controller())
        socket = FakeSocket()
        router.channels[OWNER].attach(socket, OWNER_STREAM, 1, counters())
        router.start_cue(OWNER)
        await until(lambda: len(payloads(socket)) >= 3)
        router.stop_cue()
        await asyncio.sleep(0.3)
        settled = len(socket.sent)
        await asyncio.sleep(0.15)
        assert len(socket.sent) == settled
        router.close()

    asyncio.run(run())


# ------------------------------------------------------------ reader and serve


def test_two_signed_streams_carry_audio_both_ways():
    async def run():
        store = OperatorSessions(SETTINGS)
        session = await session_ready(store)
        await store.bind_call_sid(session.id, OWNER, "CA" + "1" * 32)
        await store.bind_call_sid(session.id, REMOTE, "CA" + "2" * 32)
        controller = Controller()
        router = CallRouter(session.id, controller)
        # Each scripted step waits for its gate, so both readers are attached
        # before either frame is delivered and neither socket closes early.
        flow, finish = asyncio.Event(), asyncio.Event()
        owner_socket = FakeSocket([(None, connected()), (None, start(session, OWNER)),
                                   (flow, media(OWNER_FRAME, sequence="3")),
                                   (finish, disconnect())])
        remote_socket = FakeSocket([(None, connected()), (None, start(session, REMOTE)),
                                    (flow, media(REMOTE_FRAME, sequence="4", role=REMOTE)),
                                    (finish, disconnect())])
        tasks = [asyncio.create_task(router.serve(owner_socket, OWNER, store)),
                 asyncio.create_task(router.serve(remote_socket, REMOTE, store))]
        await until(lambda: router.attached(OWNER) and router.attached(REMOTE))
        flow.set()
        await until(lambda: OWNER_FRAME in payloads(remote_socket)
                    and REMOTE_FRAME in payloads(owner_socket))
        finish.set()
        await asyncio.gather(*tasks)
        assert owner_socket.accepted and remote_socket.accepted
        assert controller.started == [(OWNER, OWNER_STREAM), (REMOTE, REMOTE_STREAM)]
        assert sorted(controller.stopped) == [(OWNER, "socket-disconnected"),
                                              (REMOTE, "socket-disconnected")]
        assert session.legs[OWNER].counters["frames_in"] == 1
        assert session.legs[REMOTE].counters["frames_in"] == 1
        assert router.counters["routed"] == 2
        # Every outbound frame names its own destination stream.
        assert {message["streamSid"] for message in owner_socket.sent} == {OWNER_STREAM}
        assert {message["streamSid"] for message in remote_socket.sent} == {REMOTE_STREAM}
        assert session.legs[OWNER].attached is False
        assert session.legs[REMOTE].attached is False

    asyncio.run(run())


def test_a_start_with_the_wrong_token_closes_the_socket_and_binds_nothing():
    async def run():
        store = OperatorSessions(SETTINGS)
        session = await session_ready(store)
        controller = Controller()
        router = CallRouter(session.id, controller)
        message = start(session, OWNER)
        message["start"]["customParameters"]["token"] = "guessed-token"
        socket = FakeSocket([(None, connected()), (None, message), (None, disconnect())])
        await router.serve(socket, OWNER, store)
        assert socket.closed == 1008
        assert session.legs[OWNER].attached is False
        assert (session.legs[OWNER].stream_sid, session.legs[OWNER].state) == ("", "reserved")
        assert controller.started == []
        assert router.attached(OWNER) is False
        # The channel can still be attached afterwards: nothing was consumed.
        assert session.legs[OWNER].token_used is False

    asyncio.run(run())


def test_a_socket_that_never_binds_a_stream_is_given_up_on(monkeypatch):
    async def run():
        monkeypatch.setattr("operator_service.audio.START_SECONDS", 0.05)
        store = OperatorSessions(SETTINGS)
        session = await session_ready(store)
        controller = Controller()
        router = CallRouter(session.id, controller)
        socket = FakeSocket([(None, connected())])
        await router.serve(socket, OWNER, store)
        assert controller.stopped == [(OWNER, "start-timeout")]
        assert session.legs[OWNER].attached is False
        assert controller.started == []

    asyncio.run(run())


def test_unknown_messages_are_counted_and_never_forwarded():
    async def run():
        store = OperatorSessions(SETTINGS)
        session = await session_ready(store)
        await store.bind_call_sid(session.id, OWNER, "CA" + "1" * 32)
        controller = Controller()
        router = CallRouter(session.id, controller)
        script = [(None, connected()), (None, start(session, OWNER))]
        script += [(None, {"event": "mystery"}) for _ in range(8)]
        script += [(None, "not json"),
                   (None, {"event": "media", "media": {"track": "outbound", "payload": "AAAA"}}),
                   (None, disconnect())]
        socket = FakeSocket(script)
        await router.serve(socket, OWNER, store)
        assert session.legs[OWNER].counters["rejected"] == 1
        assert router.counters["rejected_messages"] == 10
        assert controller.stopped == [(OWNER, "invalid-message")]
        assert router.counters["routed"] == 0

    asyncio.run(run())
