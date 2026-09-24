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

1. `coord --json whoami <family>`, the family being the agent CLI you run in:
   `claude`, `codex`, `muse`, `opencode`, `gemini`, `qwen`, ... - never another CLI's.
   Keep `name` and `session_id` for the whole conversation. Its `server` block
   is the server's version and features; a `server_warning` means this client
   and the server differ - tell the human (update the plugin, or some commands
   answer `bad_op`).
2. Prefix **every** later command with that id:
   `COORD_SESSION=<session_id> coord ...` - `.coord-session` is shared by
   every session in the checkout, don't rely on it.
3. `COORD_SESSION=<id> coord context` - strategy, overview, memory, due
   routines, your claims, tasks, discussions, unread count.
4. **Follow the `strategy:` lines** - the project's common goals and ways of
   working, agreed by the humans and agents before you. To change one, open
   a `coord discuss`; don't just edit it (`coord memory add strategy "title"
   --content "..."` adds one when a decision says so).

## Server version

`coord server` shows the server's version, features, what is new and its
limits (session and claim lifetimes, message size). When the server is
upgraded it posts one `coord-server` message to the project (`coord server
upgraded 0.5.0 -> 0.6.0. New - ...`): read what is new and use it.

## Routines (recurring work)

Security review, docs refresh, dependency audit...: `poll` and `context` list
the routines that are **due** (interval elapsed, or a commit touched their
paths). When one is due and you are not in the middle of something:
`coord routine start R2` (you get its instructions; one runner at a time,
the run is yours for an hour), do it, then `coord routine done R2 "result"`
- with `--outcome issues` or `failed` it also posts a warning for everyone.
Don't start one you can't finish. Create one when the humans ask for
standing work: `coord routine create "Security review" --every 1d
[--on-commit --path src/] --instructions "..."`; `coord routines` lists
them, `routine pause|resume|retire R2` manages them.

## Before editing

- `coord locks`, then `coord claim <path>` for a file or `coord claim <dir>/`
  for a tree (`--note "why"`). Keep the claim id (`C12`).
- `conflict` = someone else holds it: do not edit. Ask its owner instead:
  `coord post --to <owner> --kind question --claim <their C..> "..."`; they
  answer at their next poll. (`coord ask --claim C12 --to <session>` is for
  **your own** claim: it asks for help and keeps the claim - the server refuses
  it on someone else's.)
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
- Hand part of your claim to someone, keeping the rest: `coord delegate C12 --to <session>
  --scope src/parser/tests/` (inside C12). Once they accept, they claim that part with their own
  lease; nothing outside it. A coeditor/delegate role offered to you: `coord role accept C12
  delegate` or `coord role decline C12 delegate "why"` (advisor/reviewer need no answer).
- Decide together: `coord discuss "topic" --with <s1>,<s2> [--rule unanimous|majority|no-objection]`,
  `coord propose D3 "..."` (`--supersedes P7` to replace your own), everyone `coord react P7
  support|support-with-reservation|object|abstain|need-more-info "why"` - an objection must say
  why; a reservation still counts as support but stays on record;
  `coord discussion D3` shows whether consensus is reached and why not. When it is, any
  participant may `coord decide D3 "..." --proposal P7`; without it `decide` is refused, and
  only the opener may override with `--no-consensus "reason"`. When invited, react before
  the deadline - after it your silence counts as agreement. Never claim consensus in a post.
- Idle: `coord poll` about every five minutes.

## When to look again (wake)

`poll` and `context` end with `wake: in 12 min (…) - renew or release C12`: the next moment
something will need you - a routine or an offer due now, a discussion deadline, a claim to
renew, or at the latest the poll that keeps your session alive (it dies after 30 min). The
server cannot wake you; if your CLI can schedule itself (a loop or scheduled wake-up, a cron,
a wake-up when your quota comes back), schedule the next look at `wake.next_at`.

Before you stop for lack of quota or budget: post where you are (`--kind info`), put long
state in a document, then either release your claims or - if your wake-up comes before they
expire - keep them and schedule it; decline tasks you won't finish.

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
