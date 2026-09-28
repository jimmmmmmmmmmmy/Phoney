```sh
ssh macmini 'cd "$HOME/Library/Application Support/NewCollegeOperator" && .venv/bin/python scripts/server.py status'
```

**Permanent production host: Mac mini.** `https://phoney.dev` reaches the mini; the MacBook's old login service is disabled after migration. The mini uses `APP_PORT=18000` with candidate port `18001` because other services already occupy `8000` and `8001`. Run the remaining local server commands below on the mini over SSH. See [network resilience and migration storage](NETWORK_RESILIENCE.md).

Run `python3 scripts/server.py status` from a project checkout on the server Mac to check the service location, supervisor, tunnel, and deployed revision. The source repository is [jimmmmmmmmmmmy/fictional-rotary-phone](https://github.com/jimmmmmmmmmmmy/fictional-rotary-phone); the deployment branch is `main`.

The installed service lives at `~/Library/Application Support/NewCollegeOperator`, independently of your editable project folder. Its logs, releases, and configuration live there. The project's private `.runtime/server-root.json` records that location so `scripts/server.py` commands continue to manage the installed service from the project.

## Publish a change

1. Edit and test your change in your own checkout.
2. Commit the intended files and run `git push origin main`.
3. On the server Mac, run `python3 scripts/server.py status` and compare the active revision with the commit you pushed.

To compare GitHub directly with the running application from this Mac:

```sh
git ls-remote https://github.com/jimmmmmmmmmmmy/fictional-rotary-phone.git refs/heads/main
curl --silent http://127.0.0.1:8000/health
```

The examples use the default app port `8000` and candidate port `8001`. Substitute `18000` and `18001` for the permanent mini installation. `APP_PORT` accepts an integer from 1024 through 65534, excluding overlap with tunnel ports 4040/4041; its candidate always uses the next port. A named Cloudflare ingress configuration must target the same app port. Changing a running app's port requires stopping the service after calls finish and restarting it; the supervisor refuses to replace a live app through the wrong control endpoint.

The GitHub SHA and the health response's `commit` value must match after deployment finishes. A different SHA during `preparing` means the previous healthy app is still serving while the candidate is checked.

GitHub sends a signed push event to `PUBLIC_BASE_URL/github/webhook`. The receiver queues the event; while idle, the deployment supervisor checks the queue every second. It also fetches `main` at 30-second intervals between checks, so missed webhook deliveries do not require a manual pull. These are detection intervals: dependency installation, tests, and startup take additional time. Several closely spaced pushes may be combined into one deployment of the latest `main`.

Each candidate gets its own checkout and Python environment under the installed service's `.runtime/deploy/releases/<commit>`. The supervisor installs `requirements-lock.txt`, or `requirements-dev.txt` when the lock file is absent, runs `pytest`, and starts a candidate on local port `8001`. A candidate that fails preparation leaves the current app running. Health checks require `status: "ok"` and a `commit` matching `DEPLOY_COMMIT`. Activation moves the new release to port `8000`; failed activation restores the previous release. Switching the app can briefly interrupt new requests. For a configured Build 1 or later, the supervisor closes admission to new calls and waits up to 60 seconds for both active sessions and outstanding call/capture work to reach zero. If calls remain, it reopens admission, retains the old revision, and retries automatically. A `waiting` state is a deferred deployment, not a failed commit.

**The running application code updates automatically from GitHub.** The editable project checkout stays in place; automatic deployments do not pull into it or reset uncommitted work. Keep code changes in Git and secrets in `~/Library/Application Support/NewCollegeOperator/.env` after installation.

## Grant teammate access

Having the public link lets someone read and clone the repository. GitHub requires an accepted collaborator invitation for them to push to this personal repository. Direct pushes to `main` also remain subject to any branch rules configured on GitHub. See [GitHub's personal repository permissions](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/repository-access-and-collaboration/permission-levels-for-a-personal-account-repository).

1. Open [repository access settings](https://github.com/jimmmmmmmmmmmy/fictional-rotary-phone/settings/access).
2. Choose **Add people**, enter the teammate's GitHub username, and send the invitation.
3. Ask them to accept it before pushing.

Grant write access only to trusted teammates. Their code, dependency installation, and tests run as the logged-in Mac user and can access that user's local files and credentials. The webhook signature proves GitHub sent the event; it does not make the code safe.

## Set up server mode

Installation takes about 2–5 minutes when Python 3.11+, a tunnel client, and the GitHub CLI (`gh`) are ready. Set `TUNNEL_PROVIDER=cloudflare` for the accountless Quick Tunnel with `cloudflared` installed; the default `ngrok` option needs an authenticated ngrok client. See [the Cloudflare guide](CLOUDFLARE.md) for migration and URL behavior. `gh auth status` must show an account with repository administration access for webhook setup. Installation and the first release need internet access to fetch code and dependencies.

1. Install the root environment:

   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements-lock.txt
   ```

2. Fill the project's local `.env` using `.env.example`. Preserve existing Twilio credentials. Set `DEPLOY_REPOSITORY` to `jimmmmmmmmmmmy/fictional-rotary-phone` and use a random `GITHUB_WEBHOOK_SECRET` of at least 32 characters. The installer sets `DEPLOY_TRIGGER_PATH` to the service's absolute `.runtime/deploy.trigger` path. For Build 1, also set `CALLEE_NUMBER` and a separate random `DEPLOY_CONTROL_TOKEN` of at least 32 characters; the supervisor uses that token for the private `/internal/deploy` endpoint. Keep `.env` out of Git.

3. Install the macOS service:

   ```sh
   python3 scripts/server.py install
   ```

4. Run `python3 scripts/server.py status`, then call the Twilio number from a phone different from `CALLEE_NUMBER`. For configured Build 1, the teammate phone rings; answer it and speak both ways. With no forwarding number configured, expect the team greeting and hangup.

The installer creates the separate service checkout and Python environment. On first installation it copies the project's `.env` privately and migrates existing runner and webhook metadata so an existing managed tunnel can be recovered, or a compatible ngrok tunnel reused. Reinstalling preserves the service's `.env`. Subsequent configuration changes belong in **`~/Library/Application Support/NewCollegeOperator/.env`**; editing the project's `.env` does not change the installed service's settings. After editing the active configuration, run `python3 scripts/server.py stop`, then `python3 scripts/server.py start` from the project to restart the application with those settings.

The service runs as the macOS LaunchAgent `com.newcollege.passive-operator`. It starts at login and restarts if its supervisor exits. It starts or recovers the local app and selected tunnel and updates both Twilio incoming-voice/caller-status webhooks and the GitHub push webhook when the public tunnel URL changes. Repository administration access is required to create or update the GitHub hook.

The hook configuration helper supports an explicit setup or repair:

```sh
cd "$HOME/Library/Application Support/NewCollegeOperator"
.venv/bin/python scripts/configure_github.py --apply
```

It creates or updates the repository's push webhook using the current `PUBLIC_BASE_URL` and the secret in the service's `.env`. It resolves the repository's current GitHub name before hook mutations, so the rename from `fictional-rotary-phone` to `Phoney` does not block tunnel URL repairs. Signed push events accept both project names. Run this helper after rotating `GITHUB_WEBHOOK_SECRET`, too. Running it without `--apply` inspects configuration. From the same service directory, the Twilio equivalent is `.venv/bin/python scripts/configure_twilio.py --apply`; it sets and verifies `PUBLIC_BASE_URL/voice` and `PUBLIC_BASE_URL/status`, both POST. From the source checkout, use `.venv/bin/python scripts/configure_twilio.py --env-file "$HOME/Library/Application Support/NewCollegeOperator/.env" --apply` to target the installed configuration explicitly.

## Operate and recover

| Action | Command |
| --- | --- |
| Inspect managed service | `python3 scripts/server.py status` |
| Start the installed service | `python3 scripts/server.py start` |
| Stop automatic deployment, app, and managed tunnel | `python3 scripts/server.py stop` |
| Stop and remove the login service | `python3 scripts/server.py remove` |
| Check the active application's health | `curl http://127.0.0.1:8000/health` |

A failed revision is not continuously rebuilt. After fixing a local cause, request a retry from the installed service directory:

```sh
cd "$HOME/Library/Application Support/NewCollegeOperator"
.venv/bin/python scripts/deploy.py retry
```

Alternatively, push a new commit. In the service directory, `.venv/bin/python scripts/deploy.py status` prints detailed deployment state. Keep status output and logs local; `.runtime/` is excluded from Git, as are `.env` and Python environments.

The following log paths are relative to `~/Library/Application Support/NewCollegeOperator`:

| Log | What it explains |
| --- | --- |
| `.runtime/server.log` and `.runtime/server-error.log` | Supervisor progress and service errors |
| `.runtime/deploy/build.log` | Fetching, dependency installation, tests, and webhook configuration |
| `.runtime/deploy/candidate.log` | Candidate startup and health failures |
| `.runtime/app.log` and `.runtime/cloudflared.log` or `.runtime/ngrok.log` | Active application and selected tunnel behavior |

Run `python3 scripts/server.py stop` to disable the supervisor and stop the managed app and selected managed tunnel process. Use `start` to resume. A pre-existing ngrok tunnel reused by the runner remains running. Run `python3 scripts/server.py remove` to stop the service and remove its LaunchAgent file; source files and runtime data remain on disk.

To undo a published application change, push a new commit that restores the intended code. The supervisor deploys that revision through the same dependency, test, and health checks. It does not rewrite GitHub history.

The supervisor uses deployment-management scripts from the installed service checkout. Changes to those scripts require a deliberate service update and reinstall; deploying application releases does not replace the running supervisor. After publishing an infrastructure change, rerun `python3 scripts/server.py install` from the project to update the clean service checkout from GitHub and reinstall it. The service's `.env` is preserved. Before updating your editable project checkout, finish or preserve local edits and confirm `git status` is clean.

## Keep the Mac available

The LaunchAgent uses `caffeinate -i` to prevent idle sleep while running. Keep the Mac powered, online, and logged in. Closing a laptop lid, explicitly sleeping it, shutting it down, or logging out can stop service availability. Login starts the service again; this is not a service that starts before a user logs in.

Twilio and GitHub reach this Mac through the selected Cloudflare or ngrok tunnel. When the Mac or tunnel is unavailable, calls cannot reach the app; the polling fallback discovers the latest `main` after the server returns. The public URL may change after tunnel restart, so the service reconciles both webhook destinations.

Build 1 is implemented; perform the real-phone checks in [BUILD_1.md](BUILD_1.md). Automatic updates preserve established calls through the authenticated drain protocol, including outstanding dialing/cleanup tasks. App shutdown gets a 40-second grace period. Explicitly stopping the service or a process crash still ends continuity: this milestone does not persist live conference sessions across restarts.

## Build 2 capture storage

Set `MEDIA_CAPTURE_ENABLED=true`, an absolute `MEDIA_STORAGE_DIR`, and optionally `MEDIA_MAX_SECONDS` (default 1800) in the installed service's `.env`. This Mac uses the service's `.runtime/recordings` directory, outside its release checkouts. To keep the two-human conference without recording, set both `TRANSCRIPTION_ENABLED=false` and `MEDIA_CAPTURE_ENABLED=false` before restarting. Build 3 requires capture while transcription is enabled; disabling only capture makes configuration invalid. The caller hears a recording notice when capture is enabled.

Capture workers count toward deployment draining until their WAV files are finalized. The supervisor launches Uvicorn with a 64 KiB WebSocket message limit. Updating those launch arguments requires the usual manager reinstall after pushing; application-only revisions still deploy automatically. Explicit service stop ends the call and attempts bounded capture finalization; a process crash can leave incomplete files. Only completed manifests are accepted by partner replay.

Read [BUILD_2.md](BUILD_2.md) for the phone test and [PARTNER_HANDOFF.md](PARTNER_HANDOFF.md) for the local audio interface. Captures have private filesystem permissions and are excluded from Git. Finalized `completed` and `partial` local WAVs are publicly playable/downloadable through the dashboard; those filesystem permissions do not make HTTP audio private. They persist until an operator removes them; deployment never deletes existing recordings.

## Public viewer and unanswered-call voicemail

Run `.venv/bin/python scripts/open_dashboard.py` from the source checkout to open the installed service's configured `PUBLIC_BASE_URL/dashboard`. The dashboard, transcript/recording APIs, and JSON/text/WAV downloads are public: anyone with the public tunnel URL can read available text and play/download finalized local recordings without signing in. Active recordings and Twilio cloud recording URLs are not served; provider keys stay server-side; existing Twilio signature checks, GitHub webhook signatures, and deployment-control authentication remain enabled.

Set `VOICEMAIL_ENABLED=true`, `VOICEMAIL_MAX_SECONDS=120`, and an absolute private `VOICEMAIL_STORAGE_DIR` in the installed service's private `.env` to record a message when the teammate does not connect. This option defaults to off in a new checkout and is enabled for the live setup. Apply environment changes during an idle period with the server controls above. Keep the signed `/status` incoming-call callback configured so hangup during the greeting cleans up promptly. Saved receipts outside the ten-row startup inbox can be restored for late signed recording callbacks after deployment. See [VOICEMAIL.md](VOICEMAIL.md) for callback contracts, storage boundaries, and the pending real-phone acceptance checks.

The public recording library reads finalized captures from the existing `MEDIA_STORAGE_DIR`; no extra audio codec or MP3/M4A conversion is required. The default stereo WAV is generated on request from caller-input and caller-playback files, with silence padding for unequal lengths and no third file stored. Browser seeking uses HTTP byte ranges. Catalog scans stop at 1,000 directory entries and return the newest ten valid captures within that scan; larger archives need retention/indexing work to guarantee newest-record discovery. Public playback accepts completed or partial finalized captures, while offline partner replay still requires completion. See [Build 3 playback](BUILD_3.md#play-or-download-a-finalized-recording) for the routes and labels.
