"""Live STT lifecycle with fake sockets only: no credentials or provider traffic."""

import asyncio
import base64
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from media_capture import CaptureManager
from transcription import TranscriptionManager
from transcription import service, storage

CALL = "CA" + "1" * 32
STREAM = "MZ" + "2" * 32
ACCOUNT = "AC" + "a" * 32


def settings(path, **changes):
    return SimpleNamespace(**(dict(transcription_enabled=True, deepgram_api_key="fixture-private-key",
                                  deepgram_model="nova-3", transcript_storage_dir=str(path),
                                  media_max_seconds=1800, account_sid=ACCOUNT,
                                  media_capture_enabled=True, media_storage_dir=str(path / "audio")) | changes))


def result(text="Hello there.", *, final=True, start=0, duration=0.02, confidence=0.98):
    return {"type": "Results", "start": start, "duration": duration, "is_final": final,
            "channel": {"alternatives": [{"transcript": text, "confidence": confidence}]}}


class Provider:
    def __init__(self, *, failure=False, stall=False, tail=None, close_result=True):
        self.messages = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.failure = failure
        self.stall = stall
        self.close_result = close_result
        self.tail = tail

    async def __aenter__(self):
        if self.failure:
            raise RuntimeError("Never expose fixture-private-key")
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    async def send(self, data):
        if self.stall:
            await asyncio.Event().wait()
        self.sent.append(data)
        if isinstance(data, str) and json.loads(data)["type"] == "CloseStream" and self.close_result:
            if self.tail:
                self.messages.put_nowait(json.dumps(self.tail))
            self.messages.put_nowait(json.dumps({"type": "Metadata"}))
            self.messages.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.messages.get()
        if message is None:
            raise StopAsyncIteration
        return message

    def push(self, event):
        self.messages.put_nowait(json.dumps(event))


class Connector:
    def __init__(self, options=None):
        self.sockets = []
        self.requests = []
        self.options = options or [{}, {}]

    def __call__(self, url, **kwargs):
        self.requests.append((url, kwargs))
        socket = Provider(**self.options[len(self.sockets) % len(self.options)])
        self.sockets.append(socket)
        return socket


async def until(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


async def complete(manager):
    manager.finish(CALL)
    await until(lambda: manager.active_count == 0)


def test_disabled_manager_opens_no_sockets_or_storage(tmp_path):
    connector = Connector()
    manager = TranscriptionManager(settings(tmp_path / "never", transcription_enabled=False), connector)
    manager.start(CALL, STREAM)
    manager.offer(CALL, "inbound", 0, b"\xff" * 160)
    manager.finish(CALL)
    asyncio.run(manager.close())
    assert connector.requests == []
    assert not (tmp_path / "never").exists()
    assert manager.snapshot()["sessions"] == []
    assert manager.snapshot()["enabled"] is False


def test_two_tracks_preserve_padding_and_use_verified_deepgram_contract(tmp_path):
    async def run():
        connector = Connector()
        manager = TranscriptionManager(settings(tmp_path), connector)
        manager.start(CALL, STREAM)
        manager.offer(CALL, "inbound", 20, b"\x00" * 160)
        manager.offer(CALL, "outbound", 40, b"\x80" * 160)
        await until(lambda: len(connector.sockets) == 2)
        await until(lambda: all(any(isinstance(item, bytes) for item in sock.sent) for sock in connector.sockets))
        assert b"".join(item for item in connector.sockets[0].sent if isinstance(item, bytes)) == b"\xff" * 160 + b"\x00" * 160
        assert b"".join(item for item in connector.sockets[1].sent if isinstance(item, bytes)) == b"\xff" * 320 + b"\x80" * 160
        for url, kwargs in connector.requests:
            parsed = urlsplit(url)
            assert parsed.scheme == "wss" and parsed.netloc == "api.deepgram.com"
            query = parse_qs(parsed.query)
            assert query["encoding"] == ["mulaw"] and query["sample_rate"] == ["8000"]
            assert query["model"] == ["nova-3"] and query["channels"] == ["1"]
            assert kwargs["additional_headers"] == {"Authorization": "Token fixture-private-key"}
            assert "fixture-private-key" not in url
        assert manager.snapshot()["sessions"][0]["tracks"]["outbound"]["meaning"] == "caller-playback"
        await complete(manager)
        assert all(socket.closed for socket in connector.sockets)
        assert manager.snapshot()["sessions"][0]["status"] == "completed"
    asyncio.run(run())


def test_interims_replace_final_segments_deduplicate_and_snapshot_is_detached(tmp_path):
    async def run():
        connector = Connector()
        manager = TranscriptionManager(settings(tmp_path), connector)
        manager.start(CALL, STREAM)
        manager.offer(CALL, "inbound", 0, b"\xff" * 160)
        await until(lambda: len(connector.sockets) == 2)
        inbound = connector.sockets[0]
        inbound.push(result("First", final=False))
        await until(lambda: manager.snapshot()["sessions"][0]["tracks"]["inbound"]["interim"] == "First")
        inbound.push(result("Second", final=False))
        await until(lambda: manager.snapshot()["sessions"][0]["tracks"]["inbound"]["interim"] == "Second")
        inbound.push(result("Final"))
        inbound.push(result("Final"))
        inbound.push(result("Corrected final"))
        await until(lambda: len(manager.snapshot()["sessions"][0]["segments"]) == 1 and
                    manager.snapshot()["sessions"][0]["segments"][0]["text"] == "Corrected final")
        snap = manager.snapshot()
        assert snap["sessions"][0]["tracks"]["inbound"]["interim"] == ""
        snap["sessions"][0]["segments"][0]["text"] = "mutated"
        assert manager.snapshot()["sessions"][0]["segments"][0]["text"] == "Corrected final"
        await complete(manager)
    asyncio.run(run())


def test_first_word_onset_is_listener_only_and_preserves_result_identity(tmp_path):
    async def run():
        connector, observed = Connector(), []
        manager = TranscriptionManager(settings(tmp_path), connector,
            on_segment=lambda sid, segment, final: observed.append((segment, final)))
        manager.start(CALL, STREAM)
        manager.offer(CALL, "inbound", 3000, b"\xff" * 160)
        await until(lambda: len(connector.sockets) == 2)
        message = result("Wait", final=False, start=1, duration=2)
        message["channel"]["alternatives"][0]["words"] = [
            {"word": "Wait", "start": 2.4, "end": 2.7}]
        connector.sockets[0].push(message)
        await until(lambda: len(observed) == 1)
        message["is_final"] = True
        connector.sockets[0].push(message)
        await until(lambda: len(observed) == 2)
        assert [final for _, final in observed] == [False, True]
        assert all(segment["speech_start_ms"] == 2400 for segment, _ in observed)
        assert all(segment["start_ms"] == 1000 for segment, _ in observed)
        # A timing-only word update neither changes canonical identity nor
        # replays the final transcript to the control listener.
        message["channel"]["alternatives"][0]["words"][0]["start"] = 2.5
        connector.sockets[0].push(message)
        await complete(manager)
        assert len(observed) == 2
        stored = manager.history[0]["segments"]
        assert len(stored) == 1 and stored[0]["id"] == "inbound-0"
        assert stored[0]["start_ms"] == 1000 and stored[0]["end_ms"] == 3000
        assert "speech_start_ms" not in stored[0]
        restored = TranscriptionManager(settings(tmp_path), Connector())
        assert restored.history[0]["segments"] == stored
        await restored.close()
    asyncio.run(run())


@pytest.mark.parametrize("words", [
    None, [], "invalid", [None],
    [{"word": "Wait", "start": float("nan"), "end": 2.7}],
    [{"word": "Wait", "start": 2.4, "end": float("inf")}],
    [{"word": "Wait", "start": True, "end": 2.7}],
    [{"word": "Wait", "start": "2.4", "end": 2.7}],
    [{"word": "Wait", "start": .9, "end": 2.7}],
    [{"word": "Wait", "start": 2.4, "end": 3.1}],
    [{"word": "Wait", "start": 2.7, "end": 2.4}],
    [{"word": "", "start": 2.4, "end": 2.7}],
    [{"word": "Wait", "start": 2.4}],
])
def test_malformed_word_timing_falls_back_without_failing_transcription(tmp_path, words):
    async def run():
        connector, observed = Connector(), []
        manager = TranscriptionManager(settings(tmp_path), connector,
            on_segment=lambda sid, segment, final: observed.append(segment))
        manager.start(CALL, STREAM)
        manager.offer(CALL, "inbound", 3000, b"\xff" * 160)
        await until(lambda: len(connector.sockets) == 2)
        message = result("Wait", start=1, duration=2)
        message["channel"]["alternatives"][0]["words"] = words
        connector.sockets[0].push(message)
        await until(lambda: bool(observed))
        assert observed[0]["start_ms"] == 1000
        assert "speech_start_ms" not in observed[0]
        assert not manager.sessions[CALL].tracks["inbound"].error
        await complete(manager)
    asyncio.run(run())


def test_close_stream_tail_persists_privately_and_reloads_history(tmp_path):
    async def run():
        connector = Connector([{"tail": result("Last words.")}, {}])
        manager = TranscriptionManager(settings(tmp_path), connector)
        manager.start(CALL, STREAM)
        manager.offer(CALL, "inbound", 0, b"\xff" * 160)
        await complete(manager)
        assert manager.snapshot()["sessions"][0]["segments"][0]["text"] == "Last words."
        file = tmp_path / f"{CALL}.json"
        text = file.read_text()
        assert "fixture-private-key" not in text and "payload" not in text
        assert os.stat(file).st_mode & 0o777 == 0o600
        assert os.stat(tmp_path).st_mode & 0o777 == 0o700
        restored = TranscriptionManager(settings(tmp_path), Connector())
        assert restored.snapshot()["sessions"][0]["segments"][0]["text"] == "Last words."
        restored.start(CALL, STREAM)
        assert restored.active_count == 0
    asyncio.run(run())


def test_queue_overflow_only_fails_affected_track(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "QUEUE_FRAMES", 2)
    async def run():
        connector = Connector()
        manager = TranscriptionManager(settings(tmp_path), connector)
        manager.start(CALL, STREAM)
        for timestamp in (0, 20, 40):
            manager.offer(CALL, "inbound", timestamp, b"\xff" * 160)
        assert manager.snapshot()["sessions"][0]["tracks"]["inbound"]["error"] == "audio-queue-overflow"
        manager.offer(CALL, "outbound", 0, b"\xff" * 160)
        await complete(manager)
        session = manager.snapshot()["sessions"][0]
        assert session["status"] == "partial"
        assert session["tracks"]["outbound"]["status"] == "completed"
        assert all(socket.closed for socket in connector.sockets)
    asyncio.run(run())


@pytest.mark.parametrize("timestamp,payload,error", [(0,b"x","audio-order"),
                                                    (-1,b"x","invalid-audio"),
                                                    (2_000_000,b"x","duration-limit")])
def test_invalid_timeline_fails_track_without_throwing(tmp_path, timestamp, payload, error):
    async def run():
        manager = TranscriptionManager(settings(tmp_path), Connector())
        manager.start(CALL, STREAM)
        manager.offer(CALL, "inbound", 0, b"\xff" * 160)
        manager.offer(CALL, "inbound", timestamp, payload)
        assert manager.snapshot()["sessions"][0]["tracks"]["inbound"]["error"] == error
        await complete(manager)
    asyncio.run(run())


@pytest.mark.parametrize("message", [result(duration=5),result(confidence=float("nan")),
                                     result(final="true"),{"type":"Error","description":"fixture-private-key"},
                                     {"type":"Results","channel":[]}])
def test_invalid_provider_results_fail_visibly_without_exposing_raw_errors(tmp_path, message):
    async def run():
        connector = Connector()
        manager = TranscriptionManager(settings(tmp_path), connector)
        manager.start(CALL, STREAM)
        manager.offer(CALL, "inbound", 0, b"\xff" * 160)
        await until(lambda: len(connector.sockets) == 2)
        connector.sockets[0].push(message)
        await until(lambda: manager.snapshot()["sessions"][0]["tracks"]["inbound"]["error"])
        assert "fixture-private-key" not in json.dumps(manager.snapshot())
        await complete(manager)
    asyncio.run(run())


def test_connection_failures_are_sanitized_and_leave_no_tasks(tmp_path):
    async def run():
        manager = TranscriptionManager(settings(tmp_path), Connector([{"failure":True}]))
        manager.start(CALL, STREAM)
        await until(lambda: manager.active_count == 0)
        session = manager.snapshot()["sessions"][0]
        assert session["status"] == "failed"
        assert session["tracks"]["inbound"]["error"] == "provider-unavailable"
        assert "fixture-private-key" not in json.dumps(session)
        await manager.close()
    asyncio.run(run())


def test_keepalive_is_text_and_stop_flush_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "KEEPALIVE_SECONDS", 0.01)
    monkeypatch.setattr(service, "FLUSH_SECONDS", 0.03)
    async def run():
        connector = Connector([{"close_result":False}])
        manager = TranscriptionManager(settings(tmp_path), connector)
        manager.start(CALL, STREAM)
        await until(lambda: len(connector.sockets) == 2 and all(socket.sent for socket in connector.sockets))
        assert all(json.loads(socket.sent[0]) == {"type":"KeepAlive"} for socket in connector.sockets)
        await complete(manager)
        assert manager.snapshot()["sessions"][0]["tracks"]["inbound"]["error"] == "flush-timeout"
        assert all(socket.closed for socket in connector.sockets)
    asyncio.run(run())


def test_flush_deadline_also_bounds_stalled_audio_sender(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "FLUSH_SECONDS", 0.03)
    async def run():
        connector = Connector([{"stall":True}])
        manager = TranscriptionManager(settings(tmp_path), connector)
        manager.start(CALL, STREAM)
        manager.offer(CALL, "inbound", 0, b"\xff" * 160)
        await complete(manager)
        assert manager.snapshot()["sessions"][0]["tracks"]["inbound"]["error"] == "flush-timeout"
        assert all(socket.closed for socket in connector.sockets)
    asyncio.run(run())


def test_shutdown_closes_workers_and_counts_flush_until_finished(tmp_path):
    async def run():
        connector = Connector()
        manager = TranscriptionManager(settings(tmp_path), connector)
        manager.start(CALL, STREAM)
        await until(lambda: len(connector.sockets) == 2)
        manager.finish(CALL)
        assert manager.active_count > 0
        await manager.close()
        assert manager.active_count == 0
        assert all(socket.closed for socket in connector.sockets)
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()]
    asyncio.run(run())


def test_storage_failure_does_not_erase_live_result(tmp_path, monkeypatch):
    def fail(*args): raise OSError("private path not in logs")
    monkeypatch.setattr(storage,"save",fail)
    async def run():
        manager = TranscriptionManager(settings(tmp_path), Connector())
        manager.start(CALL,STREAM)
        await complete(manager)
        assert manager.snapshot()["sessions"][0]["storage_error"] == "storage-failed"
    asyncio.run(run())


def test_reload_ignores_symlinks_and_invalid_history(tmp_path):
    (tmp_path / (CALL+".json")).write_text(json.dumps({"call_sid":CALL,"provider":"deepgram"}))
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    (tmp_path / ("CA" + "3"*32 + ".json")).symlink_to(outside)
    manager = TranscriptionManager(settings(tmp_path), Connector())
    assert manager.snapshot()["sessions"] == []


class TwilioSocket:
    def __init__(self, events):
        self.events = asyncio.Queue()
        for event in events:
            self.events.put_nowait({"type":"websocket.receive","text":json.dumps(event)})
    async def accept(self): pass
    async def receive(self): return await self.events.get()
    async def close(self, **kwargs): self.events.put_nowait({"type":"websocket.disconnect"})


def capture_events(ticket, valid=True):
    return [{"event":"start","streamSid":STREAM,"start":{
        "accountSid":ACCOUNT,"callSid":CALL,"streamSid":STREAM,
        "customParameters":{"token":ticket.token if valid else "wrong"},
        "tracks":["inbound","outbound"],
        "mediaFormat":{"encoding":"audio/x-mulaw","sampleRate":8000,"channels":1}}},
        {"event":"media","streamSid":STREAM,"media":{
            "track":"inbound","chunk":"1","timestamp":"0",
            "payload":base64.b64encode(b"\xff"*160).decode()}},
        {"event":"stop","streamSid":STREAM,"stop":{"accountSid":ACCOUNT,"callSid":CALL}}]


def test_capture_observer_only_receives_validated_stream_and_accepts_failures(tmp_path):
    class Observer:
        def __init__(self): self.events=[]
        def start(self,*args): self.events.append(("start",args));raise ValueError("observer failed")
        def offer(self,*args): self.events.append(("offer",args));raise ValueError("observer failed")
        def finish(self,*args): self.events.append(("finish",args));raise ValueError("observer failed")
    async def run():
        observer = Observer()
        manager = CaptureManager(settings(tmp_path), observer)
        ticket=manager.reserve(CALL)
        await manager.handle(TwilioSocket(capture_events(ticket)),CALL)
        await asyncio.sleep(0)
        assert [name for name,args in observer.events] == ["start","offer","finish"]
        assert observer.events[1][1] == (CALL,"inbound",0,b"\xff"*160)
        manifest=json.loads((tmp_path/"audio"/CALL/"manifest.json").read_text())
        assert manifest["status"]=="completed"
        other=CaptureManager(settings(tmp_path/"other"), Observer())
        ticket=other.reserve(CALL)
        await other.handle(TwilioSocket(capture_events(ticket,valid=False)),CALL)
        assert other.observer.events==[]
    asyncio.run(run())


def test_capacity_failure_is_visible_without_extra_provider_connection(tmp_path):
    async def run():
        connector=Connector()
        manager=TranscriptionManager(settings(tmp_path),connector)
        manager.start(CALL,STREAM)
        manager.start("CA"+"3"*32,"MZ"+"4"*32)
        third="CA"+"5"*32
        manager.start(third,"MZ"+"6"*32)
        rejected=next(item for item in manager.snapshot()["sessions"] if item["call_sid"]==third)
        assert rejected["status"]=="failed"
        assert rejected["tracks"]["inbound"]["error"]=="capacity-limit"
        assert rejected["storage_error"]=="not-recorded"
        await manager.close()
        assert len(connector.sockets)==4
    asyncio.run(run())


def test_pending_disk_write_remains_in_deployment_drain_count(tmp_path, monkeypatch):
    import threading
    entered, release = threading.Event(), threading.Event()
    real_save = storage.save
    def slow_save(*args):
        entered.set()
        release.wait(timeout=2)
        real_save(*args)
    monkeypatch.setattr(storage,"save",slow_save)
    monkeypatch.setattr(service,"STORE_SECONDS",0.02)
    async def run():
        manager=TranscriptionManager(settings(tmp_path),Connector())
        manager.start(CALL,STREAM)
        manager.finish(CALL)
        try:
            await until(lambda: entered.is_set() and bool(manager.history))
            assert manager.snapshot()["sessions"][0]["storage_error"]=="storage-failed"
            assert manager.active_count>0
        finally:
            release.set()
        await until(lambda: manager.active_count==0)
        await manager.close()
    asyncio.run(run())


def test_duration_ceiling_finishes_provider_streams_without_external_trigger(tmp_path,monkeypatch):
    # Fractional fixture ceiling speeds the same production duration-guard path.
    async def run():
        connector=Connector()
        manager=TranscriptionManager(settings(tmp_path,media_max_seconds=0.02),connector)
        manager.start(CALL,STREAM)
        await until(lambda:manager.active_count==0)
        session=manager.snapshot()["sessions"][0]
        assert session["finish_reason"]=="duration-limit"
        assert session["status"]=="partial"
        assert all(socket.closed for socket in connector.sockets)
    asyncio.run(run())


@pytest.mark.parametrize('ending', ['speech-final', 'empty-final', 'utterance-end', 'repeated-final'])
def test_turn_boundaries_are_control_only_and_final_chunks_remain_saved(tmp_path, ending):
    async def run():
        connector, observed = Connector(), []
        manager = TranscriptionManager(settings(tmp_path), connector,
            on_segment=lambda sid, segment, final: observed.append((segment, final)))
        manager.start(CALL, STREAM)
        manager.offer(CALL, 'inbound', 5000, b'\xff' * 160)
        await until(lambda: len(connector.sockets) == 2)
        sock = connector.sockets[0]
        sock.push({'type': 'SpeechStarted', 'timestamp': 0.1})
        first = result('90,000 miles.', start=.1, duration=1)
        first['speech_final'] = False
        sock.push(first)
        second = result('And the condition is good.', start=1.1, duration=1)
        second['speech_final'] = ending == 'speech-final'
        sock.push(second)
        if ending == 'empty-final':
            last = result('', start=2.1, duration=.5)
            last['speech_final'] = True
            sock.push(last)
        elif ending == 'utterance-end':
            sock.push({'type': 'UtteranceEnd', 'last_word_end': 2.1})
        elif ending == 'repeated-final':
            second['speech_final'] = True
            sock.push(second)
        await until(lambda: any(row.get('speech_final') for row, _ in observed))
        # A redundant utterance event cannot produce a second response.
        sock.push({'type': 'UtteranceEnd', 'last_word_end': 2.1})
        await complete(manager)
        assert sum(bool(row.get('speech_final')) for row, _ in observed) == 1
        assert observed[0][0]['speech_started'] is True
        assert [row['text'] for row, final in observed if final] == [
            '90,000 miles.', 'And the condition is good.']
        rows = manager.history[0]['segments']
        assert len(rows) == 2
        assert all('speech_final' not in row and 'speech_started' not in row for row in rows)
        query = parse_qs(urlsplit(connector.requests[0][0]).query)
        assert query['endpointing'] == ['750']
        assert query['utterance_end_ms'] == ['1000']
        assert query['vad_events'] == ['true']
    asyncio.run(run())


def test_stale_utterance_end_cannot_close_new_speech(tmp_path):
    async def run():
        connector, observed = Connector(), []
        manager = TranscriptionManager(settings(tmp_path), connector,
            on_segment=lambda sid, segment, final: observed.append(segment))
        manager.start(CALL, STREAM)
        manager.offer(CALL, 'inbound', 5000, b'\xff' * 160)
        await until(lambda: len(connector.sockets) == 2)
        sock = connector.sockets[0]
        sock.push(result('90,000 miles.', start=.1, duration=1))
        sock.push({'type': 'SpeechStarted', 'timestamp': 2})
        sock.push({'type': 'UtteranceEnd', 'last_word_end': 1.1})
        await until(lambda: len(observed) == 2)
        assert not any(row['speech_final'] for row in observed)
        final = result('And it is in good condition.', start=2, duration=1)
        final['speech_final'] = True
        sock.push(final)
        await until(lambda: len(observed) == 3)
        assert observed[-1]['speech_final']
        await complete(manager)
    asyncio.run(run())


@pytest.mark.parametrize('ending', ['new-final', 'updated-final', 'repeated-final', 'empty-final'])
def test_stale_result_endpoint_cannot_close_new_speech(tmp_path, ending):
    async def run():
        connector, observed = Connector(), []
        manager = TranscriptionManager(settings(tmp_path), connector,
            on_segment=lambda sid, segment, final: observed.append((segment, final)))
        manager.start(CALL, STREAM)
        manager.offer(CALL, 'inbound', 5000, b'\xff' * 160)
        await until(lambda: len(connector.sockets) == 2)
        sock = connector.sockets[0]
        first = result('90,000 miles.', start=.1, duration=1)
        sock.push(first)
        sock.push({'type': 'SpeechStarted', 'timestamp': 2})
        if ending == 'new-final':
            delayed = result('On the odometer.', start=1.1, duration=.5)
        elif ending == 'updated-final':
            delayed = result('Ninety thousand miles.', start=.1, duration=1)
        elif ending == 'repeated-final':
            delayed = dict(first)
        else:
            delayed = result('', start=1.1, duration=.5)
        sock.push(delayed | {'speech_final': True})
        # This ordered interim proves the delayed endpoint was processed before
        # checking that the newer utterance remains open.
        sock.push(result('And the condition', final=False, start=2, duration=.5))
        await until(lambda: observed[-1][0]['text'] == 'And the condition' if observed else False)
        assert not any(row['speech_final'] for row, _ in observed)
        assert manager.sessions[CALL].tracks['inbound'].pending_utterance
        assert manager.sessions[CALL].tracks['inbound'].speech_active
        sock.push(result('And the condition is good.', start=2, duration=1) | {'speech_final': True})
        await until(lambda: any(row['speech_final'] for row, _ in observed))
        assert sum(bool(row['speech_final']) for row, _ in observed) == 1
        assert observed[-1][0]['text'] == 'And the condition is good.'
        await complete(manager)
        saved = [row['text'] for row in manager.history[0]['segments']]
        expected = ['Ninety thousand miles.'] if ending == 'updated-final' else ['90,000 miles.']
        if ending == 'new-final':
            expected.append('On the odometer.')
        assert saved == expected + ['And the condition is good.']
    asyncio.run(run())


def test_empty_endpoint_releases_speech_activity_without_a_new_final(tmp_path):
    async def run():
        connector, observed = Connector(), []
        manager = TranscriptionManager(settings(tmp_path), connector,
            on_segment=lambda sid, segment, final: observed.append(segment))
        manager.start(CALL, STREAM)
        manager.offer(CALL, 'inbound', 5000, b'\xff' * 160)
        await until(lambda: len(connector.sockets) == 2)
        sock = connector.sockets[0]
        first = result('90,000 miles.', start=.1, duration=1)
        first['speech_final'] = True
        sock.push(first)
        sock.push({'type': 'SpeechStarted', 'timestamp': 2})
        end = result('', start=2, duration=1)
        end['speech_final'] = True
        sock.push(end)
        await until(lambda: len(observed) == 3)
        assert observed[-1]['text'] == '' and observed[-1]['speech_final']
        await complete(manager)
        assert len(manager.history[0]['segments']) == 1
    asyncio.run(run())
