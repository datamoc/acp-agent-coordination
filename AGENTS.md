# AGENTS.md

## Coordination server (this repo)

- Start: `uv run ACP_server.py` (`-v` for the per-call traffic log) → `http://localhost:1337`. Verify: `curl http://localhost:1337/agents`.
- Loopback only: never bind `0.0.0.0` or expose the port — no auth (see README `## Security`).
- Optional HTTPS with `ACP_TLS_CERT`/`ACP_TLS_KEY` (client: `ACP_TLS_CA` or `ACP_TLS_INSECURE=1`) — see README `## HTTPS (optional)`. Still no auth; TLS only encrypts the transport.
- CLI: `uv run ACP_client.py <agent> [input]` (agents: `post`, `inbox`, `resolve`, `claim`, `release`, `locks`, `status`, `heartbeat`, `presence`, `echo`).

## Mailbox conventions

- Post as `"<session-name>: <message>"`; replies reference numbers (`"you: re: #66 ..."`).
- `inbox` lines look like `#66 [at] from: message`; catch up with `inbox #N`, `inbox <session>`, or `inbox <session> #N` (`inbox 5` = last 5).
- Claim work by posting `"you: re: #N taking this"`; mark done with `resolve "#N: <note>"`.
- Live mailbox keeps the last 5000; older entries archive to `mailbox-archive-<date>.json` (same dir, gitignored).
- Before broad work: `locks`, then `claim "<you>: <file-or-area>: <note>"` (2h hold, re-claim to extend); `release` when done. Expired claims can be taken over.
- Stay live with `heartbeat "<session>: <status>"`; check who's live with `presence`; triage with `status`.
- Full session protocol lives in README `## Session protocol`.

## Dev

- Smoke test: `uv run smoke_test.py` (temp-dir round-trip, no live server needed).
- TLS test: `uv run test_tls.py` (real subprocess, real HTTPS handshake, scratch port via `ACP_PORT` — never touches a live dev server on 1337; skips if `openssl` isn't on PATH).
- Runtime state (`mailbox.json`, `mailbox-archive-*.json`, `presence.json`, `locks.json`, `server*.log`) is gitignored; never commit it.
