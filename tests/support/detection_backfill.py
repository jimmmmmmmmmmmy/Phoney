"""Shared fixtures and fakes for focused integration checks."""

import asyncio


import json


from pathlib import Path


from types import SimpleNamespace


import wave


from partner_detection.backfill import BackfillManager


from partner_detection.storage import DetectionStore


CALL = 'CA' + '1' * 32


STREAM = 'MZ' + '2' * 32


def settings(tmp_path):
    return SimpleNamespace(media_storage_dir=str(tmp_path / 'audio'),
                           detection_storage_dir=str(tmp_path / 'detection'),
                           modulate_backfill_enabled=True, modulate_api_key='never-log',
                           modulate_detection_min_confidence=.8)


def capture(config, *, seconds=5, pad_ms=125, **changes):
    root = Path(config.media_storage_dir) / CALL
    root.mkdir(parents=True)
    samples = int(seconds * 8000) + pad_ms * 8
    with wave.open(str(root / 'inbound.wav'), 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(8000)
        wav.writeframes(bytes(pad_ms * 16) + b'\x01\x00' * int(seconds * 8000))
    manifest = {'call_sid': CALL, 'stream_sid': STREAM, 'status': 'completed',
                'finished_at': '2026-09-27T00:00:00Z', 'sample_rate': 8000,
                'channels': 1, 'sample_width': 2, 'encoding': 'pcm_s16le',
                'tracks': {'inbound': {'samples': samples, 'gap_samples': pad_ms * 8,
                                       'first_timestamp_ms': pad_ms}},
                'counters': {'dropped_messages': 0, 'rejected_messages': 0}, **changes}
    (root / 'manifest.json').write_text(json.dumps(manifest))
    return root, manifest


class Provider:
    def __init__(self):
        self.calls = []
        self.fail = False
    async def __call__(self, wav_bytes, *, source_start_ms):
        import io
        with wave.open(io.BytesIO(wav_bytes)) as wav:
            samples = wav.getnframes()
            audio = wav.readframes(samples)
        self.calls.append((source_start_ms, samples, audio[:2]))
        if self.fail:
            raise ValueError('private key and provider body')
        return {'frames': [{'start_ms': source_start_ms, 'end_ms': source_start_ms + samples // 8,
                            'verdict': 'synthetic', 'confidence': .95}]}
