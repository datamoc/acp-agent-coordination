# AGENTS.md

## Agent efficiency: scripts before agents

**Any task automatable by a script, command, or tool should be done that way—not by spawning an agent.** Agents are expensive (tokens + latency); reserve them for reasoning, judgment calls, and complex multi-step work that requires understanding.

**Use scripts for:** testing, linting, building, git operations, file transformations, searching/grepping, data processing, environment setup.

**Use agents for:** bug diagnosis, system design, code review, architecture decisions, writing new code when the shape is unclear, complex multi-file changes requiring cross-file consistency.

When in doubt: before spawning an agent, ask "can I do this in 5 lines of Python?" If yes, do that instead. See CLAUDE.md for details.

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
