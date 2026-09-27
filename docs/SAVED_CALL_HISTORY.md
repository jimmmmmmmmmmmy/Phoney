# Saved call history

Saved transcript JSON, caller metadata, detection results, and recordings remain
in their configured private storage directories. There is no automatic deletion.
The ten-call in-memory preview is a performance limit, not a retention limit.

## Dashboard behavior

- Recent calls and contact profiles offer **Load older calls** in pages of 20.
- A call's existing `#calls/recent/<CallSid>` link loads that exact saved call,
  even after a restart or when it is outside the loaded pages.
- Transcript details, TXT/JSON exports, WAV playback/download, summaries, and
  saved AI markers are loaded by call ID. A missing call never silently opens
  another conversation. A missing/corrupt transcript does not hide valid audio.
- Contact counts, talk time, and last-contact dates use the whole caller catalog,
  independently of loaded pages. Partial storage failures are identified.
- Caller filters still include live sessions in the response so another
  caller's incoming notification can appear without changing the visible filter.

## API and storage

`GET /api/transcripts` accepts `cursor`, `caller` (E.164), and `call_sid`.
It retains the existing snapshot envelope and adds `history` with `next_cursor`,
`has_more`, `total`, `duration_seconds`, `last_contact_at`, `caller_metrics`, and
`complete`. Cursors use the last start-time/call-ID pair and are bound to the
caller filter. New arrivals do not shift an offset and skip older rows.

Only the selected call returns transcript segments and detailed detection timing.
Each page aligns its transcript, caller details, recording, and legacy voicemail
metadata. The selected call and live sessions can appear outside the page's 20
items. Catalog entries are deduplicated by call ID; totals do not count these
extra entries twice.

Catalogs enumerate saved files without the old first-1,000-entry scan cutoff.
Transcript headers are cached by file signature without retaining archive text
in memory; metadata catalogs refresh on a two-second cache. Individual reads
retain file-size, schema, and no-symlink protections. This remains a local-file
catalog rather than a database-backed historical index.

## Summary recovery

The post-call worker also rotates through archived transcripts, reading at most
20 older documents per poll. Detailed and brief summaries retain their existing
durable retry counts. In-flight results are fingerprint-checked before saving.
The summary and retry CLI commands accept an explicit older call ID.

Automated coverage includes restart/eviction, 45-call paging, equal timestamps,
new arrivals between pages, full caller metrics, direct exports/audio, old AI
evidence, unreadable storage, and archived summary retry recovery. Browser checks
use existing saved calls without placing calls or generating provider audio.
