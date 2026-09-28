#!/usr/bin/env python3
"""Inspect finalized caller WAVs; transmitting them requires --send-to-provider."""
import argparse
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from dotenv import load_dotenv
from config import Settings
from partner_detection.backfill import BackfillManager, inspect_recordings
from partner_detection.storage import DetectionStore
from scripts.dev import base_url


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, required=True, help='explicit private server environment file')
    parser.add_argument('--call-sid', help='limit analysis to one captured call')
    parser.add_argument('--send-to-provider', action='store_true')
    args = parser.parse_args()
    if not args.env_file.is_file():
        raise ValueError('Environment file is unavailable')
    load_dotenv(args.env_file, override=True)
    local_origin = base_url(os.environ)
    settings = Settings.from_env()
    recordings = inspect_recordings(settings, args.call_sid)
    print(json.dumps({'mode': 'send' if args.send_to_provider else 'dry-run',
                      'calls': [{key: row[key] for key in ('call_sid', 'duration_ms', 'reason')} for row in recordings],
                      'total_duration_ms': sum(row['duration_ms'] for row in recordings)}))
    if not args.send_to_provider:
        return 0
    if not settings.modulate_api_key or not settings.detection_storage_dir:
        raise ValueError('Configure the Modulate key and private detection directory')
    settings = replace(settings, modulate_backfill_enabled=True)
    active = set()
    manager = BackfillManager(settings, DetectionStore(settings.detection_storage_dir),
                              active_call_ids=lambda: active)
    manager.call_sid = args.call_sid
    async def run():
        # One bounded pass through possible chunks; failed attempts retain backoff.
        # The shared nonblocking lock prevents overlap with the installed worker.
        for _ in range(min(30000, max(1, len(recordings) * 30))):
            # Observe active calls afresh; never infer safety from stale saved files.
            try:
                async with httpx.AsyncClient(timeout=3) as client:
                    response = await client.get(local_origin + '/api/transcripts')
                    response.raise_for_status()
                    snapshot = response.json()
                active.clear()
                active.update(s['call_sid'] for s in snapshot['sessions'] if not s.get('ended_at'))
                active.update(s['call_sid'] for s in snapshot.get('detection', {}).get('calls', [])
                              if s.get('status') == 'analyzing')
            except (httpx.HTTPError, ValueError, TypeError, KeyError):
                print(json.dumps({'mode': 'deferred', 'reason': 'local-call-state-unavailable'}))
                break
            if not await manager.run_once():
                break
        await manager.close()
    asyncio.run(run())
    print(json.dumps({'mode': 'finished', 'calls': len(recordings),
                      'note': 'Completed chunks are cached; failures retain bounded retry backoff.'}))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError):
        print('Analysis unavailable; check the explicit environment file and private recording configuration.', file=sys.stderr)
        raise SystemExit(2)
