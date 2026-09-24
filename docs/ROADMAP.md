# Road to coord 1.0.0

*Written 2026-09-24, at 0.10.0. The tasks below are real coord tasks in the project
`github.com/datamoc/coord` (T22-T43, category `roadmap-1.0`): `coord tasks --graph`, or the
Tasks tab of `coord-server --ui`.*

## Where the design notes stand

The design notes (*ACP Agent Coordination v2 — notes d'architecture et backlog*; "v2" is the
second revision of the notes, not a coord 2.0) are the scope of **1.0.0**. They are almost
entirely delivered:

| Notes | Status |
|---|---|
| P0 correctness - BEGIN IMMEDIATE, session UUID + generation, server-side identity, id cursors, idempotency, multiprocess tests, CI | done |
| P1 claims - repo-relative paths, exact/tree scopes, claim ids, renew, release --all, fences, `coord check` | done |
| P2 messages - no hard 300-char limit, `--to`, reply/thread, kinds, bounded output, `--json` | done |
| P3 consensus - `ask --claim`, roles, discussions, proposals, reactions and objections, decisions | done (consensus is computed) |
| P4 documents - shared documents, optimistic revisions, history, link to discussions, patches | done |
| P5 integrations - git hooks, SSE, sub-scope delegation | done; the MCP adapter was dropped (A2A instead) |
| §13 `coord check` warn **or** fail, configurable | **T22** |
| §13 post-commit releases exact claims on committed files | **T23** (only `release_on_commit` claims today) |
| §18 CI on the supported Python versions, lint and type checking, clean-install test | **T24, T25, T26** |

Beyond their scope, coord already has per-CLI identities, strategy and routines, wake hints, the
task graph, a UI for humans, and plugins for eight agent CLIs.

## What 1.0.0 means

The notes, finished (T22-T26), and a promise: **the wire contract (`schema/ops.json`) and the database
are stable** - a 1.0 client keeps working against 1.x servers, a 1.x server opens every older
database - and coord has been used for real, under load, without a serious bug.

## Milestones

```
0.11 quality gates ──► 0.12 contract frozen ──► 0.13 hardened ──► 1.0-rc1 ──► one week of real use ──► 1.0
 T22 check warn/fail      T28 contract review      T33 security review   T38 guides    T42 mwg-pixel-dungeon
 T23 post-commit          T29 compat policy        T34 load test         T39 changelog
 T24 Python matrix        T30 upgrade tests        T35 24 h soak         T40 packaging
 T25 lint + types         T31 import / restore     T36 fixes
 T26 clean install
```

| Milestone | Tasks | Effort (active days) | Notes |
|---|---|---|---|
| **0.11** quality gates | T22-T26 → T27 | 1 | T25 (ruff/pyright) is the biggest: it will find things |
| **0.12** contract frozen | T28-T31 → T32 | 1.5 | T28 decides what 1.0 promises; T30 needs fixture databases from old tags |
| **0.13** hardened | T33-T36 → T37 | 1.5 + a 24 h soak | the soak runs overnight; T36 is the unknown |
| **1.0-rc1** | T37, T38-T40 → T41 | 0.5 | guides, changelog and packaging can run beside 0.13 |
| **1.0** | T42 → T43 | 7 calendar days | real multi-agent work on mwg-pixel-dungeon, no P0 bug |

**Critical path:** T25 → T27 → T28 → T29 → T32 → T34 → T35 → T36 → T37 → T41 → T42 → T43. The
week of real use is the longest step and cannot be compressed: it is what earns the "1".

## Forecast

**Measured pace.** 0.1.0 → 0.10.0: 16 releases over 4 calendar days, of which 3 were active
(56 commits), in sessions led by one person with several agents - including pauses when a
usage quota ran out. Roughly 4 300 lines of Python and 2 100 of TypeScript today.

**Assumptions.** An *active day* is a day of work like 23 or 24 September. About 4.5 active days
remain before the release candidate (the table above). Three scenarios, starting 2026-09-25:

| Scenario | Pace | 0.11 | 0.12 | 0.13 | 1.0-rc1 | **1.0** |
|---|---|---|---|---|---|---|
| Optimistic | 5 active days a week, fixes small (3.5 days) | Sep 25 | Sep 26 | Sep 28 | Sep 29 | **Oct 6** |
| **Likely** | 4 active days a week, 4.5 days of work | Sep 25 | Sep 28 | Sep 30 | Oct 1 | **Oct 8** |
| Pessimistic | 2-3 active days a week, the review or the soak finds real problems (6.5 days) | Sep 28 | Oct 2 | Oct 8 | Oct 12 | **Oct 19** |

**Target milestone: 1.0 around 8 October 2026**, with 6-19 October as the range.

**What would move it:**
- T25 (lint and types) or T33 (security review) finding a structural issue - the pessimistic line.
- T42 finding a P0 bug: fix it, and the week of real use starts again.
- Packaging (T40): `coord` is free on PyPI and `coord-client` on npm (checked 2026-09-24), so
  publishing is not blocked; the alternative is to keep the GitHub release assets.
- Available time: the forecast counts active days, so a quiet week shifts every date by that week.

## Keeping it current

Each milestone is a task: when its prerequisites are done, whoever it is for is told it is
unblocked (`task.unblocked`). Re-run the forecast at each milestone with the actual pace, and
update this file with the dates reached.
