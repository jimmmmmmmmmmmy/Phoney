# Persistent contacts and agent drafts

Set `WORKSPACE_STORAGE_DIR` to an absolute private directory outside the source checkout and deployment releases. The dashboard saves contacts, edits to sample contacts, and agent drafts to SQLite there. New Cloudflare URLs, browser sessions, app restarts, and deployments use the same records as long as this server and storage directory remain in place.

```dotenv
WORKSPACE_STORAGE_DIR=/absolute/private/path/to/workspace
```

For the installed Mac service, configure `~/Library/Application Support/NewCollegeOperator/.env`; the source checkout has a separate environment. Keep the directory private (0700) and database files private (0600). Runtime records must not be committed to Git. Empty or unavailable storage returns a visible error; the dashboard does not report a browser-only save as successful.

## Import existing browser saves

Open the updated dashboard in the browser and at the URL where the records were originally saved, before retiring that tunnel URL. It automatically imports both contact records and agent drafts, retaining the original localStorage data as a backup. Imports are atomic and safe to retry: existing server IDs and matching contact phone numbers are retained, and legacy agent drafts receive stable IDs. Reopening an old browser cannot overwrite newer server edits with stale local data.

After import, open the new dashboard URL and the same records load from the server. Another browser's old localStorage cannot be read from a new origin. If an old URL has already stopped working before import, its browser data needs a separate recovery/export step. Opening `dashboard.html` directly with `file://` is not a connected dashboard; use the server's `/dashboard` URL.

## API and durability

`GET /api/workspace` returns `{version: 1, contacts, demoOverrides, agents}`. Save individual records through `PUT /api/workspace/contacts/{id}` or `PUT /api/workspace/agents/{id}`. `POST /api/workspace/import` accepts the three legacy arrays and returns the resulting snapshot. Writes require JSON, `X-Workspace-Request: 1`, and a matching request/public origin. Invalid records fail validation; duplicate contact phone numbers conflict. Each contact and draft includes a positive integer `revision`; include that revision in an update. New records use `revision: 0` or omit it. Updating an existing record without its current revision returns HTTP 409. Successful edits increment the revision within the same transaction as the write, so simultaneous stale edits cannot overwrite one another. Legacy saved records without a revision are read as revision 1; their IDs and data stay unchanged. Imports still retain existing records and do not overwrite them.

## Shared browser refresh and conflicts

Visible dashboards refresh contacts and agent settings every five seconds, and immediately on focus, reconnection, or return to a visible tab. Unchanged responses do not rebuild the lists. Refresh preserves active search, scroll, keyboard focus, and open form values. Network failures keep the last known records visible and retry automatically.

An editor keeps the revision that was opened, even when another browser changes the record. A conflict leaves the draft on screen, refreshes the saved list, and explains how to recover: copy any draft text you need, close the editor, and reopen it to review the latest saved version. Saving again without that review cannot overwrite the other browser’s work.

Published phone agents use their separate registry revision: `PUT /api/agents/{id}` requires `expectedRevision: 0` for a new agent or the current `revision` for an edit. The check and publication are atomic. Browser saves never change a revision already pinned by a live call. The existing local administration/CLI publication path remains intentional, rather than pretending to be an optimistic browser edit.

This remains the existing shared dashboard: anyone with its URL can access the workspace. Origin checks prevent cross-site browser form submissions; they do not provide user authentication. Legacy workspace agents are drafts; the phone-agent registry publishes settings for subsequent activations. Saving settings never initiates takeover.

## Notification history

Call notifications and their read state are stored in the same private workspace database, independent of browser origin. The bell restores the latest 20 calls after reload; up to 100 entries are retained. Initial saved history is marked read; live calls and calls arriving afterward are unread. Read status is shared across this demo workspace. Opening the bell acknowledges only the visible notification IDs, so a concurrent incoming call cannot be accidentally marked read.

`GET /api/notifications` reconciles the inbox from server call headers, including calls saved while no dashboard was open. `POST /api/notifications/read` takes `{ids: [...]}` and uses the same origin/JSON guard as workspace edits. Entries link directly to archived calls and voicemail. An unavailable archive preserves previous notification history and displays a reconnecting message when the inbox request fails.

Back up the SQLite database using SQLite's backup API, or copy the directory while the app is stopped. Keep backups outside deployment releases. URL changes are independent of storage; loss of this server's disk requires a backup restore.
