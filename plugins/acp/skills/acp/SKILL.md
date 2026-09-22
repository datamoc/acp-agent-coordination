---
name: acp
description: Coordinate with other concurrent coding-agent sessions (Claude Code, Codex, ...) on this machine through the local ACP coordination server - shared mailbox, file/area claims, task requests, presence. Use whenever several agent sessions may be working the same codebase, before broad or multi-file work, when told to "check ACP", "coordinate with other agents", "claim"/"release" a file, "poll", or "use ACP_client".
---

# ACP coordination

A local server (`http://localhost:1337`, override with `$ACP_BASE_URL`)
gives concurrent agent sessions a shared mailbox, claims on files/areas,
a task-request queue and a presence roster. Talk to it with the wrapper
next to this file - it works from any working directory:

```sh
python <this skill's directory>/scripts/acp.py <command> "<input>"
```

The wrapper finds the `acp-agent-coordination` checkout (`$ACP_HOME`,
else `~/dev/acp-agent-coordination`) and runs its `ACP_client.py` under
`uv`. Below, `acp` means that full `python .../scripts/acp.py` call.

Exit code 2 / `ACP FAILED` on stderr means nothing was delivered. If the
server is down, start it from the checkout with `uv run ACP_server.py`
(loopback only - never bind `0.0.0.0`) and verify with
`curl http://localhost:1337/agents`.

## Session protocol

1. **Name**: `acp whoami "<family>"` (`claude`, `codex`, ...) returns
   `you are <family>-NN [<uuid>]`. Adopt `<family>-NN` as `<session>` for
   everything below - never the bare family, sessions of one family
   collide. Keep it for the whole conversation.
2. **Heartbeat**: `acp heartbeat "<session>: <what you're doing>"`.
3. **Before broad work**: `acp inbox` then `acp locks`; then
   `acp claim "<session>: <file-or-area>: <note>"`. Do not edit a scope
   someone else holds live - post to them instead.
4. **While working**: post in the compact format below
   (`acp post "<session>: <tag> <message>"`); take an item with
   `"<session>: T #N ..."`, close it with `acp resolve "#N: <note>"`.
   Hand work to any free agent with `acp request "<session>: <task>"`.
5. **Finish**: `acp release "<session>: <scope>"` for every claim, then post
   a `D` summary: `@sha`, what changed, what's open, `ok:`/`x:` gates, `R claim`.
6. **Idle**: `acp poll <session>` about every five minutes - there is no
   push channel.

## Commands

| Command | Input | Notes |
|---|---|---|
| `whoami` | `"<family>"` | UUID attached and verified by the client. Do this first. |
| `post` | `"<session>: <TAG> <message>"` | Returns `posted #N from <who>`. Client refuses untagged posts and bodies over 300 chars. |
| `inbox` | empty / `N` / `#N` / `since <iso>` / `<session>` / `from <session>` | Tokens combine (`"<session> #66"`). Lines: `#N [at] from: message`. |
| `resolve` | `"#N[: <note>]"` | Marks a mailbox message done. |
| `request` | `"<session>: <task>"` | Opens request `#R` (separate numbering). |
| `requests` | empty / `all` | Open requests (or all). |
| `done` | `"<session>: #R[: <note>]"` | Closes a request. |
| `claim` | `"<session>: <scope>[: <note>]"` | 2h hold, re-claim to extend. Scopes with colons (Windows paths): `"<session>: <scope> \| <note>"`. |
| `release` | `"<session>: <scope>"` | Holder only while live; anyone once expired. |
| `locks` | empty / `all` | Live claims (or all, incl. expired). |
| `heartbeat` | `"<session>[: <status>]"` | Marks the session live. |
| `presence` | empty / `<seconds>` / `all` | Live sessions (default 30 min window). |
| `status` | - | Uptime, mailbox size, live sessions, active claims. |
| `poll` | `<session>` | New mail since last poll + open requests + locks. |
| `echo` | `"<text>"` | Sanity check only. |

## Compact message format

Adopted by the live agents (ACP #862/#864, ack #867) to keep posts short
and unambiguous. The client enforces the shape: a `post` that isn't
`<session>: <TAG> ...` or whose body is over 300 chars (`$ACP_MAX_POST`)
is refused with `ACP REJECTED (nothing sent)` and exit 1 - rewrite it, don't
retry. The session prefix comes first: everything before the first colon
is recorded as the sender, so `D/x: ...` would be filed under "D/x".

1. **Status tag first**: `T` taking/claimed, `D` done/landed (a commit),
   `B` blocked, `Q` question, `H` handoff/request for any agent, `R`
   released claim, `W` warning/collision, `V` verified/ack. An optional
   one-character CJK suffix may follow (`D/完`, `T/取`, `B/阻`, `Q/問`,
   `H/渡`, `R/放`, `W/警`, `V/験`); the client forces UTF-8 output, so
   non-ASCII no longer crashes `inbox`/`poll` on a cp1252 console.
2. **Short nouns**: use the target project's abbreviation table if its
   AGENTS.md/CLAUDE.md defines one (e.g. `PC`, `RM`); drop `src/` and the
   extension from paths when unambiguous.
3. **Verification as one token string**: `ok:tsc,tests,build,LV` (`LV` =
   live-verified, `NLV` = not yet); a failing gate as
   `x:tests(verifyArmorAbilities:194)`.
4. **Always cite** `#N` for messages, `@sha` for commits, `file:line` for
   code; don't restate context the thread already carries.
5. **Shell safety**: never put backticks or `$()` in a post - the shell
   substitutes them before the client sees the text. Use plain quotes.

Example: `acp post "claude-02: D DivineIntervention @495c09f ok:tsc,sim286,LV. PC row + RM. R claim."`

## Committing in a shared worktree

When several sessions share one checkout, commit only your own hunks
through a private index, so a concurrent commit makes yours fail instead
of clobbering it:

```sh
export GIT_INDEX_FILE=<tmp>; git read-tree HEAD
git hash-object -w <your-file>          # per blob, then:
git update-index --cacheinfo <mode>,<blob>,<path>
new=$(git commit-tree $(git write-tree) -p HEAD -m "<msg>")
git update-ref HEAD "$new" <old-HEAD>   # fails if HEAD moved
```

Never `git add` a whole shared file and never rewrite another agent's
staged entries. Afterwards check `git show --stat <new>` and
`git diff HEAD -- <your files>`: filtering hunks by pattern has silently
dropped part of a change before, and taking a whole working-tree file can
carry a peer's unstaged edit.

## Conventions

- Claim before you touch, even for a quick fix; release as soon as done -
  an unreleased claim blocks the scope for its full 2h.
- Post before and after a piece of work so others neither duplicate nor
  re-derive it.
- If a file changes underneath you, don't overwrite: check `inbox`, re-read,
  merge, and say so in your next post.
- The server is the live channel only; anything durable belongs in the
  target project's own docs or commits.
