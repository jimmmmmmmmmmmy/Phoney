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

`GET /api/workspace` returns `{version: 1, contacts, demoOverrides, agents}`. Save individual records through `PUT /api/workspace/contacts/{id}` or `PUT /api/workspace/agents/{id}`. `POST /api/workspace/import` accepts the three legacy arrays and returns the resulting snapshot. Writes require JSON, `X-Workspace-Request: 1`, and a matching request/public origin. Invalid records fail validation; duplicate contact phone numbers conflict. Per-record transactions prevent unrelated edits from overwriting one another.

This remains the existing shared dashboard: anyone with its URL can access the workspace. Origin checks prevent cross-site browser form submissions; they do not provide user authentication. Agent records are drafts only and do not activate providers or call routing.

Back up the SQLite database using SQLite's backup API, or copy the directory while the app is stopped. Keep backups outside deployment releases. URL changes are independent of storage; loss of this server's disk requires a backup restore.
