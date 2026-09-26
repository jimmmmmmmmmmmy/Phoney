```sh
python3 scripts/server.py status
```

Run this from the repository on the server Mac to check the supervisor, tunnel, and deployed revision. The source repository is [jimmmmmmmmmmmy/fictional-rotary-phone](https://github.com/jimmmmmmmmmmmy/fictional-rotary-phone); the deployment branch is `main`.

## Publish a change

1. Edit and test your change in your own checkout.
2. Commit the intended files and run `git push origin main`.
3. On the server Mac, run `python3 scripts/server.py status` and compare the active revision with the commit you pushed.

GitHub sends a signed push event to `PUBLIC_BASE_URL/github/webhook`. The receiver queues the event; while idle, the deployment supervisor checks the queue every second. It also fetches `main` at 30-second intervals between checks, so missed webhook deliveries do not require a manual pull. These are detection intervals: dependency installation, tests, and startup take additional time. Several closely spaced pushes may be combined into one deployment of the latest `main`.

Each candidate gets its own checkout and Python environment under `.runtime/deploy/releases/<commit>`. The supervisor installs `requirements-lock.txt`, or `requirements-dev.txt` when the lock file is absent, runs `pytest`, and starts a candidate on local port `8001`. A candidate that fails preparation leaves the current app running. Health checks require `status: "ok"` and a `commit` matching `DEPLOY_COMMIT`. Activation moves the new release to port `8000`; failed activation restores the previous release. Switching the app can briefly interrupt requests.

**The running application code updates automatically from GitHub.** The editable root checkout stays in place; automatic deployments do not pull into it or reset uncommitted work. Keep code changes in Git and secrets in the server's local `.env`.

## Grant teammate access

Having the public link lets someone read and clone the repository. GitHub requires an accepted collaborator invitation for them to push to this personal repository. Direct pushes to `main` also remain subject to any branch rules configured on GitHub. See [GitHub's personal repository permissions](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/repository-access-and-collaboration/permission-levels-for-a-personal-account-repository).

1. Open [repository access settings](https://github.com/jimmmmmmmmmmmy/fictional-rotary-phone/settings/access).
2. Choose **Add people**, enter the teammate's GitHub username, and send the invitation.
3. Ask them to accept it before pushing.

Grant write access only to trusted teammates. Their code, dependency installation, and tests run as the logged-in Mac user and can access that user's local files and credentials. The webhook signature proves GitHub sent the event; it does not make the code safe.

## Set up server mode

Allow about 5–10 minutes when Python 3.11+, authenticated ngrok, and the GitHub CLI (`gh`) are ready. `gh auth status` must show an account with repository administration access for webhook setup. The first release also needs internet access to install its dependencies.

1. Install the root environment:

   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements-lock.txt
   ```

2. Fill the local `.env` using `.env.example`. Preserve existing Twilio credentials. Set `DEPLOY_REPOSITORY` to `jimmmmmmmmmmmy/fictional-rotary-phone`, use a random `GITHUB_WEBHOOK_SECRET` of at least 32 characters, and set `DEPLOY_TRIGGER_PATH` to this repository's absolute `.runtime/deploy.trigger` path. Keep `.env` out of Git.

3. Stop the manual development runner if it is active:

   ```sh
   python3 scripts/dev.py stop
   ```

4. Install the macOS service:

   ```sh
   python3 scripts/server.py install
   ```

5. Run `python3 scripts/server.py status`, then call the Twilio number. For Build 0, expect **“New College Data Science Team”** and the call to end.

The service runs as the macOS LaunchAgent `com.newcollege.passive-operator`. It starts at login and restarts if its supervisor exits. It starts or recovers the local app and ngrok tunnel and updates the Twilio voice webhook and GitHub push webhook when the public tunnel URL changes. Repository administration access is required to create or update the GitHub hook.

The hook configuration helper supports an explicit setup or repair:

```sh
.venv/bin/python scripts/configure_github.py --apply
```

It creates or updates the repository's push webhook using the current `PUBLIC_BASE_URL` and the secret in `.env`. Running it without `--apply` inspects configuration. The Twilio equivalent is `.venv/bin/python scripts/configure_twilio.py --apply`.

## Operate and recover

| Action | Command |
| --- | --- |
| Inspect managed service | `python3 scripts/server.py status` |
| Start the installed service | `python3 scripts/server.py start` |
| Inspect deployment state | `.venv/bin/python scripts/deploy.py status` |
| Retry the current remote revision after fixing a local cause | `.venv/bin/python scripts/deploy.py retry` |
| Check the active application's health | `curl http://127.0.0.1:8000/health` |

A failed revision is not continuously rebuilt. Fix the cause and use `retry`, or push a new commit. Keep status output and logs local; `.runtime/` is excluded from Git, as are `.env` and Python environments.

| Log | What it explains |
| --- | --- |
| `.runtime/server.log` and `.runtime/server-error.log` | Supervisor progress and service errors |
| `.runtime/deploy/build.log` | Fetching, dependency installation, tests, and webhook configuration |
| `.runtime/deploy/candidate.log` | Candidate startup and health failures |
| `.runtime/app.log` and `.runtime/ngrok.log` | Active application and tunnel behavior |

Run `python3 scripts/server.py stop` to disable the supervisor and stop the managed app and managed ngrok process. Use `start` to resume. A pre-existing ngrok tunnel reused by the runner remains running. Run `python3 scripts/server.py remove` to stop the service and remove its LaunchAgent file; source files and runtime data remain on disk.

To undo a published application change, push a new commit that restores the intended code. The supervisor deploys that revision through the same dependency, test, and health checks. It does not rewrite GitHub history.

The supervisor uses deployment-management scripts from the root checkout. Changes to those scripts on GitHub require a deliberate update of that checkout and service reinstall; deploying application releases does not replace the running supervisor. Before updating the root checkout, finish or preserve local edits and confirm `git status` is clean.

## Keep the Mac available

The LaunchAgent uses `caffeinate -i` to prevent idle sleep while running. Keep the Mac powered, online, and logged in. Closing a laptop lid, explicitly sleeping it, shutting it down, or logging out can stop service availability. Login starts the service again; this is not a service that starts before a user logs in.

Twilio and GitHub reach this Mac through ngrok. When the Mac or tunnel is unavailable, calls cannot reach the app; the polling fallback discovers the latest `main` after the server returns. The public URL may change after tunnel restart, so the service reconciles both webhook destinations.

Build 1 still needs the implementation and real-phone checks in [BUILD_1.md](BUILD_1.md). Before enabling automatic deployment during live Build 1 calls, add graceful call draining or choose a deployment window: restarting an in-memory call controller can lose its session state.
