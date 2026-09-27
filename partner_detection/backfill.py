"""Serial, resumable caller-recording analysis. Only explicit enablement sends audio."""

import asyncio
from copy import deepcopy
import fcntl
import hashlib
import io
import json
import math
import os
from pathlib import Path
import secrets
import re
import stat
import time
import wave

import httpx

from .analysis import build_analysis, validate_windows, MAX_WINDOWS, MIN_RELIABLE_MS
from .modulate import BATCH_URL, MAX_MESSAGE_BYTES, MAX_OBSERVATIONS, ModulateError, _batch_frame
from .storage import SID, DIR_FLAGS, READ_FLAGS, _root

MAX_SECONDS = 1800
MAX_SCAN = 1000
MAX_JOB_BYTES = 1024 * 1024
RETRY_DELAYS = (30, 120)
POLL_SECONDS = 10
SUCCESS_REASONS = {"confident_synthetic", "confident_non_synthetic", "insufficient_evidence",
                   "below_confidence_threshold", "conflicting_evidence", "no_usable_content",
                   "provider_timeout", "provider_transport_failed", "provider_reported_error",
                   "provider_incomplete", "audio_collection_timeout"}


def _read(fd, limit):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
        raise ValueError("invalid-file")
    with os.fdopen(fd, 'rb', closefd=False) as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError("oversized-file")
    return data


def _capture(settings, sid, *, audio=False):
    if not isinstance(sid, str) or not SID.fullmatch(sid):
        raise ValueError("invalid-call")
    root = _root(Path(settings.media_storage_dir))
    directory = None
    try:
        directory = os.open(sid, DIR_FLAGS, dir_fd=root)
        fd = os.open('manifest.json', READ_FLAGS, dir_fd=directory)
        try:
            raw = _read(fd, 65536)
            manifest = json.loads(raw)
        finally:
            os.close(fd)
        if (manifest.get('call_sid') != sid or manifest.get('status') not in {'completed', 'partial', 'failed'}
                or manifest.get('sample_rate') != 8000 or manifest.get('channels') != 1
                or manifest.get('sample_width') != 2 or manifest.get('encoding') != 'pcm_s16le'
                or not isinstance(manifest.get('finished_at'), str)):
            raise ValueError('unfinalized-recording')
        stream = manifest.get('stream_sid')
        if not isinstance(stream, str) or not re.fullmatch(r'MZ[0-9a-fA-F]{32}', stream):
            raise ValueError('invalid-stream')
        track = manifest['tracks']['inbound']
        first = track.get('first_timestamp_ms')
        gap = track.get('gap_samples')
        if type(first) is not int or first < 0 or type(gap) is not int or gap < 0:
            raise ValueError('invalid-capture-timing')
        fd = os.open('inbound.wav', READ_FLAGS, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_SECONDS * 16000 + 65536:
                raise ValueError('invalid-wav')
            with os.fdopen(os.dup(fd), 'rb') as source, wave.open(source, 'rb') as wav:
                samples = wav.getnframes()
                if ((wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 8000, 'NONE')
                        or type(track.get('samples')) is not int or samples != track['samples']
                        or not 0 <= first * 8 <= samples <= MAX_SECONDS * 8000):
                    raise ValueError('invalid-wav-format')
                pcm = wav.readframes(samples) if audio else None
                if audio and len(pcm) != samples * 2:
                    raise ValueError('truncated-wav')
            fingerprint = hashlib.sha256(raw + repr((info.st_size, info.st_mtime_ns, info.st_ino)).encode()).hexdigest()
        finally:
            os.close(fd)
        counters = manifest.get('counters', {})
        clean = (manifest['status'] == 'completed' and gap == first * 8
                 and type(counters.get('dropped_messages')) is int and counters['dropped_messages'] == 0
                 and type(counters.get('rejected_messages')) is int and counters['rejected_messages'] == 0)
        reason = '' if clean else 'incomplete_audio'
        if clean and samples - first * 8 < 32000:
            reason = 'insufficient_evidence'
        return {'call_sid': sid, 'stream_id': stream, 'fingerprint': fingerprint,
                'start_sample': first * 8, 'end_sample': samples,
                'duration_ms': (samples - first * 8) // 8, 'reason': reason,
                **({'pcm': pcm} if audio else {})}
    finally:
        if directory is not None:
            os.close(directory)
        os.close(root)


def inspect_recordings(settings, call_sid=None):
    """Metadata only, bounded traversal; invalid/unfinalized files are never eligible."""
    if call_sid is not None and (not isinstance(call_sid, str) or not SID.fullmatch(call_sid)):
        raise ValueError('invalid-call')
    try:
        root = _root(Path(settings.media_storage_dir))
    except (OSError, ValueError):
        return []
    try:
        with os.scandir(root) as entries:
            names = [entry.name for _, entry in zip(range(MAX_SCAN), entries)
                     if SID.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False)
                     and (call_sid is None or call_sid == entry.name)]
    finally:
        os.close(root)
    found = []
    for sid in sorted(names):
        try:
            found.append(_capture(settings, sid))
        except (OSError, ValueError, TypeError, KeyError, EOFError, wave.Error):
            continue
    return found


def _wav(pcm):
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(pcm)
    return output.getvalue()


def _chunks(start, end):
    """Legacy job partitions, retained for reading already-paid batch work."""
    count = max(1, math.ceil((end - start) / 480000))
    size, remainder = divmod(end - start, count)
    for index in range(count):
        stop = start + size + int(index < remainder)
        yield start, stop
        start = stop


def _plan(capture, existing):
    start, end = capture['start_sample'], capture['end_sample']
    live = []
    analysis = existing.get('analysis', {}) if isinstance(existing, dict) else {}
    if (analysis.get('source') == 'live' and existing.get('reason') in SUCCESS_REASONS):
        try:
            proposed = validate_windows(analysis.get('windows'))
            if all(w['stream_id'] == capture['stream_id'] and start <= w['start_ms'] * 8 < w['end_ms'] * 8 <= end
                   for w in proposed):
                live = proposed
        except (ValueError, TypeError):
            pass
    # Live rolling windows are provisional. A completed recording gets its own
    # continuous caller-only pass, regardless of live coverage. The 30-minute
    # capture bound is <29 MB and <=450 batch frames (provider bounds: 100 MB).
    return {'version': 2, 'fingerprint': capture['fingerprint'],
            'live': [] if capture['reason'] else live, 'quality': capture['reason'],
            'chunks': [] if capture['reason'] else [
                {'start': start, 'end': end, 'attempts': 0, 'status': 'missing',
                 'retry_at': 0, 'windows': [], 'error': ''}]}


def _full_batch_plan(job, capture):
    cursor = capture['start_sample']
    for chunk in job['chunks']:
        if chunk['start'] != cursor:
            return False
        cursor = chunk['end']
    return bool(job['chunks']) and cursor == capture['end_sample']


def _upgrade_job(job, capture):
    """Keep paid batch evidence/retry budgets; replace old live gap-fill plans."""
    if job['quality'] or _full_batch_plan(job, capture):
        return {**job, 'version': 2}
    upgraded = _plan(capture, None)
    upgraded['live'] = job['live']
    return upgraded


def _job_read(root, sid, fingerprint):
    try:
        fd = os.open(sid + '.json', READ_FLAGS, dir_fd=root)
    except FileNotFoundError:
        return None
    try:
        job = json.loads(_read(fd, MAX_JOB_BYTES))
    finally:
        os.close(fd)
    if (not isinstance(job, dict) or set(job) != {'version', 'fingerprint', 'live', 'quality', 'chunks'}
            or type(job['version']) is not int or job['version'] not in {1, 2} or job['fingerprint'] != fingerprint
            or job['quality'] not in {'', 'incomplete_audio', 'insufficient_evidence'}
            or not isinstance(job['chunks'], list) or len(job['chunks']) > 60):
        raise ValueError('invalid-job')
    validate_windows(job['live'])
    total = len(job['live'])
    for chunk in job['chunks']:
        if (not isinstance(chunk, dict) or set(chunk) != {'start', 'end', 'attempts', 'status', 'retry_at', 'windows', 'error'}
                or type(chunk['start']) is not int or type(chunk['end']) is not int
                or not 0 <= chunk['start'] < chunk['end'] <= MAX_SECONDS * 8000
                or not 32000 <= chunk['end'] - chunk['start'] <= (480000 if job['version'] == 1 else MAX_SECONDS * 8000)
                or type(chunk['attempts']) is not int or not 0 <= chunk['attempts'] <= 3
                or chunk['status'] not in {'missing', 'pending', 'failed', 'complete'}
                or chunk['error'] not in {'', 'provider_transport_failed', 'provider_timeout', 'invalid_provider_response'}
                or type(chunk['retry_at']) not in (int, float) or not math.isfinite(chunk['retry_at'])
                or not 0 <= chunk['retry_at'] < 10_000_000_000):
            raise ValueError('invalid-chunk')
        total += len(validate_windows(chunk['windows']))
        if chunk['status'] != 'complete' and chunk['windows']:
            raise ValueError('uncompleted-evidence')
    if total > MAX_WINDOWS:
        raise ValueError('too-many-windows')
    return job


def _job_save(root, sid, job):
    raw = json.dumps(job, allow_nan=False).encode()
    if len(raw) > MAX_JOB_BYTES:
        raise ValueError('oversized-job')
    name = '.' + sid + '.' + secrets.token_hex(6) + '.tmp'
    try:
        fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=root)
        with os.fdopen(fd, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, sid + '.json', src_dir_fd=root, dst_dir_fd=root)
    finally:
        try:
            os.unlink(name, dir_fd=root)
        except FileNotFoundError:
            pass


def _validate_job_capture(job, capture):
    """Bind cached ranges and evidence to the exact finalized caller recording."""
    start, end = capture['start_sample'], capture['end_sample']
    if job['quality'] != capture['reason'] or (job['quality'] and (job['live'] or job['chunks'])):
        raise ValueError('invalid-job-quality')
    for window in job['live']:
        if (window['stream_id'] != capture['stream_id']
                or not start <= window['start_ms'] * 8 < window['end_ms'] * 8 <= end):
            raise ValueError('invalid-live-range')
    previous_end = start
    for chunk in job['chunks']:
        if not previous_end <= chunk['start'] < chunk['end'] <= end:
            raise ValueError('invalid-capture-range')
        previous_end = chunk['end']
        for window in chunk['windows']:
            if (window['stream_id'] != capture['stream_id']
                    or window['start_ms'] < chunk['start'] // 8
                    or window['end_ms'] > math.ceil(chunk['end'] / 8)):
                raise ValueError('invalid-cached-window')
    if job['version'] == 2 and not job['quality'] and not _full_batch_plan(job, capture):
        raise ValueError('incomplete-recording-plan')


class BackfillManager:
    def __init__(self, settings, store, active_call_ids=lambda: set(), can_run=lambda: True, provider=None):
        self.settings, self.store = settings, store
        self.active_call_ids, self.can_run = active_call_ids, can_run
        self.enabled = bool(getattr(settings, 'modulate_backfill_enabled', False))
        self.provider = provider or self._provider
        self._runner = self._work = None
        self._closed = False
        self._io_tasks = set()
        self.call_sid = None

    @property
    def active_count(self):
        return int(self._work is not None and not self._work.done()) + sum(not t.done() for t in self._io_tasks)

    def _allowed(self, sid=None):
        try:
            return (not self._closed and self.can_run() is True
                    and (sid is None or sid not in self.active_call_ids()))
        except Exception:
            return False

    async def _io(self, method, *args):
        task = asyncio.create_task(asyncio.to_thread(method, *args))
        self._io_tasks.add(task)
        task.add_done_callback(self._io_tasks.discard)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Do not release the process lock while a disk write is still running.
            await asyncio.shield(task)
            raise

    async def _provider(self, wav_bytes, *, source_start_ms):
        with wave.open(io.BytesIO(wav_bytes), 'rb') as wav:
            duration = wav.getnframes() / 8
        async with asyncio.timeout(180):
            async with httpx.AsyncClient(timeout=httpx.Timeout(175, connect=10), follow_redirects=False) as client:
                async with client.stream('POST', BATCH_URL, headers={'X-API-Key': self.settings.modulate_api_key},
                                         files={'upload_file': ('caller.wav', wav_bytes, 'audio/wav')}) as response:
                    if response.status_code != 200:
                        raise ValueError('provider-error')
                    raw = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=16384):
                        if len(raw) + len(chunk) > MAX_MESSAGE_BYTES:
                            raise ValueError('provider-too-large')
                        raw.extend(chunk)
        body = json.loads(raw)
        if (not isinstance(body, dict) or type(body.get('duration_ms')) is not int
                or abs(body['duration_ms'] - duration) > 1 or not isinstance(body.get('frames'), list)
                or len(body['frames']) > MAX_OBSERVATIONS):
            raise ValueError('invalid-provider')
        return {'frames': [_batch_frame(frame, source_start_ms, body['duration_ms']) for frame in body['frames']]}

    def start(self):
        if self.enabled and not self._closed and (self._runner is None or self._runner.done()):
            self._runner = asyncio.create_task(self._run(), name='recording-analysis')

    async def _run(self):
        while not self._closed:
            await self.run_once()
            await asyncio.sleep(POLL_SECONDS)

    async def run_once(self):
        if not self.enabled or not self._allowed() or self.active_count:
            return
        self._work = asyncio.create_task(self._process(), name='recording-analysis-chunk')
        return await asyncio.shield(self._work)

    def _lock(self):
        root = _root(Path(self.settings.detection_storage_dir) / 'backfill', create=True)
        try:
            fd = os.open('.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=root)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError('invalid-lock')
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BaseException:
                os.close(fd)
                raise
            return root, fd
        except BaseException:
            os.close(root)
            raise

    async def _save_result(self, capture, job):
        recorded = [w for c in job['chunks'] for w in c['windows']]
        complete = not job['quality'] and bool(job['chunks']) and all(c['status'] == 'complete' for c in job['chunks'])
        # Do not let correlated live predictions contaminate the final batch
        # result. Preserve live evidence only as a provisional fallback.
        windows = recorded if recorded or complete else job['live']
        source = 'recording' if recorded or complete or not windows else 'live'
        analysis = build_analysis(windows, min_confidence=self.settings.modulate_detection_min_confidence,
                                  source=source, complete=complete,
                                  recording_fingerprint=capture['fingerprint'])
        label = {'ai_detected': 'synthetic', 'none': 'non-synthetic'}.get(analysis['alert'], 'unknown')
        confidence = min((w['confidence'] for w in windows if w['verdict'] == label
                          and w['confidence'] >= self.settings.modulate_detection_min_confidence),
                         default=None) if label != 'unknown' else None
        failure = next((c['error'] or 'provider_transport_failed' for c in job['chunks'] if c['status'] == 'failed'), '')
        unknown_reason = ('no_usable_content' if not analysis['analyzed_ms'] else
                          'insufficient_evidence' if analysis['synthetic_ms'] + analysis['non_synthetic_ms'] < MIN_RELIABLE_MS
                          else 'conflicting_evidence')
        reason = job['quality'] or failure or ({'synthetic': 'confident_synthetic',
                  'non-synthetic': 'confident_non_synthetic'}.get(label)
                  or unknown_reason)
        result = {'provider': 'modulate', 'status': 'complete' if label != 'unknown' else 'unknown',
                  'label': label, 'confidence': confidence, 'reason': reason, 'streams': 1,
                  'observations': len(windows), 'accepted_frames': 0, 'dropped_frames': 0,
                  'submitted_audio_ms': sum((c['end'] - c['start']) // 8 for c in job['chunks'] if c['status'] == 'complete'),
                  'coverage_limited': not complete, 'analysis': analysis}
        previous = await self._io(self.store.get, capture['call_sid'])
        if previous and all(previous.get(key) == value for key, value in result.items()):
            return True
        return await self._io(self.store.save, capture['call_sid'], result)

    async def _process(self):
        root = lock = None
        held = []
        def acquire():
            result = self._lock()
            held.extend(result)
            return result
        try:
            root, lock = await self._io(acquire)
            for capture in await self._io(inspect_recordings, self.settings, self.call_sid):
                sid = capture['call_sid']
                if not self._allowed(sid):
                    continue
                try:
                    job = await self._io(_job_read, root, sid, capture['fingerprint'])
                    if job is not None:
                        _validate_job_capture(job, capture)
                except (ValueError, TypeError, KeyError, OSError):
                    continue  # Corrupt/changed cache never silently authorizes another paid pass.
                if job is not None and job['version'] == 1:
                    # Keep the original evidence before a one-time plan migration.
                    await self._io(_job_save, root, sid + '.v1', job)
                    job = _upgrade_job(job, capture)
                    _validate_job_capture(job, capture)
                    await self._io(_job_save, root, sid, job)
                if job is None:
                    old = await self._io(self.store.get, sid)
                    if old and old.get('analysis', {}).get('recording_fingerprint') == capture['fingerprint']:
                        analysis = old['analysis']
                        if analysis['source'] != 'recording' or not analysis['complete'] or capture['reason']:
                            # Missing attempt history is not permission to reset
                            # a failed/provisional job's paid retry budget.
                            continue
                        job = _plan(capture, None)
                        job['chunks'][0].update(status='complete', attempts=1, windows=analysis['windows'])
                        _validate_job_capture(job, capture)
                    else:
                        job = _plan(capture, old)
                    await self._io(_job_save, root, sid, job)
                if job['quality'] or all(c['status'] == 'complete' for c in job['chunks']):
                    await self._save_result(capture, job)
                    continue
                for chunk in job['chunks']:
                    if chunk['status'] == 'complete' or chunk['attempts'] >= 3 or chunk['retry_at'] > time.time():
                        continue
                    if not self._allowed(sid):
                        return
                    # Reopen and revalidate the capture immediately before uploading it.
                    current = await self._io(lambda: _capture(self.settings, sid, audio=True))
                    if current['fingerprint'] != capture['fingerprint']:
                        return
                    chunk.update(attempts=chunk['attempts'] + 1, status='pending', error='',
                                 retry_at=time.time() + 190 + RETRY_DELAYS[min(chunk['attempts'], 1)])
                    await self._io(_job_save, root, sid, job)
                    try:
                        if not self._allowed(sid):
                            return
                        response = await asyncio.wait_for(self.provider(
                            _wav(current['pcm'][chunk['start'] * 2:chunk['end'] * 2]),
                            source_start_ms=chunk['start'] // 8), timeout=185)
                        frames = response['frames']
                        if not isinstance(frames, list) or len(frames) > MAX_OBSERVATIONS:
                            raise ValueError('invalid-frames')
                        windows = validate_windows([{key: f[key] for key in ('start_ms', 'end_ms', 'verdict', 'confidence')}
                                                    | {'stream_id': capture['stream_id']} for f in frames])
                        if any(w['start_ms'] < chunk['start'] // 8 or w['end_ms'] > math.ceil(chunk['end'] / 8) for w in windows):
                            raise ValueError('outside-chunk')
                        if len(job['live']) + sum(len(c['windows']) for c in job['chunks']) + len(windows) > MAX_WINDOWS:
                            raise ValueError('too-many-windows')
                        chunk.update(status='complete', windows=windows, retry_at=0, error='')
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        error = ('provider_timeout' if isinstance(exc, (TimeoutError, httpx.TimeoutException)) else
                                 'invalid_provider_response' if isinstance(exc, (ValueError, TypeError, KeyError, ModulateError)) else
                                 'provider_transport_failed')
                        chunk.update(status='failed', error=error, retry_at=(time.time() + RETRY_DELAYS[chunk['attempts'] - 1]
                                                               if chunk['attempts'] < 3 else 0))
                    await self._io(_job_save, root, sid, job)
                    await self._save_result(capture, job)
                    return True
        except asyncio.CancelledError:
            raise
        except Exception:
            return  # Sanitized advisory failures cannot interrupt calls or leak provider details.
        finally:
            for fd in reversed(held):
                os.close(fd)

    async def close(self):
        self._closed = True
        if self._runner is not None:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
        if self._work is not None and not self._work.done():
            await asyncio.wait({self._work}, timeout=5)
