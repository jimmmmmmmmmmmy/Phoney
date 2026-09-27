# Run this Mac through Cloudflare Tunnel

Run `python3 scripts/server.py status` to inspect the installed server, its public URL, and deployed revision.

The Python app runs on this computer at `http://127.0.0.1:8000`. A local `cloudflared` process creates an outbound connection to Cloudflare; public requests travel through that connection to the app. No router port forwarding or public IP address is required. Cloudflare provides the connection, while this Mac still executes the code and stores recordings. [Cloudflare Tunnel architecture](https://developers.cloudflare.com/tunnel/).

```text
Twilio / browser → public HTTPS or WSS → Cloudflare → cloudflared on this Mac → app :8000
GitHub main → local deployment supervisor → tested application release on this Mac
```

## Quick Tunnel behavior

`TUNNEL_PROVIDER=cloudflare` selects an accountless Quick Tunnel with a random `https://…trycloudflare.com` address. The address can change when the connector restarts. Cloudflare offers these tunnels for development, with no uptime SLA, a 200 concurrent in-flight request limit, and no Server-Sent Events (SSE). [Quick Tunnel contract](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/).

The dashboard uses ordinary HTTP polling, and Twilio Media Streams use WebSockets. Cloudflare Tunnel supports WebSockets, so the current protocol design does not require SSE. A working tunnel still needs the signed media and actual phone checks; protocol support alone is not an acceptance test. [WebSocket support](https://developers.cloudflare.com/cloudflare-one/faq/cloudflare-tunnels-faq/).

The helper owns its `cloudflared` process, records its identity in private runtime state, and checks its local readiness endpoint on `127.0.0.1:4041`. It writes connector logs to `.runtime/cloudflared.log`. Port `8000` remains the application's port; the metrics port is not the public web service. Application deployments and supervisor restarts reuse a healthy owned connector and its URL; restarting the connector itself obtains a new address.

If connector logs repeatedly report QUIC timeouts, set `TUNNEL_TRANSPORT_PROTOCOL=http2` in the runner's active `.env` and restart the connector after calls finish. Both runners read this setting. The default is explicitly `auto`, allowing Cloudflare's TCP fallback; Cloudflare Quick Tunnel otherwise defaults to QUIC in the current client. [Transport parameters](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/run-parameters/).

## Manual development

Allow about five minutes with the project's Python environment and Twilio credentials ready. Use one runner at a time: manual development and installed server mode both need port `8000`.

1. Install the connector with `brew install cloudflared`.
2. Set `TUNNEL_PROVIDER=cloudflare` in the source checkout's existing private `.env`. Preserve its credentials and storage settings; use `.env.example` only as a reference. When switching an existing manual runner, stop it with `python3 scripts/dev.py stop` after calls finish.
3. Start the app and connector with `python3 scripts/dev.py start`. The helper saves the discovered public origin as `PUBLIC_BASE_URL` in that `.env`.
4. Apply and verify both Twilio callbacks with `.venv/bin/python scripts/configure_twilio.py --apply`. These are `PUBLIC_BASE_URL/voice` and `PUBLIC_BASE_URL/status`, both POST. Repeat this step if a later manual restart changes the URL.
5. Run `python3 scripts/dev.py status` and open the displayed origin's `/dashboard`. The installed-server dashboard helper can point at a separate installed service, so use the manual runner's own displayed URL for this check.

Manual development has no deployment supervisor to repair webhook destinations automatically. `TUNNEL_PROVIDER=ngrok`, the default when unset, keeps the existing ngrok path available.

## Migrate the installed server

Allow about five to ten minutes for the controller update and release checks. Perform the maintenance restart after active calls finish. The installed service's environment is **`~/Library/Application Support/NewCollegeOperator/.env`**; editing the source checkout's `.env` does not change it.

1. Install `cloudflared` and ensure the Cloudflare-capable controller changes are published to the repository's `main` branch.
2. Set `TUNNEL_PROVIDER=cloudflare` in the installed service's existing `.env`. Keep all other values, including credentials and absolute storage paths.
3. Run `python3 scripts/server.py stop`, then `python3 scripts/server.py install` from the project checkout. Installation updates the clean controller checkout from GitHub and preserves its existing `.env`; it does not replace that environment with `.env.example`.
4. Run `python3 scripts/server.py status` until the app and connector are healthy and the intended revision is active. Open the dashboard with `.venv/bin/python scripts/open_dashboard.py`.
5. Verify the public `/health`, dashboard, Twilio callback read-back, and signed media path before repeating the phone acceptance test. The installer or a healthy local process alone does not prove public routing works.

The running supervisor starts or recovers the selected connector, saves `PUBLIC_BASE_URL`, and reconciles Twilio's incoming-voice and caller-status callbacks plus the GitHub push webhook. A changed public URL also requires the application to load that URL for Twilio signature validation; the supervisor uses its call-aware restart path. An unexpected tunnel outage can still interrupt media or callbacks before recovery.

Signed GitHub push events remain the fast path, with a 30-second `main` polling fallback. Each application revision must pass installation, tests, and health checks. A normal application deployment does not replace the long-running controller scripts; changing those scripts requires the reinstall above. See [SERVER.md](SERVER.md) for deployment and recovery details.

## Availability and a stable address

Keep the Mac powered, online, and logged in. The existing login service uses `caffeinate -i` to inhibit idle sleep while running. Closing a laptop lid, explicitly sleeping, logging out, or shutting down can make the service unreachable. Cloudflare does not keep the Python app running when the Mac is unavailable.

The runner supports a stable hostname through a locally managed named Cloudflare Tunnel. Set both `CLOUDFLARE_TUNNEL_CONFIG` and `CLOUDFLARE_PUBLIC_URL` with `TUNNEL_PROVIDER=cloudflare`; leaving both named settings empty selects the existing Quick Tunnel behavior. A named tunnel keeps the hostname across connector restarts. The Mac still runs the application. [Named tunnel setup](https://developers.cloudflare.com/tunnel/get-started/).

## Use phoney.dev with a named tunnel

Allow about ten minutes once Cloudflare account access is available. Update the installed controller with these runner changes before applying the new environment settings. Normal application deployment does not update the controller.

1. Authenticate the local connector with `cloudflared tunnel login`, create the tunnel with `cloudflared tunnel create phoney`, and route the domain with `cloudflared tunnel route dns phoney phoney.dev`. Reuse the intended existing tunnel instead of creating a duplicate. The DNS route must point `phoney.dev` at that tunnel.
2. Save a private YAML configuration outside release directories, for example `~/Library/Application Support/NewCollegeOperator/.runtime/cloudflare/phoney.yml`. Use the tunnel UUID and the absolute credentials file path returned by tunnel creation:

   ```yaml
   tunnel: YOUR-TUNNEL-UUID
   credentials-file: /Users/YOUR-USER/.cloudflared/YOUR-TUNNEL-UUID.json
   ingress:
     - hostname: phoney.dev
       service: http://127.0.0.1:8000
     - service: http_status:404
   ```

   Keep the directory private (`700`) and the configuration and credentials files private (`600`). Credentials remain in the JSON file; do not put tunnel tokens or credential contents in command arguments, `.env`, Git, or logs. The runner passes an explicit configuration path, so unrelated default Cloudflare configuration is preserved.
3. After publishing the runner changes to `main`, run `python3 scripts/server.py install` from the project checkout to update the clean installed controller. It preserves the existing service `.env` and healthy owned app/connector processes. Then set these values in `~/Library/Application Support/NewCollegeOperator/.env`, keeping the existing credentials and storage paths:

   ```dotenv
   TUNNEL_PROVIDER=cloudflare
   TUNNEL_TRANSPORT_PROTOCOL=http2
   CLOUDFLARE_TUNNEL_CONFIG="/Users/YOUR-USER/Library/Application Support/NewCollegeOperator/.runtime/cloudflare/phoney.yml"
   CLOUDFLARE_PUBLIC_URL=https://phoney.dev
   ```

   Use an existing absolute regular file for the config, not a symlink. The public URL must be an HTTPS hostname origin without credentials, port, trailing slash, path, query, or fragment. The config must publish that hostname to the app; the fixed URL setting does not create a Cloudflare DNS route.
4. The supervisor reads the updated environment on its next check, closes new-call admission, and waits up to 60 seconds for calls and pending work to finish before replacing the connector. An active call defers the change and reopens admission for an automatic retry. It validates ingress before stopping the current connector, checks ownership of the new connector's readiness port, saves `PUBLIC_BASE_URL=https://phoney.dev`, reloads the application with that origin, and updates the Twilio and GitHub callbacks. `python3 scripts/server.py status` should then display the fixed URL. Explicit `server.py stop` ends calls; use it only after calls finish if a maintenance stop is needed.
5. Verify `https://phoney.dev/health`, open `https://phoney.dev/dashboard#calls/recent`, and read back Twilio `/voice` and `/status` plus GitHub `/github/webhook` destinations. Repeat the signed callback/media checks and a real phone call. Connector readiness alone does not verify the DNS route, certificate, public hostname, or live media.

The runner records the configuration path, a content hash, protocol, and hostname with its owned process. A Quick Tunnel cannot be reused as a named tunnel, and configuration changes trigger the same call-aware replacement. Changes to a referenced credentials file alone require a maintenance connector restart; the runner does not copy or inspect its secret contents. To return to a Quick Tunnel, remove both named settings; the supervisor drains and replaces the connector, then reconciles the new random URL and callbacks. A default Cloudflare `config.yml`/`config.yaml` still prevents Quick Tunnel startup, so keep the named configuration at its explicit custom path.

The sharing scope stays the same: anyone with the public URL can see caller details, summaries and transcripts and play/download finalized local WAVs. Twilio signatures, GitHub signatures, and deployment-control authentication remain required on their existing routes. Tunnel selection does not alter Gemini billing; its depleted-credit blocker is documented in [CALL_SUMMARIES.md](CALL_SUMMARIES.md).

Verified on 2026-09-26: public health/dashboard/transcript routes returned 200, the dashboard rendered in a browser, Twilio and GitHub destinations matched by API read-back, signed HTTP callbacks reached application validation, and an unknown-session WebSocket handshake was rejected by the app through Cloudflare. The installed service uses HTTP/2 because QUIC timed out on this network. All 598 automated tests pass. A real phone call through Cloudflare remains pending; the rejection check does not establish live audio streaming.
