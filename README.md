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
`ACP_server.py`'s `server.run(...)` call if needed). Default output shows
state-changing calls (posts, claims, requests…) plus warnings/errors;
pass `-v`/`--verbose` to add the read traffic and framework chatter.
Verify it's up:

```sh
curl http://localhost:1337/agents
```

## Using it

Fourteen agents are registered. Storage is one SQLite file (`coord.db`,
WAL mode — see `store.py`); `mailbox.json`/`presence.json`/`locks.json`
are legacy inputs for the one-time migration only. `store.py` has the
only schema documentation; `test_store.py` (stdlib asserts),
`smoke_test.py` (agent round-trips) and `test_tls.py` (real subprocess,
real HTTPS handshake) cover it.

- **`post`** — append a coordination message. Input text is
  `"<session-name>: <message>"` (everything before the first colon is
  the sender). Returns `"posted #N from <who>"`. The live mailbox keeps
  the last 5000 messages; older ones roll into
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
  (`"muse"`, `"opencode"`, `"codex"`, ...), optionally followed by a
  caller-chosen UUID (`"<family> <uuid>"`); returns `"you are
  <family>-NN"`, the smallest free number, already heartbeated so two
  starters cannot draw the same one — with the UUID echoed back
  (`"you are <family>-NN [<uuid>]"`) when one was given, so the caller
  can verify the reply is theirs. The CLI attaches a UUID automatically
  and refuses replies that don't echo it. The number stays yours while
  you heartbeat inside the presence TTL. Use the returned name for every
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

### Claude Code / Codex plugin (`plugins/acp`)

This repo is also a plugin marketplace for both tools. The `acp` plugin
ships one skill (`acp`, the session protocol) plus `/acp:join`,
`/acp:poll`, `/acp:post`, `/acp:claim` and `/acp:release` commands. Its
`scripts/acp.py` wrapper runs this checkout's `ACP_client.py` from any
working directory (set `$ACP_HOME` if the checkout is not at
`~/dev/acp-agent-coordination`).

```sh
claude plugin marketplace add <path-to-this-checkout>
claude plugin install acp@acp-agent-coordination
codex plugin marketplace add <path-to-this-checkout>
codex plugin add acp@acp-agent-coordination
```

Installs are cached copies: after editing `plugins/acp`, reinstall
(uninstall + install) or bump the version in both `plugin.json` files.
Command bodies must not use `$ARGUMENTS`, because Codex skips such commands when it
converts them to skills (Claude Code still appends the arguments).

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

## HTTPS (optional)

Off by default — plain HTTP, unchanged. To turn it on:

```sh
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
  -keyout acp-key.pem -out acp-cert.pem -days 825 -nodes \
  -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"

ACP_TLS_CERT=acp-cert.pem ACP_TLS_KEY=acp-key.pem uv run ACP_server.py
```

Now `https://localhost:1337`. The cert is self-signed (there's no CA to
ask for a loopback-only server), so clients need to be told to trust it
rather than verifying against the system trust store:

```sh
ACP_BASE_URL=https://localhost:1337 ACP_TLS_CA=acp-cert.pem \
  uv run ACP_client.py status
```

(`ACP_TLS_INSECURE=1` skips verification entirely instead of pinning the
cert — fine for a quick loopback check, not a substitute for `ACP_TLS_CA`.)

**Key exchange:** this project adds no cryptography of its own — it
only wires `ssl_certfile`/`ssl_keyfile` into the server (`ACP_server.py`
`main()`) and `verify=` into the client (`ACP_client.py`). All of the
actual TLS behavior comes from OpenSSL. On OpenSSL 3.5+ (what `uv sync`
installs via the pinned Python here), the library's own default TLS 1.3
group preference already puts the hybrid post-quantum group
**`X25519MLKEM768`** (ML-KEM-768 combined with classical X25519) first,
so a modern peer gets it automatically; an older peer that doesn't
support it falls back to classical ECDHE (`X25519`/`P-256`) with no
error — "possible", not mandatory. The negotiated TLS 1.3 cipher is
`TLS_AES_256_GCM_SHA384` either way. Confirm what a given connection
actually negotiated with:

```sh
openssl s_client -connect localhost:1337 -tls1_3 2>&1 | grep -i "Negotiated TLS1.3 group"
```

On OpenSSL older than 3.5, `X25519MLKEM768` doesn't exist yet — TLS
still works, just without the post-quantum hybrid group.

## Coordination v2 (`coord`)

A second, self-contained coordination layer (`coordination/` + `coord.py`,
stdlib only, its own `coord2.db`) built for real multi-agent work. It runs
next to the ACP server; nothing above changes.

`coord`, `coord-server` and `coord-admin` are on PATH once the project is
synced (`uv sync`, then `ln -s "$PWD"/.venv/bin/coord* ~/.local/bin/`);
agents use `coord`, never `coord.py`.

**Identity.** `coord whoami claude` gives you `claude-NN`, a session UUID
and a generation. Every command authenticates with the UUID
(`COORD_SESSION` env, else `.coord-session` at the repo root). A recycled
name gets a new UUID and a higher generation, so it can never touch the
old session's claims. A dead or ended session cannot mutate anything, and
its claims lapse with it.

**Correctness.** Every mutation runs under `BEGIN IMMEDIATE`. Mutations
accept a `client_id` idempotency key (the CLI sends one automatically), so
a retried post/claim is replayed, not duplicated. `poll` and
`inbox --after N` use message ids as cursors, never timestamps. Everything
carries a `project_id` (auto-detected from `git remote origin`, e.g.
`github.com/org/repo`, override with `COORD_PROJECT`); claims, messages
and tasks are isolated per project; `coord projects` gives the cross-project view.

**Claims.** Paths are repo-relative and normalized (`\` -> `/`, `..` and
absolute paths rejected, case-folded on Windows). `coord claim src/auth/`
(or `--tree`) claims a directory tree; a file claims `exact`. Parent/child
scopes conflict; siblings don't. Claims get ids (`C12`) and a monotonic
**fence**: pass it to `coord fence-check C12 <fence>` before a write to
refuse stale leases. `renew C12`, `release C12`, `release --all`.

**Asking for help without losing ownership.**
`coord ask --claim C12 --to codex-01 "second opinion?"` sends a direct
question tied to the claim and grants `advisor` (or `--role reviewer|coeditor|delegate`);
the claim stays yours. Advisors/reviewers get no write access
(`check` still flags them); only an explicit `delegate` may claim inside your scope.

**Messages.** Kinds `info question advice proposal decision review warning done`;
`--to <session>` for direct messages (visible only to both ends);
`reply N`, `thread N`, `resolve N` (records who resolved). Soft limit 300
chars (warning), hard limit 10000 - put long analyses in a document.
`inbox` shows the last 20 by default; every command takes `--json`.

**Consensus.** `discuss "topic"` -> `D3`; `propose D3 "..."` -> `P7`;
`react P7 support|object|abstain|need-more-info [comment]`; `discussion D3`
shows tallies; the opener closes it with `decide D3 "..." --proposal P7
[--no-consensus]`, which records who/when/consensus and writes a final
`decision` document linked from the discussion thread.

**Documents.** `doc create --kind note|diagnosis|plan|proposal|decision|review|adr`,
`doc show DOC4 [--revision N]`, `doc edit DOC4 --base-revision N --file x.md`
(optimistic concurrency: a stale base is refused with the current content so
you merge and retry), `doc history DOC4`.

**Git.** `coord install-hooks` adds a pre-commit hook (`coord check` - fails
if a staged file is claimed by another session) and a post-commit hook
(`coord post-commit` - publishes `commit.created @sha` and releases your exact
claims made with `--release-on-commit`). Hooks do nothing without a session.

**Shared context and routing.** `memory add overview|convention|architecture|decision|pitfall|glossary "title" --content ...`
(versioned, attributed, `--source`), `memory show`, `memory search`,
`memory edit M2 --base-revision N`. `coord context` is the compact start-of-session
view (overview, memory, my claims, tasks, discussions, unread count).
`task create/accept/done`, `tasks --status open`. `coord profile --category reasoning
--capability debugging` declares a (transient) profile; `coord suggest
--prefer-category reasoning --capability debugging` ranks live agents - a hint, you choose.

**Network (opt-in): three roles, three files, three commands.**

| Role | File | Command | Holds |
|---|---|---|---|
| Management | `coordination/pki.py` | `coord-admin` | the CA (`pki/`, CA key never leaves it) |
| Server | `coordination/server.py` | `coord-server` | the service; asks management |
| Client | `coordination/client.py` + `coord.py` | `coord` | only its identity bundle |

Local mode (no `COORD_SERVER`, SQLite only), plain loopback HTTP and OIDC
never load the PKI code: `pki.py` is imported only by `coord-server --pki`
and `coord-admin`, and the client never imports it (it only swaps in a
renewed cert file when a server sends one).

`coord-server` listens on `127.0.0.1:1338`. A non-loopback `--listen` is
refused unless TLS **and** an identity method are configured:

- mTLS - the administrator, once: `coord-admin init`,
  `coord-admin server-cert`, then per client `coord-admin enroll <name>`.
  Enroll issues a 30-day client cert (or reuses a valid one) and writes a
  self-contained bundle to `~/.config/coord/<name>/` (`ca.crt`,
  `agent.crt`, `agent.key` 0600, `env`), or to `--out DIR` to hand to
  another machine. The first identity (or `--default`) becomes
  `~/.config/coord/env`; `COORD_IDENTITY=<name>` selects another,
  `COORD_CONFIG=<file>` any file; shell variables still win. Serve with
  `coord-server --pki pki`: on every request the server asks management
  whether the presented cert is still valid, so `coord-admin revoke <name>`
  (all of that client's certs) applies to the next request, no restart.
  Once a client cert is 15 days old (`--renew-after-days`), the server
  asks management for a renewal and returns it with the response; the
  client checks it matches its key and replaces `agent.crt` in place. The
  renewal re-certifies the key the client already holds: no private key
  is ever sent, and the client needs no openssl. A client offline for
  more than 30 days has to be enrolled again. `coord-admin list` shows
  every cert; `--crl` (TLS-level CRL) is still accepted.
- OIDC (Keycloak): `--oidc-introspect-url .../protocol/openid-connect/token/introspect
  --oidc-client-id coord` (secret in `COORD_OIDC_SECRET`); clients set
  `COORD_TOKEN`. Per-project roles come from token roles/groups named
  `coord:<project>:viewer|contributor|admin` (`coord:*:...` for all projects).

In both modes the session is bound to the authenticated principal at
`whoami`; another identity cannot drive it. Agents only ever run `coord`
(any directory, no exports, survives reboots).

**Servers as services.** `contrib/systemd/` has user units
(`coord-server --pki pki`, `acp-server`); install with
`cp contrib/systemd/*.service ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now coord-server acp-server`
(WSL needs `systemd=true` under `[boot]` in `/etc/wsl.conf`).

Tests: `uv run test_coord.py` (temp dirs, includes an 8-process claim race,
an HTTP round-trip, OIDC role checks and a real mTLS handshake with a revoked cert).

## Security

**No authentication, no access control — HTTPS above adds transport
encryption only.** Anyone who can reach the port can read and write the
mailbox and the presence roster, over HTTP or HTTPS alike. Run it on
loopback (`127.0.0.1`) only — never change the host to `0.0.0.0`, and
never expose the port via a port forward, tunnel, or proxy to a LAN or
the internet.

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
  busy timeout) holding the mailbox (last 5000 live,
  `mailbox-archive-<date>.json` for older ones), the roster and the
  claims; the old flat files stay on disk as the migration source only.
  No restart resilience for in-flight state, single machine only
  (`127.0.0.1`). Fine for this prototype's purpose; not meant to
  survive into whatever the dedicated project becomes.
