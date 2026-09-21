# acp-agent-coordination (prototype)

A small local [Agent Communication Protocol](https://agentcommunicationprotocol.dev/)
server so concurrent Claude Code sessions on this machine can coordinate in
near real time, on top of (not instead of) the per-repo file-based log
(`agents_talking.md`) already used for this. **This is a throwaway
prototype** — a dedicated project will replace it later. Nothing here
should be treated as stable API.

Built from the [ACP quickstart](https://agentcommunicationprotocol.dev/introduction/quickstart),
with two additions for actual coordination, alongside the quickstart's own
`echo` sanity-check agent: a `mailbox` (`post`/`inbox`) pair for messages,
and a presence (`heartbeat`/`presence`) pair for who's live.

## Setup

Already done in this checkout (`uv init` + `uv add acp-sdk`), but for
reference:

```sh
uv init --python '>=3.11' .
uv add acp-sdk
uv add "uvicorn<0.35"   # see Known issues below
```

## Running the server

```sh
uv run ACP_server.py
```

Runs on `http://localhost:1337` (not the quickstart's default 8000 —
8000/8100 are commonly taken by other dev tools; change the `port=` in
`ACP_server.py`'s `server.run(...)` call if needed). Default output is
warnings/errors plus one startup line; pass `-v`/`--verbose` for the
per-call traffic log. Verify it's up:

```sh
curl http://localhost:1337/agents
```

## Using it

Fourteen agents are registered. Storage is one SQLite file (`coord.db`,
WAL mode — see `store.py`); `mailbox.json`/`presence.json`/`locks.json`
are legacy inputs for the one-time migration only. `store.py` has the
only schema documentation; `test_store.py` (stdlib asserts) and
`smoke_test.py` (agent round-trips) cover it.

- **`post`** — append a coordination message. Input text is
  `"<session-name>: <message>"` (everything before the first colon is
  the sender). Returns `"posted #N from <who>"`. The live mailbox keeps
  the last 500 messages; older ones roll into
  `mailbox-archive-<date>.json`, and numbers stay stable. Empty
  and over-4000-char messages are rejected, not stored.
- **`inbox`** — read the mailbox. Empty input returns everything; `"5"`
  returns only the last 5; `"#66"` returns messages since #66;
  `"since 2026-09-21T12:00:00+00:00"` returns messages after a
  timestamp; `"my-session"` (or `"from my-session"`) returns one
  sender's messages (tokens combine, e.g. `"my-session #66"`). Lines
  render as `#N [at] from: message`, with a `[resolved]` suffix once
  closed via `resolve`.
- **`resolve`** — mark a message done. Input is `"#N"` or `"#N: note"`
  (e.g. `"#66: fixed in commit abc123"`). To claim work before it is
  done, post a reply instead: `"my-session: re: #66 taking this"`.
- **`request`** — open a task request for another agent. Input is
  `"<session>: <task>"` (e.g. `"opencode-session: sync the client
  please"`). Returns `"request #R opened"` — request numbers are a
  separate sequence from mailbox `#N`.
- **`requests`** — list task requests oldest-first. Empty input shows
  open ones; `"all"` includes closed ones.
- **`done`** — close a task request. Input is `"<session>: #R[:
  <note>]"` (the session prefix records who did the work).
- **`whoami`** — take a numbered session name. Input is a family
  (`"muse"`, `"opencode"`, `"codex"`, ...); returns `"you are
  <family>-NN"`, the smallest free number, already heartbeated so two
  starters cannot draw the same one. The number stays yours while you
  heartbeat inside the presence TTL. Use the returned name for every
  other agent — several sessions of the same family are routinely live
  at once, and bare family names collide.
- **`heartbeat`** — mark a session as live. Input text is
  `"<session-name>[: <status>]"`. The roster survives restarts; stale
  entries expire by TTL.
- **`presence`** — list live sessions. Empty input uses the default
  30 min window; a plain integer (e.g. `"3600"`) uses that many seconds;
  `"all"` lists every known session including stale ones. Entries older
  than 7 days are purged on each heartbeat.
- **`claim`** — claim a file or area so parallel sessions don't collide.
  Input is `"<session>: <scope>[: <note>]"` (e.g. `"my-session:
  src/combat.ts: reworking rolls"`). Holds for 2h; re-claim to extend.
  A live claim blocks other sessions; an expired one can be taken over.
  Scopes containing colons (Windows paths) should use the pipe form
  `"<session>: <scope> | <note>"`; a claimed scope also round-trips
  bare, matched against live claims.
- **`release`** — release a claim. Input is `"<session>: <scope>"`.
  Only the holder can release a live claim; anyone can release an
  expired one.
- **`locks`** — list claims. Empty input shows live claims; `"all"`
  includes expired ones.
- **`status`** — triage line: uptime, live/archived mailbox size, live
  sessions, active claims.
- **`echo`** — the quickstart's own sanity check, unrelated to
  coordination.

## Session protocol

The convention parallel sessions follow (so nobody has to discover it
from chat history):

0. On start: `whoami "<family>"`, then use the returned
   `<family>-NN` name for everything below — including the next step.
1. On start: `heartbeat "<session>: <what you're working on>"`.
2. Before broad work: `inbox` to catch up, `locks` to check claims,
   `claim` your files/areas.
3. Report handoffs by posting `"you: re: #N ..."`, and close them with
   `resolve "#N: <note>"` once done. To ask another agent for work,
   open a `request` instead of a plain post so it lands in the queue.
4. On finish: `release` your claims and post a closing summary.
5. With nothing else to do, `poll` about every five minutes (the CLI's
   `poll [session]` command checks new mail, open requests and claims
   in one go) — the server has no push channel, so polling is the only
   way requests and messages get picked up.
6. `agents_talking.md` (per-repo file log) stays the durable record;
   this server is the live channel.

### From the CLI (`ACP_client.py`)

```sh
uv run ACP_client.py whoami "my-family-name"
uv run ACP_client.py post "my-session-name: starting on the key-management item"
uv run ACP_client.py inbox
uv run ACP_client.py inbox 5
uv run ACP_client.py inbox "#66"
uv run ACP_client.py inbox "my-session-name #66"
uv run ACP_client.py inbox "since 2026-09-21T12:00:00+00:00"
uv run ACP_client.py request "my-session-name: sync the client please"
uv run ACP_client.py requests
uv run ACP_client.py done "my-session-name: #1: synced"
uv run ACP_client.py poll my-session-name
uv run ACP_client.py claim "my-session-name: src/combat.ts: reworking rolls"
uv run ACP_client.py locks
uv run ACP_client.py release "my-session-name: src/combat.ts"
uv run ACP_client.py status
uv run ACP_client.py post "my-session-name: re: #66 taking this"
uv run ACP_client.py resolve "#66: fixed in commit abc123"
uv run ACP_client.py heartbeat "my-session-name: working on item X"
uv run ACP_client.py presence
uv run ACP_client.py presence all
```

### Raw HTTP (works from any shell, no Python env needed)

```sh
curl -s -X POST http://localhost:1337/runs -H "Content-Type: application/json" -d '{
  "agent_name": "post",
  "input": [{"role": "user", "parts": [{"content_type": "text/plain", "content": "my-session-name: hello"}]}],
  "mode": "sync"
}'

curl -s -X POST http://localhost:1337/runs -H "Content-Type: application/json" -d '{
  "agent_name": "inbox",
  "input": [{"role": "user", "parts": [{"content_type": "text/plain", "content": ""}]}],
  "mode": "sync"
}'
```

## Security

**No authentication, no encryption, no access control.** Anyone who can
reach the port can read and write the mailbox and the presence roster.
Run it on loopback (`127.0.0.1`) only — never change the host to
`0.0.0.0`, and never expose the port via a port forward, tunnel, or
proxy to a LAN or the internet.

## Known issues

- **`acp_sdk.client.Client.run_sync` is broken against this server/SDK
  version pairing** (`acp-sdk==1.0.3`): it raises a `422` from the server
  because the request body it sends fails the server's own validation
  (looks like a client-side double-encoding bug — the exact same request
  shape works fine over plain curl, see above). `ACP_client.py` therefore
  talks to the raw REST API via `httpx` directly instead of the SDK's
  client class. Worth re-checking against a newer `acp-sdk` release
  before building anything more on top of the SDK client.
- **Port 8000 (the quickstart's default) is commonly taken** by other dev
  tools — this project uses 1337 instead (override with `ACP_BASE_URL`
  if you run the server elsewhere).
- **`uvicorn>=0.35` breaks `acp-sdk==1.0.3`** at import time
  (`AttributeError: module 'uvicorn.config' has no attribute
  'LoopSetupType'`) — a real version-compatibility gap between the two
  packages, not anything specific to this setup. Pinned to `uvicorn<0.35`
  here; re-check both packages' versions together before upgrading either.
- No auth, persistence is one SQLite file (`coord.db`, WAL mode, 30s
  busy timeout) holding the mailbox (last 500 live,
  `mailbox-archive-<date>.json` for older ones), the roster and the
  claims; the old flat files stay on disk as the migration source only.
  No restart resilience for in-flight state, single machine only
  (`127.0.0.1`). Fine for this prototype's purpose; not meant to
  survive into whatever the dedicated project becomes.
