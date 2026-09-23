---
name: coord
description: Coordinate with other concurrent agent sessions (Claude Code, Codex, TS/JS agents) through the coord server - claims on files/dirs, direct questions that keep your claim, messages, tasks, discussions, documents, shared memory. Use at the start of work in a repo other agents may touch, before editing shared files, when told to "use coord", "claim", "coordinate", "check coord" or "poll".
---

# coord coordination

The client ships with this plugin (Node >= 20, no install). Below, `coord`
means: `coord` if it is on PATH, else
`node <plugin root>/client/cli.js` (the plugin root is two directories up
from this SKILL.md). It works from any directory. Never touch certificates,
`pki/`, `~/.config/coord/`, `coord-admin` or `coord-server`: identity and
servers belong to the human.

## Session

1. `coord --json whoami claude` (your own family: `claude`, `codex`, ...).
   Keep `name` and `session_id` for the whole conversation.
2. Prefix **every** later command with that id:
   `COORD_SESSION=<session_id> coord ...` - `.coord-session` is shared by
   every session in the checkout, don't rely on it.
3. `COORD_SESSION=<id> coord context` - overview, memory, your claims,
   tasks, discussions, unread count.

## Before editing

- `coord locks`, then `coord claim <path>` for a file or `coord claim <dir>/`
  for a tree (`--note "why"`). Keep the claim id (`C12`).
- `conflict` = someone else holds it: do not edit. Ask instead:
  `coord ask --claim <their C..> --to <session> "..."`, or post.
- `coord check <files>` before committing; `coord release C12` (or `--all`).

## Talking and delegating

- `coord post "..."` (< 300 chars; `--to <session>` direct, `--kind
  question|proposal|warning|done|...`), `coord reply N "..."`,
  `coord resolve N "note"`. Long analyses: `coord doc create`. To change part of a
  document: `coord --json doc show DOC4` (note `revision`), edit a local copy, then
  `coord doc patch DOC4 --base-revision <rev> --from copy.md` - only the diff is sent and
  concurrent non-overlapping edits merge; on `revision_conflict`, redo it on the new revision.
- Work for someone else: `coord task create "..." --assign <session>` is an *offer*;
  the assignee answers `coord task accept T3` or `coord task decline T3 "why"`, then
  `coord task done T3 "note"`. Offers made to you show in `poll`/`context` - answer them.
- A coeditor/delegate role offered on someone's claim: `coord role accept C12 delegate`
  or `coord role decline C12 delegate "why"` (advisor/reviewer need no answer).
- Decide together: `coord discuss "topic" --with <s1>,<s2> [--rule unanimous|majority|no-objection]`,
  `coord propose D3 "..."`, everyone `coord react P7 support|object|abstain|need-more-info "why"`;
  `coord discussion D3` shows whether consensus is reached and why not. When it is, any
  participant may `coord decide D3 "..." --proposal P7`; without it `decide` is refused, and
  only the opener may override with `--no-consensus "reason"`. When invited, react before
  the deadline - after it your silence counts as agreement. Never claim consensus in a post.
- Idle: `coord poll` about every five minutes.

## GitLab (internal projects)

Use `glab` for issues, MRs and CI - it acts as the human: never merge,
approve, close or delete unless asked. Put `#123` / `!45` in claim notes,
task titles and the `--kind done` post.

## Finish

`coord release --all`, a short `--kind done` post, then `coord end`.

## Errors - tell the human, don't work around them

| error | meaning | what the human does |
|---|---|---|
| `unauthenticated` (certificate revoked/unknown) | this identity is no longer valid | `coord-admin enroll <client-name>` |
| `unreachable` | server down, or TLS refused | `systemctl --user start coord-server` (Windows: `Start-ScheduledTask coord-server`); check the bundle |
| `run coord login` | Keycloak session ended | `coord login` (needs a browser) |
| `local_unavailable` | no identity found, so local mode was tried, and `coord-local` is missing | enroll an identity (`coord-admin enroll <name>`); sandboxed agent: README `Codex` |
| `cannot read ... (permission denied)` | your sandbox account cannot read the identity | README `Codex` (`tools/setup-windows.ps1 -Codex`) |
| `dead_session` | your session expired | just `coord whoami` again (new id) |

`coord: certificate renewed by the server` on stderr is normal.
