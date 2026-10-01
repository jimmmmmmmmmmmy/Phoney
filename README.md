```sh
.venv/bin/python scripts/open_dashboard.py
```

# Phoney

The future of voicemail, onhold, calls

**GPT and ChatGPT plugin migration:** [research and feature coverage](docs/CHATGPT_PLUGIN_RESEARCH.md) · [five-phase systems design and build plan](docs/CHATGPT_PLUGIN_BUILD_PLAN.md). Proposed work; the live system is unchanged.

**Native phone pilot:** [setup and phone controls](docs/NATIVE_CONFERENCE.md).
The outbound conference pilot defaults off and retains transcription, detection,
and a separate AI participant. [Browser SDK calling](docs/BROWSER_CALLING.md) can
be enabled independently for dashboard calls. Incoming calls keep their current path.

**Tests:** [99 collected cases and the complete test command](docs/TESTING.md).

**Shared workspace privacy:** optional local PostgreSQL storage and a server-enforced six-digit PIN gate are available for the MVP's one shared workspace. Three incorrect PIN attempts require the recovery password; remembered devices skip the PIN for 30 days. Dashboard APIs, transcripts, recordings, and downloads require the same session when enabled. Search-index exclusion headers are included; signed service callbacks retain their existing authentication. Existing SQLite data is copied only through the verified, opt-in migration tool. See [workspace storage and setup](docs/WORKSPACE_STORAGE.md#shared-pin-and-remembered-phones). Live activation requires local credential setup and the database cutover.
