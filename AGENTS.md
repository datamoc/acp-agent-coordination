# AGENTS.md

## Agent efficiency: token economy first

**Critical for coordination:** Any task automatable by a script should be done that way—not by spawning an agent. Every agent call costs tokens; in multi-agent coordination, spawning agents for automatable work wastes tokens on coordination overhead instead of the actual work.

**Scripts, not agents, for:** running tests • linting • building • git operations • file transformations • grepping/searching • data processing • environment setup • batch edits • log inspection.

**Agents for:** reasoning (diagnosis, design, code review) • judgment calls (architecture, API design, naming) • writing from scratch when shape is unclear • complex cross-file consistency.

**Token economy:** In coordination, ask before every agent spawn: "Is this automatable?" If yes, script it. If no, spawn the agent. See CLAUDE.md for the full principle and examples.

## Coordination server (this repo)

- Start: `uv run ACP_server.py` (`-v` for the per-call traffic log) → `http://localhost:1337`. Verify: `curl http://localhost:1337/agents`.
- Loopback only: never bind `0.0.0.0` or expose the port — no auth (see README `## Security`).
- Optional HTTPS with `ACP_TLS_CERT`/`ACP_TLS_KEY` (client: `ACP_TLS_CA` or `ACP_TLS_INSECURE=1`) — see README `## HTTPS (optional)`. Still no auth; TLS only encrypts the transport.
- CLI: `uv run ACP_client.py <agent> [input]` (agents: `post`, `inbox`, `resolve`, `claim`, `release`, `locks`, `status`, `heartbeat`, `presence`, `echo`).

## Coordination v2 (`coord`)

- `uv run coord.py whoami <family>` then `export COORD_SESSION=...`; `coord context` at session start, `coord poll` when idle.
- Claim with `coord claim <path>` (dir/ = tree), keep it while asking for help: `coord ask --claim C12 --to <session> "..."`.
- Long analyses go in `coord doc create`; debates in `coord discuss`/`propose`/`react`/`decide`. Full reference: README `## Coordination v2`.

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
- v2 test: `uv run test_coord.py` (temp dirs; multiprocess race, HTTP, OIDC, mTLS).
- TLS test: `uv run test_tls.py` (real subprocess, real HTTPS handshake, scratch port via `ACP_PORT` — never touches a live dev server on 1337; skips if `openssl` isn't on PATH).
- Runtime state (`mailbox.json`, `mailbox-archive-*.json`, `presence.json`, `locks.json`, `server*.log`, `coord2.db`, `.coord-session`, `pki/`) is gitignored; never commit it.
