# Persistent contacts and agent drafts

Set `WORKSPACE_STORAGE_DIR` to an absolute private directory outside the source checkout and deployment releases. By default the dashboard saves contacts, edits to sample contacts, and agent drafts to SQLite there. `DATABASE_URL` selects local PostgreSQL instead. New Cloudflare URLs, browser sessions, app restarts, and deployments use the same records as long as their configured storage remains available.

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

With `WORKSPACE_ACCESS_ENABLED=false`, anyone with the URL can access the shared dashboard. Origin checks prevent cross-site browser form submissions; they do not provide user authentication. Enable the PIN gate below to require a server-verified session for dashboard pages, APIs, audio, downloads, and owner controls. Legacy workspace agents are drafts; the phone-agent registry publishes settings for subsequent activations. Saving settings never initiates takeover.

## Notification history

Call notifications and their read state are stored in the same private workspace database, independent of browser origin. The bell restores the latest 20 calls after reload; up to 100 entries are retained. Initial saved history is marked read; live calls and calls arriving afterward are unread. Read status is shared across this demo workspace. Opening the bell acknowledges only the visible notification IDs, so a concurrent incoming call cannot be accidentally marked read.

`GET /api/notifications` reconciles the inbox from server call headers, including calls saved while no dashboard was open. `POST /api/notifications/read` takes `{ids: [...]}` and uses the same origin/JSON guard as workspace edits. Entries link directly to archived calls and voicemail. An unavailable archive preserves previous notification history and displays a reconnecting message when the inbox request fails.

Back up the SQLite database using SQLite's backup API, or copy the directory while the app is stopped. Keep backups outside deployment releases. URL changes are independent of storage; loss of this server's disk requires a backup restore.

## Shared PIN and remembered phones

The MVP serves one shared workspace. All trusted people use the same six-digit PIN and a separate recovery password; no username or workspace picker is required. The unlock screen has a circular numeric keypad and masked PIN dots. After three incorrect PIN entries, that device requires the password. Refreshing or replacing cookies does not remove server-side source limits. Temporary source and workspace limits restrict distributed guessing; signed-in devices remain usable. The recovery password must contain at least 15 characters.

Remembering a device is checked by default. It creates a 30-day `Secure`, `HttpOnly`, `SameSite=Lax`, host-only cookie backed by a hashed database token. App/browser restarts preserve the session at the same HTTPS hostname. Unchecking Remember creates a browser-session cookie with a server-side 12-hour maximum. The dashboard's lock button revokes only that device; setting new credentials or the local revoke command invalidates all remembered devices for the workspace. Up to 64 active browser sessions are retained.

Configure credentials in an interactive terminal on the server, using the prepared environment file:

```sh
.venv/bin/python scripts/workspace_access.py set --env-file /absolute/private/workspace-postgres.env
```

The prompts hide both credentials. They are never passed as command arguments or stored in the browser. `status` checks setup; `revoke` ends all remembered sessions. The CLI reads the specified file rather than a release's unrelated environment. Setting credentials alone does not enable the gate: activate `WORKSPACE_ACCESS_ENABLED=true` with the vetted server configuration. Enabled access with missing credentials or unavailable storage fails closed.

The gate covers the website and data routes, including direct audio and transcript requests. Signed Twilio HTTP/media callbacks, the separately authenticated GitHub/deployment endpoints, and health checks remain functional. Shared workspace access also authorizes the existing owner controls. All HTTP responses carry `X-Robots-Tag: noindex, nofollow, noarchive`. Crawlers can read that instruction at the unlock page; authentication keeps private content inaccessible regardless of crawler compliance. The hostname itself can still be discovered.

## Move both SQLite stores to local PostgreSQL

`DATABASE_URL` must identify local PostgreSQL over loopback or a Unix socket. A dedicated non-superuser database owner is sufficient. Bind PostgreSQL locally; phones reach the Python app through `https://phoney.dev` rather than connecting to the database.

Use a private staging environment containing the target `DATABASE_URL`, the existing `WORKSPACE_STORAGE_DIR`, and `WORKSPACE_ID=default`. Keep the active app configuration on SQLite until verification succeeds.

1. Provision an empty local PostgreSQL database and stage its private configuration. Keep the existing SQLite files and call archive available.
2. Inspect the migration without changing the source or target:

   ```sh
   .venv/bin/python scripts/migrate_postgres.py --env-file /absolute/private/workspace-postgres.env
   ```

3. After maintenance approval, drain calls and stop all application writers. Back up both SQLite files. Repeat the inspection and copy into the empty target:

   ```sh
   .venv/bin/python scripts/migrate_postgres.py --env-file /absolute/private/workspace-postgres.env --apply
   ```

4. Set the PIN and recovery password with the interactive command above. Add `DATABASE_URL`, `WORKSPACE_ID=default`, and `WORKSPACE_ACCESS_ENABLED=true` to the installed service's active environment.
5. Activate the vetted release and restart during the maintenance window. Verify anonymous APIs/downloads reject access, a correct PIN unlocks the dashboard, the phone remains signed in after restart, and signed phone callbacks still work.

The migration copies contacts, drafts, notifications/read state, published revisions/current agents, voices, owner grants/sessions, clone receipts, and setup records. It preserves IDs, revisions, payloads, and contact/draft ordering; checks every copied row and table count before committing; refuses nonempty targets; and leaves SQLite sources unchanged. The dry run is enforced as a PostgreSQL read-only transaction. New shared PIN credentials are configured separately after the copy. Transcripts, call details, detection results, voicemail files, and recordings remain at their existing filesystem locations.

Do not delete SQLite backups after cutover. They represent the pre-cutover state; reverting after new PostgreSQL writes requires reconciling those new records. Back up PostgreSQL and the separate call archive independently.

## Future workspace and phone-number foundation

`WORKSPACE_ID` is stable and defaults to `default`. PostgreSQL keeps contact/agent/notification data in a separate schema for that ID; credentials, sessions, and attempt guards are keyed by workspace. A catalog records the workspace and the configured Twilio phone number. Database locks preserve the existing import, revision, and duplicate guarantees across server processes.

This release still assumes one workspace, one configured Twilio number, and one call archive. It does not provide workspace selection, per-person accounts, additional-number provisioning, or multi-workspace call/media routing. Those roadmap features require routing and archive ownership work before another workspace is exposed.
