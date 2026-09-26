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

For a stable public hostname, create a named Cloudflare Tunnel using a Cloudflare account and a domain on Cloudflare, with a published application route to `http://127.0.0.1:8000`. The current `cloudflare` runner is specifically the Quick Tunnel integration; named-tunnel credentials and fixed-hostname lifecycle need a separate runner configuration before switching. [Named tunnel setup](https://developers.cloudflare.com/tunnel/get-started/).

The sharing scope stays the same: anyone with the public URL can see caller details, summaries and transcripts and play/download finalized local WAVs. Twilio signatures, GitHub signatures, and deployment-control authentication remain required on their existing routes. Tunnel selection does not alter Gemini billing; its depleted-credit blocker is documented in [CALL_SUMMARIES.md](CALL_SUMMARIES.md).

Cloudflare migration and real-phone acceptance are not established by this guide. Record the actual public HTTP/WebSocket checks and phone result when completed; do not infer them from the prior ngrok test results.
