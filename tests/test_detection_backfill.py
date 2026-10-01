"""Focused product and boundary checks; test helpers live in support."""

from pathlib import Path
import asyncio
import json

from partner_detection.backfill import BackfillManager
from partner_detection.storage import DetectionStore

from support.detection_backfill import CALL, Provider, capture, settings


def test_full_recording_retry_budget_survives_repeated_restarts(tmp_path):
    async def run():
        config = settings(tmp_path); capture(config, seconds=70)
        provider = Provider(); provider.fail = True
        store = DetectionStore(config.detection_storage_dir)
        path = Path(config.detection_storage_dir) / 'backfill' / (CALL + '.json')
        for attempt in range(1, 4):
            await BackfillManager(config, store, provider=provider).run_once()
            assert len(provider.calls) == attempt
            job = json.loads(path.read_text())
            assert job['version'] == 2 and len(job['chunks']) == 1
            assert job['chunks'][0]['attempts'] == attempt
            await BackfillManager(config, store, provider=provider).run_once()
            assert len(provider.calls) == attempt
            job['chunks'][0]['retry_at'] = 0
            path.write_text(json.dumps(job))
        await BackfillManager(config, store, provider=provider).run_once()
        assert len(provider.calls) == 3
        assert all(call[:2] == (125, 560000) for call in provider.calls)
        assert store.get(CALL)['analysis']['complete'] is False
    asyncio.run(run())
