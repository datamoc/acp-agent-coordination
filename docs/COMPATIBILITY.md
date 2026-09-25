# Compatibility policy

What coord promises across versions. Written for **T29** at 0.13.0; **T32** is where this is
declared frozen and the wire contract stops moving.

## The contract

[`schema/ops.json`](../schema/ops.json) is the contract, generated from the server
(`uv run tools/gen_schema.py`): every op with its params (type, nullability, whether it is
required), the enums, and the error codes a reply can carry. Its `version` field (1 today) is the
contract's own revision. It changes only when a consumer would have to change to stay correct -
a param renamed or removed, a required param added, a code repurposed. **Adding** an op, an
optional param, an enum value or an error code does not bump it.

## What the server actually does (measured at 0.13.0)

| the client sends | what comes back |
|---|---|
| an op that does not exist | `bad_op`, HTTP 400 |
| an argument the op does not take | `bad_args`, HTTP 400 - `tasks() got an unexpected keyword argument 'x'` |
| `unauthenticated` / `forbidden` | HTTP 401 / 403 |
| everything else | HTTP 409, with `{"ok": false, "error": <code>, "message": ..., "data": ...}` |

The middle row is the whole difficulty: **the server refuses an argument it does not know**. A
client cannot hand a new argument to an old server and hope it is ignored. Match on the body's
`error` code, not the HTTP status - the status is a coarse mapping.

## The promise, from 1.0 on

1. **Ops are not removed within 1.x.** An op that must go is first called out in the README and
   in `NEWS` (`coordination/core.py`), then refuses to run with its own named error code for at
   least **two minor releases**, and only then - in the next major - disappears.
2. **An argument is never made required later, and never repurposed.** New arguments are optional
   and stay optional. An existing argument keeps its meaning for the life of 1.x.
3. **New behaviour arrives as a new op.** Because of the rule above, a server cannot accept an
   argument it has never heard of, so the way to add a capability is an op, not a flag on an old
   one - unless every server that might see it already accepts the value (an enum value, an
   optional arg both sides already have).
4. **Results grow, never shrink.** A client must ignore fields it does not recognise. New fields
   are optional; nothing present today is removed from a result in 1.x.
5. **Error codes are part of the contract.** Codes are added, never repurposed - `forbidden` keeps
   meaning what it means today. The `message` is prose for a human and may be rewritten at any
   time; `error` and `data` are what to match on.
6. **N-1 clients.** A client built from contract version N keeps working against a server of
   N-1 (one minor release behind). The server never requires a newer client. The other direction
   degrades rather than fails: a newer client against an older server gets `bad_op` or `bad_args`,
   and `coord whoami` already warns when the two versions differ.
7. **The database.** A 1.x server opens every older database - migrations add nullable columns and
   new tables, they do not rewrite what is there (proved by `older_databases_still_open`, which
   builds fixtures with the code of 0.2, 0.5, 0.9 and 0.10 and opens them with this one).
   **Downgrade is not promised**: take `coord-db export` before upgrading, restore with
   `coord-db import`.

## Settled before the freeze

Three names each carry two meanings (found by the contract review, DOC10). They are **accepted as
they are**, and this is the decision T32 froze them under:

- **`message`** is an integer id in `ack`, `receipts`, `reply`, `resolve`, `thread`, and the change
  summary in `doc_edit` / `doc_patch` - the `--message/-m` flag, a git-style note about the edit.
- **`priority`** is an integer on `task_create` / `task_update`, and the
  `low|normal|high|urgent` enum on `post` - attention, not rank.
- **`after`** is a cursor in `inbox` and `events`, and prerequisites in `task_create`, `task_link`
  and `milestone_create`; `task_waive` takes exactly one, on purpose (waiving lifts one link and
  keeps its reason).

Renaming them now would break every existing caller - CLI, agent skill, README, direct API users -
for something the generated types already prevent: a client reads `integer`, `string` or
`string[]` out of `schema/ops.json` per parameter, so it cannot confuse them. Accepted; not
revisited in 1.x, which is exactly what rule 2 above promises.

## Deprecation window

**Two minor releases**, and never fewer than one. A deprecation is announced in three places at
once - `NEWS` (which the server posts to every project when it starts on a newer version), the
README, and the error the op starts returning - so an agent reading any of them learns it without
reading this file. The window is counted in releases, not days, because the releases are what a
user installs.

## Before 1.0

0.x follows the same rules as best effort, but nothing is frozen: `schema/ops.json` may still
change where this policy allows it, and breaking an op is a decision taken in the open rather
than a defect. What is already true and stays true: the contract is generated, both sides are
tested against it, and a change that a client could not survive fails CI
(`gen_schema --check`, the TS suite's "every server op is reachable from the CLI", and the
bundle check).
