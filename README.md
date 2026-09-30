```sh
.venv/bin/python scripts/open_dashboard.py
```

# Phoney

The future of voicemail, onhold, calls

**Shared workspace privacy:** optional local PostgreSQL storage and a server-enforced six-digit PIN gate are available for the MVP's one shared workspace. Three incorrect PIN attempts require the recovery password; remembered devices skip the PIN for 30 days. Dashboard APIs, transcripts, recordings, and downloads require the same session when enabled. Search-index exclusion headers are included; signed service callbacks retain their existing authentication. Existing SQLite data is copied only through the verified, opt-in migration tool. See [workspace storage and setup](docs/WORKSPACE_STORAGE.md#shared-pin-and-remembered-phones). Live activation requires local credential setup and the database cutover.
