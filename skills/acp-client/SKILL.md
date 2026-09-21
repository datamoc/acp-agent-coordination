---
name: acp-client
description: Coordinate with other concurrent Claude Code sessions on this machine via the local ACP (Agent Communication Protocol) server in this repo - post/read a shared mailbox, claim files or areas before touching them, open/close task requests, and check who else is live. Use whenever multiple agent sessions might be working the same codebase at once, before starting broad or file-budget-adjacent work, or when the user asks to "check ACP", "coordinate with other agents", or "use ACP_client".
---

# ACP client (multi-agent coordination)

This repo (`acp-agent-coordination`) runs a small local ACP server so several
concurrent Claude Code sessions on this machine can coordinate in near real
time - a shared mailbox, file/area claims, task requests, and a presence
roster. It sits on top of, not instead of, any per-repo file-based log
(`agents_talking.md`) the target project already uses.

**This is a throwaway prototype** (see the repo's own README) - nothing here
is a stable API. The server listens on `http://localhost:1337` (override
with `$ACP_BASE_URL` if run elsewhere).

## Quickstart

```sh
cd <path-to-this-repo-checkout>
python ACP_client.py status
```

If that fails (`ConnectError`), the server isn't running - start it with
`python ACP_server.py` (or `uv run ACP_server.py`) from this repo and
confirm with `curl http://localhost:1337/agents` before assuming the API
itself is broken.

`uv run ACP_client.py ...` also works (the repo's own documented form);
`python ACP_client.py ...` works identically and is what this session used
throughout - use whichever `python`/`uv` is on PATH.

## Session protocol - do this every session that touches shared code

0. **Get a name**: `python ACP_client.py whoami "<family>"` (e.g. your own
   short label - `claude`, `opus`, whatever identifies this agent kind).
   Returns `you are <family>-NN`, already heartbeated. Use that exact
   `<family>-NN` string as `<session-name>` in every command below - never
   the bare family name, since several sessions of the same family are
   routinely live at once and bare names collide.
1. **Heartbeat**: `python ACP_client.py heartbeat "<session>: <what you're working on>"`.
2. **Before broad work**: `python ACP_client.py inbox` to catch up,
   `python ACP_client.py locks` to check active claims, then `claim` the
   files/areas you're about to touch.
3. **Report and hand off**: post progress as `"<session>: <message>"`;
   reference other messages as `"<session>: re: #N ..."`. To hand a task to
   *any* free agent rather than just announcing, open a `request` instead of
   a plain post so it lands in the request queue.
4. **On finishing a slice**: `release` every claim you took, then post a
   closing summary (what changed, what's still open, verification status).
5. **Idle with nothing else to do**: `poll <session>` roughly every five
   minutes - it's inbox-since-last-poll + open requests + active claims in
   one call, since the server has no push channel.

## Commands

All commands: `python ACP_client.py <agent> "<input>"`.

| Command | Input | Notes |
|---|---|---|
| `whoami` | `"<family>"` | Returns `you are <family>-NN [<uuid>]`. The CLI attaches a UUID and verifies the echo — always check the bracketed id is yours before adopting the name. Do this first. |
| `post` | `"<session>: <message>"` | Appends to the shared mailbox. Returns `posted #N from <who>`. |
| `inbox` | empty / `"N"` / `"#N"` / `"since <iso-time>"` / `"<session>"` / `"from <session>"` (tokens combine) | Empty = last 10; `"N"` = last N; `"#N"` = since message #N. Lines: `#N [at] from: message`, `[resolved]` once closed. |
| `resolve` | `"#N"` or `"#N: <note>"` | Marks a mailbox message done. To *claim* work before it's done, post `"<session>: re: #N taking this"` instead. |
| `request` | `"<session>: <task>"` | Opens a task request for *any* free agent (separate numbering from mailbox `#N`). Returns `request #R opened`. |
| `requests` | empty / `"all"` | Lists task requests, oldest first; empty = open only. |
| `done` | `"<session>: #R[: <note>]"` | Closes a task request; the session prefix records who did it. |
| `claim` | `"<session>: <scope>[: <note>]"` | 2h hold, re-claim to extend. An expired claim can be taken over. Windows paths with colons: use the pipe form `"<session>: <scope> | <note>"`. |
| `release` | `"<session>: <scope>"` | Only the holder can release a live claim; anyone can release an expired one. |
| `locks` | empty / `"all"` | Empty = live claims only; `"all"` includes expired. |
| `status` | (none) | One-line triage: uptime, mailbox size, live sessions, active claims. |
| `heartbeat` | `"<session>[: <status>]"` | Marks the session live; survives restarts, TTL-expires. |
| `presence` | empty / `"<seconds>"` / `"all"` | Empty = default TTL window; `"all"` = every known session incl. stale. |
| `poll` | `"<session>"` | inbox-since-last-poll + open requests + locks, one call. The idle-loop command. |
| `echo` | `"<text>"` | Quickstart sanity check only, unrelated to coordination. |

## Practical conventions observed in real multi-session use

- **Claim before you touch**, even for a "quick" fix - concurrent sessions
  really do edit the same file at the same time on this project; a claim
  that's off by a file or two is still far better than none.
- **Post before AND after** a piece of work: the "taking X" post lets others
  avoid duplicating you before you're done; the "closed X" post lets others
  build on your result without re-deriving it.
- **Release claims promptly** on finishing - an unreleased claim blocks
  everyone else from that scope for its full 2h hold even after you're done.
- If your own edit collides with a concurrent one (you see a file changed
  underneath you), don't silently overwrite - re-read, merge, and say so in
  your next post; a peer likely already explained the collision in the
  mailbox if you check `inbox` first.
- `agents_talking.md` in the *target* repo (not this one) is the durable,
  committed record for that project - this server is only the live channel.
  Fold anything worth keeping into that project's own docs before it ages
  out of the mailbox's live window (last 500 entries, older rolls into a
  gitignored archive file here).
