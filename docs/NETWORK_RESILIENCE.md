# Network changes and saved data

Open `https://phoney.dev` again after reconnecting. The named Cloudflare Tunnel keeps the same public address across network changes and connector restarts.

## What recovers automatically

- The dashboard retries failed reads. Calls, contacts, agents, and notifications refresh when the browser returns online or becomes visible; requests time out instead of holding polling open forever. Existing views, loaded data, and open forms stay in place.
- Failed saves remain unconfirmed and require another Save. Mutations are not blindly replayed after an ambiguous network failure.
- The deployment supervisor recovers the saved application release before waiting for the tunnel or GitHub, so a local restart can succeed while offline. A healthy application is preserved during a tunnel outage. Cloudflare reconnects through the existing connector; the supervisor restarts a dead connector.
- Contacts, agents, notifications, finalized recordings, transcripts, voicemail receipts, summaries, and detection results live in the configured storage directories outside application releases. Browser reconnection and normal deployments do not erase them.

## Availability depends on the server

Production's permanent host is the Mac mini, reachable with `ssh macmini`, under `~/Library/Application Support/NewCollegeOperator`. It serves the application on loopback port `18000` and tests candidate releases on `18001`, leaving the mini's existing services on `8000` and `8001` alone. Cloudflare forwards `https://phoney.dev` to the mini. The MacBook's server service is disabled after migration, so moving the MacBook or disconnecting its Wi-Fi does not stop the backend.

Keep the mini powered, logged in, and online. The login service prevents idle sleep and restarts its supervisor if it exits. The mini's own internet or power failure can still interrupt service. This is one permanent host, not a replicated cluster. Do not restart the old MacBook service alongside it: live sessions are process-local and the SQLite/filesystem stores are not replicated.

Migration preserves all six storage directories and `VOICE_OUTPUT_DIR`, including both `workspace.sqlite3` and `agent-execution.sqlite3`. Private migration snapshots remain on the MacBook under the project `.runtime/macmini-migration`. The old service data is retained as a migration backup; after production receives new writes on the mini, those old copies are no longer current. A later host move must drain calls, stop writers, transfer a fresh snapshot, and leave only one serving tunnel connector.

Check the permanent host from the MacBook:

```sh
ssh macmini 'cd "$HOME/Library/Application Support/NewCollegeOperator" && .venv/bin/python scripts/server.py status'
```

An established phone/media session cannot resume after its serving process crashes or its media connection is lost. Reconnecting the browser does not recreate that call. Previously finalized data remains available; the user must place a new call.

## Verification

The regression suites exercise offline local recovery, healthy-process preservation, changed tunnel origins, browser request timeouts, and reconnect refresh behavior. They do not disconnect production networking or establish carrier caller-ID presentation. After deployment, use a second phone to confirm original caller ID and two-way audio.

See [server operations](SERVER.md), [Cloudflare configuration](CLOUDFLARE.md), and [workspace backups](WORKSPACE_STORAGE.md).
