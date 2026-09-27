"""Retrospective uploads are serial, private, resumable, and explicitly gated."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import wave

import pytest
from partner_detection.analysis import build_analysis
from partner_detection.backfill import BackfillManager, inspect_recordings, _chunks
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


def test_strip_initial_padding_preserve_offsets_cache_and_restart(tmp_path):
    async def run():
        config = settings(tmp_path); capture(config)
        provider = Provider(); store = DetectionStore(config.detection_storage_dir)
        manager = BackfillManager(config, store, provider=provider)
        await manager.run_once()
        assert provider.calls == [(125, 40000, b'\x01\x00')]
        result = store.get(CALL)
        assert result['analysis']['source'] == 'recording'
        assert result['analysis']['complete'] and result['analysis']['alert'] == 'ai_caller'
        assert result['analysis']['windows'][0]['start_ms'] == 125
        await manager.run_once()
        await BackfillManager(config, DetectionStore(config.detection_storage_dir), provider=provider).run_once()
        assert len(provider.calls) == 1
        assert manager.active_count == 0
    asyncio.run(run())


@pytest.mark.parametrize('change', ['gap', 'dropped', 'rejected', 'partial', 'short'])
def test_unreliable_or_short_recordings_abstain_without_upload(tmp_path, change):
    async def run():
        config = settings(tmp_path)
        root, manifest = capture(config, seconds=2 if change == 'short' else 5)
        if change == 'gap': manifest['tracks']['inbound']['gap_samples'] += 8
        if change in {'dropped', 'rejected'}: manifest['counters'][change + '_messages'] = 1
        if change == 'partial': manifest['status'] = 'partial'
        (root / 'manifest.json').write_text(json.dumps(manifest))
        provider = Provider(); store = DetectionStore(config.detection_storage_dir)
        await BackfillManager(config, store, provider=provider).run_once()
        assert provider.calls == []
        saved = store.get(CALL)
        assert saved['label'] == 'unknown' and saved['analysis']['complete'] is False
    asyncio.run(run())


def test_disabled_candidate_draining_and_live_calls_send_nothing(tmp_path):
    async def run():
        config = settings(tmp_path); capture(config)
        provider = Provider(); store = DetectionStore(config.detection_storage_dir)
        manager = BackfillManager(config, store, can_run=lambda: False, provider=provider)
        await manager.run_once()
        manager = BackfillManager(config, store, active_call_ids=lambda: {CALL}, provider=provider)
        await manager.run_once()
        config.modulate_backfill_enabled = False
        manager = BackfillManager(config, store, provider=provider)
        manager.start(); await manager.run_once()
        assert not provider.calls and manager._runner is None
    asyncio.run(run())


def test_chunks_are_between_four_and_sixty_seconds():
    for samples in [32000, 480000, 480001, 484000, 1800 * 8000]:
        chunks = list(_chunks(1000, 1000 + samples))
        assert sum(end - start for start, end in chunks) == samples
        assert all(32000 <= end - start <= 480000 for start, end in chunks)


def test_only_failed_chunk_retries_with_durable_attempt_budget(tmp_path):
    async def run():
        config = settings(tmp_path); capture(config, seconds=65)
        provider = Provider(); store = DetectionStore(config.detection_storage_dir)
        manager = BackfillManager(config, store, provider=provider)
        await manager.run_once()
        provider.fail = True
        await manager.run_once()
        assert len(provider.calls) == 2
        await manager.run_once()
        assert len(provider.calls) == 2
        path = Path(config.detection_storage_dir) / 'backfill' / (CALL + '.json')
        for _ in range(2):
            job = json.loads(path.read_text()); job['chunks'][1]['retry_at'] = 0
            path.write_text(json.dumps(job))
            await BackfillManager(config, store, provider=provider).run_once()
        await manager.run_once()
        assert len(provider.calls) == 4
        assert len({c[0] for c in provider.calls[1:]}) == 1
        job = json.loads(path.read_text())
        assert job['chunks'][0]['attempts'] == 1 and job['chunks'][1]['attempts'] == 3
        assert store.get(CALL)['analysis']['complete'] is False
    asyncio.run(run())


def test_global_file_lock_prevents_simultaneous_workers(tmp_path):
    async def run():
        config = settings(tmp_path); capture(config)
        entered, release = asyncio.Event(), asyncio.Event()
        provider = Provider()
        async def blocked(*args, **kwargs):
            entered.set(); await release.wait(); return await provider(*args, **kwargs)
        store = DetectionStore(config.detection_storage_dir)
        first = BackfillManager(config, store, provider=blocked)
        second = BackfillManager(config, store, provider=provider)
        task = asyncio.create_task(first.run_once())
        await entered.wait()
        assert first.active_count > 0
        await second.run_once()
        assert not provider.calls
        release.set(); await task
        await second.run_once()
        assert len(provider.calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize('reason', ['confident_synthetic', 'provider_timeout', 'provider_transport_failed'])
def test_valid_live_coverage_only_uploads_missing_recording_tail(tmp_path, reason):
    async def run():
        config = settings(tmp_path); capture(config, seconds=10)
        store = DetectionStore(config.detection_storage_dir)
        analysis = build_analysis([{'stream_id': STREAM, 'start_ms': 125, 'end_ms': 6125,
                                    'verdict': 'synthetic', 'confidence': .95}], complete=False)
        assert store.save(CALL, {'provider': 'modulate', 'status': 'complete', 'label': 'synthetic',
                                'confidence': .95, 'reason': reason, 'streams': 1,
                                'observations': 1, 'accepted_frames': 1, 'dropped_frames': 0,
                                'submitted_audio_ms': 6000, 'coverage_limited': True, 'analysis': analysis})
        provider = Provider()
        await BackfillManager(config, store, provider=provider).run_once()
        assert provider.calls[0][:2] == (6125, 32000)
        assert store.get(CALL)['analysis']['source'] == 'combined'
        assert store.get(CALL)['analysis']['complete'] is True
    asyncio.run(run())


@pytest.mark.parametrize('part', ['root', 'wav', 'manifest'])
def test_symlink_boundaries_are_not_followed(tmp_path, part):
    config = settings(tmp_path); root, _ = capture(config)
    if part == 'root':
        renamed = root.parent.with_name('actual'); root.parent.rename(renamed)
        root.parent.symlink_to(renamed, target_is_directory=True)
    else:
        name = 'inbound.wav' if part == 'wav' else 'manifest.json'
        outside = tmp_path / name; (root / name).rename(outside); (root / name).symlink_to(outside)
    assert inspect_recordings(config) == []


def test_malformed_cache_never_reauthorizes_a_paid_pass(tmp_path):
    async def run():
        config = settings(tmp_path); capture(config)
        cache = Path(config.detection_storage_dir) / 'backfill'; cache.mkdir(parents=True)
        (cache / (CALL + '.json')).write_text('{"attempts":"reset"}')
        provider = Provider()
        await BackfillManager(config, DetectionStore(config.detection_storage_dir), provider=provider).run_once()
        assert provider.calls == []
    asyncio.run(run())


@pytest.mark.parametrize('corruption', ['outside_recording', 'wrong_stream', 'outside_chunk'])
def test_cached_ranges_are_bound_to_the_actual_recording_before_upload(tmp_path, corruption):
    async def run():
        config = settings(tmp_path); capture(config, seconds=65)
        provider = Provider(); store = DetectionStore(config.detection_storage_dir)
        manager = BackfillManager(config, store, provider=provider)
        await manager.run_once()
        before = store.get(CALL)
        path = Path(config.detection_storage_dir) / 'backfill' / (CALL + '.json')
        job = json.loads(path.read_text())
        if corruption == 'outside_recording':
            job['chunks'][1].update(start=800000, end=840000)
        elif corruption == 'wrong_stream':
            job['chunks'][0]['windows'][0]['stream_id'] = 'MZ' + '3' * 32
        else:
            job['chunks'][0]['windows'][0]['end_ms'] += 1000
        path.write_text(json.dumps(job))
        await manager.run_once()
        assert len(provider.calls) == 1
        assert store.get(CALL) == before
    asyncio.run(run())


def test_scalar_confidence_only_uses_qualified_evidence(tmp_path):
    async def run():
        config = settings(tmp_path); capture(config)
        async def provider(wav_bytes, *, source_start_ms):
            return {'frames': [
                {'start_ms': source_start_ms, 'end_ms': source_start_ms + 4000,
                 'verdict': 'synthetic', 'confidence': .95},
                {'start_ms': source_start_ms + 4000, 'end_ms': source_start_ms + 5000,
                 'verdict': 'synthetic', 'confidence': .3}]}
        store = DetectionStore(config.detection_storage_dir)
        await BackfillManager(config, store, provider=provider).run_once()
        result = store.get(CALL)
        assert result['analysis']['alert'] == 'ai_caller'
        assert result['confidence'] == .95
    asyncio.run(run())


def test_persist_attempt_before_upload_and_do_not_spend_if_cache_write_fails(tmp_path, monkeypatch):
    import partner_detection.backfill as backfill
    async def run():
        config = settings(tmp_path); capture(config)
        provider = Provider()
        def fail(*args): raise OSError('private filesystem error')
        monkeypatch.setattr(backfill, '_job_save', fail)
        manager = BackfillManager(config, DetectionStore(config.detection_storage_dir), provider=provider)
        await manager.run_once()
        assert provider.calls == [] and manager.active_count == 0
    asyncio.run(run())


def test_wrong_live_stream_cannot_suppress_recording_analysis(tmp_path):
    async def run():
        config = settings(tmp_path); capture(config)
        store = DetectionStore(config.detection_storage_dir)
        analysis = build_analysis([{'stream_id': 'MZ' + '3' * 32, 'start_ms': 125, 'end_ms': 5125,
                                    'verdict': 'synthetic', 'confidence': .95}])
        store.save(CALL, {'provider': 'modulate', 'status': 'complete', 'label': 'synthetic',
                         'confidence': .95, 'reason': 'confident_synthetic', 'streams': 1,
                         'observations': 1, 'accepted_frames': 1, 'dropped_frames': 0,
                         'submitted_audio_ms': 5000, 'coverage_limited': False, 'analysis': analysis})
        provider = Provider()
        await BackfillManager(config, store, provider=provider).run_once()
        assert provider.calls[0][:2] == (125, 40000)
        assert store.get(CALL)['analysis']['source'] == 'recording'
    asyncio.run(run())


def test_live_coverage_covering_all_audio_needs_no_provider_request(tmp_path):
    async def run():
        config = settings(tmp_path); capture(config)
        store = DetectionStore(config.detection_storage_dir)
        analysis = build_analysis([{'stream_id': STREAM, 'start_ms': 125, 'end_ms': 5125,
                                    'verdict': 'synthetic', 'confidence': .95}])
        store.save(CALL, {'provider': 'modulate', 'status': 'complete', 'label': 'synthetic',
                         'confidence': .95, 'reason': 'confident_synthetic', 'streams': 1,
                         'observations': 1, 'accepted_frames': 1, 'dropped_frames': 0,
                         'submitted_audio_ms': 5000, 'coverage_limited': False, 'analysis': analysis})
        provider = Provider()
        await BackfillManager(config, store, provider=provider).run_once()
        assert provider.calls == [] and store.get(CALL)['analysis']['complete']
    asyncio.run(run())


def test_pending_crash_attempt_preserves_retry_deadline(tmp_path):
    async def run():
        config = settings(tmp_path); capture(config)
        provider = Provider(); provider.fail = True
        store = DetectionStore(config.detection_storage_dir)
        manager = BackfillManager(config, store, provider=provider)
        await manager.run_once()
        path = Path(config.detection_storage_dir) / 'backfill' / (CALL + '.json')
        job = json.loads(path.read_text()); job['chunks'][0]['status'] = 'pending'
        path.write_text(json.dumps(job))
        await BackfillManager(config, store, provider=provider).run_once()
        assert len(provider.calls) == 1
    asyncio.run(run())


def test_no_content_preserves_known_no_content_duration(tmp_path):
    async def run():
        config = settings(tmp_path); capture(config)
        provider = Provider()
        async def no_content(*args, **kwargs):
            response = await provider(*args, **kwargs)
            response['frames'][0]['verdict'] = 'no-content'
            return response
        store = DetectionStore(config.detection_storage_dir)
        await BackfillManager(config, store, provider=no_content).run_once()
        saved = store.get(CALL)
        assert saved['label'] == 'unknown'
        assert saved['analysis']['no_content_ms'] == 5000
        assert saved['analysis']['synthetic_share'] is None
    asyncio.run(run())


def test_completed_cache_does_not_rewrite_result_timestamp(tmp_path):
    async def run():
        config = settings(tmp_path); capture(config)
        provider = Provider(); store = DetectionStore(config.detection_storage_dir)
        manager = BackfillManager(config, store, provider=provider)
        assert await manager.run_once() is True
        before = store.get(CALL)['updated_at']
        assert await manager.run_once() is None
        assert store.get(CALL)['updated_at'] == before
    asyncio.run(run())


def test_transport_failure_has_sanitized_reason_and_preserves_partial_evidence(tmp_path):
    async def run():
        config = settings(tmp_path); capture(config, seconds=65)
        store = DetectionStore(config.detection_storage_dir)
        provider = Provider()
        manager = BackfillManager(config, store, provider=provider)
        await manager.run_once()
        async def failed(*args, **kwargs): raise OSError('secret token and raw provider details')
        manager.provider = failed
        await manager.run_once()
        saved = store.get(CALL)
        assert saved['reason'] == 'provider_transport_failed'
        assert saved['analysis']['synthetic_ms'] > 0
        assert saved['analysis']['complete'] is False
        assert 'secret' not in json.dumps(saved)
    asyncio.run(run())


def test_cli_defaults_to_metadata_only_dry_run(tmp_path, monkeypatch, capsys):
    from scripts import analyze_calls
    config = settings(tmp_path); capture(config)
    env = tmp_path / 'private.env'; env.write_text('MODULATE_API_KEY=never-print-key\n')
    monkeypatch.setattr(analyze_calls, 'load_dotenv', lambda *args, **kwargs: None)
    monkeypatch.setattr(analyze_calls.Settings, 'from_env', lambda: config)
    monkeypatch.setattr('sys.argv', ['analyze_calls.py', '--env-file', str(env)])
    assert analyze_calls.main() == 0
    output = capsys.readouterr().out
    assert json.loads(output)['mode'] == 'dry-run'
    assert json.loads(output)['total_duration_ms'] == 5000
    assert 'never-print-key' not in output
    assert not Path(config.detection_storage_dir).exists()
