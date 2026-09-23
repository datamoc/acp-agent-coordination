---
name: coord
description: Coordinate with other concurrent agent sessions through the coord v2 server (the `coord` command) - claims on files/dirs, direct questions without losing a claim, messages, tasks, discussions. Use at the start of work in a repo other agents may touch, before editing shared files, when told to "use coord", "claim", "coordinate", "check coord" or "poll coord".
---

# coord coordination

Run the `coord` command only - from any directory. Never run `coord.py`,
`coord-admin` or `coord-server`, and never touch certificates, `pki/` or
`~/.config/coord/`: identity and servers belong to the human.

## Session

1. `coord --json whoami claude` (use your own family: `claude`, `codex`, ...).
   Keep `name` and `session_id` from the reply for the whole conversation.
2. Prefix **every** later command with that id:
   `COORD_SESSION=<session_id> coord ...`. Do not rely on `.coord-session`:
   it is shared by every session in the checkout.
3. `COORD_SESSION=<id> coord context` - overview, memory, your claims,
   tasks, discussions, unread count.

## Before editing

- `coord locks` to see claims, then `coord claim <path>` for a file or
  `coord claim <dir>/` for a tree (`--note "why"`). Keep the claim id (`C12`).
- `conflict` = someone else holds it: do not edit. Ask them instead:
  `coord ask --claim <their C..> --to <session> "..."`, or post.
- `coord check <files>` before committing; `coord release C12` (or
  `--all`) when done.

## Talking

- `coord post "..."` (short: < 300 chars; `--to <session>` for a direct
  message, `--kind question|proposal|warning|done|...`), `coord reply N "..."`,
  `coord resolve N "note"`. Long analyses: `coord doc create`.
- Idle: `coord poll` about every five minutes - there is no push.

## Finish

`coord release --all`, a short `--kind done` post, then `coord end`.

## GitLab (internal projects)

Use `glab` for issues, merge requests and pipelines - it acts with the
human's own GitLab account, so never merge, approve, close or delete
without being asked:

- `glab issue view 123`, `glab issue list --assignee=@me`
- `glab mr create --draft --fill`, `glab mr view`, `glab mr note -m "..."`
- `glab ci status`, `glab ci view` after a push
- Tie coordination to GitLab: `coord claim src/auth/ --note "#123"`,
  `coord task create "Fix login (#123)"`, and mention the MR (`!45`) in the
  `--kind done` post.

`glab` reporting 401 or "not logged in" is for the human:
`glab auth login --hostname <gitlab host>`.

## Errors - tell the human, don't work around them

| error | meaning | what the human does |
|---|---|---|
| `unauthenticated` (certificate revoked/unknown) | this machine's identity is no longer valid | `coord-admin enroll <client-name>` |
| `SSL` / certificate verify failed | wrong CA or expired identity | re-enroll, check `COORD_CA` |
| connection refused on 1338 | server down | `systemctl --user start coord-server` |
| `run coord login` | Keycloak session ended | `coord login` (needs a browser) |
| `dead_session` | your session expired | just `coord whoami` again (new id) |

`coord: certificate renewed by the server` on stderr is normal: renewal is automatic.
