# Road to coord 1.0.0

*Written 2026-09-24 at 0.10.0; **updated 2026-09-25 at 0.13.0** - the milestones reached, the
pace measured again, the forecast re-run. The tasks below are real coord tasks in the project
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
| §13 `coord check` warn **or** fail, configurable | done - **T22** (24 Sep, 21:57) |
| §13 post-commit releases exact claims on committed files | done - **T23** (24 Sep; only `release_on_commit` claims today) |
| §18 CI on the supported Python versions, lint and type checking, clean-install test | done - **T24, T25, T26** (24 Sep) |

Beyond their scope, coord already has per-CLI identities, strategy and routines, wake hints, the
task graph, a UI for humans, and plugins for eight agent CLIs. Since these notes were written it
also has, at 0.11-0.13: project permissions (`viewer`/`contributor`/`decider`/`admin`),
`coord doc import` with provenance, and the rest of the human-participation proposition -
receipts and contact policies, session states and wake-ups, weighted governance and crisis
mandates, typed links and milestones with a date projection, claims on any resource, a
dashboard and an activity stream.

## What 1.0.0 means

The notes, finished (T22-T26, all five closed on 24 September), and a promise: **the wire
contract (`schema/ops.json`) and the database are stable** - a 1.0 client keeps working against
1.x servers, a 1.x server opens every older database - and coord has been used for real, under
load, without a serious bug.

## Milestones

```
0.11 quality gates ──► 0.14 contract frozen ──► 0.15 hardened ──► 1.0-rc1 ──► one week of real use ──► 1.0
 T22 check warn/fail      T28 contract review      T33 security review   T38 guides    T42 mwg-pixel-dungeon
 T23 post-commit          T29 compat policy        T34 load test         T39 changelog
 T24 Python matrix        T30 upgrade tests        T35 24 h soak         T40 packaging
 T25 lint + types         T31 import / restore     T36 fixes
 T26 clean install
```

| Milestone | Tasks | Effort (active days) | Reached (25 Sep) | Notes |
|---|---|---|---|---|
| **0.11** quality gates | T22-T26 → T27 | 1 | **yes** - the night of 24-25 Sep | T25 (ruff/pyright) was the biggest: 285 errors to 0 |
| **0.14** contract frozen | T28-T31 → T32 | 1.5 | **yes** - 25 Sep | contract reviewed (DOC10), policy in docs/COMPATIBILITY.md, fixtures built from the tags, import + restore guide |
| **0.15** hardened | T33-T36 → T37 | 1.5 + a 24 h soak | no - T33-T37 open | the soak runs overnight; T36 is the unknown |
| **1.0-rc1** | T37, T38-T40 → T41 | 0.5 | no - T37-T41 open | guides, changelog and packaging can run beside the hardening |
| **1.0** | T42 → T43 | 7 calendar days | no - T42-T43 open | real multi-agent work on mwg-pixel-dungeon, no P0 bug |

**The numbers ran ahead of the milestones - decided 2026-09-25 (T104).** 0.11.0, 0.12.0 and
0.13.0 shipped in one night while T28-T37 - contract review, compatibility policy, upgrade tests,
threat model, load test, soak - are all still open, so T32 and T37 were named after version
numbers that had already been released. **Decision: a milestone takes the number of the release
it ships in**, so the two remaining ones were retitled to the next free versions - T32 "Milestone
0.14: contract frozen (1.0 API candidate)", T37 "Milestone 0.15: hardened" - while 0.12.0 and
0.13.0 keep what they actually shipped (doc import, participation). The jump from 0.11 to 0.14
is that, and only that. If a feature release takes 0.14.0 first, the milestone moves again:
retitle the task, never the version that already shipped.

**Critical path:** T25 → T27 → T28 → T29 → T32 (all five done) → T34 → T35 → T36 → T37 → T41 →
T42 → T43. The week of real use is the longest step and cannot be compressed: it is what earns
the "1".

## Forecast

**Measured pace.** 0.1.0 → 0.13.0: 20 releases over 5 calendar days (21-25 September), 70
commits - 8, 3, 25, 29 and 5 a day - in sessions led by one person with several agents,
including pauses when a usage quota ran out. The last four (0.10.1, 0.11.0, 0.12.0, 0.13.0)
went out between 23:34 and 01:48. Roughly 6 900 lines of Python (`coordination/`) and 2 800 of
TypeScript (`clients/ts/src`) today.

**What the first day of this roadmap did.** It was written at 21:18 on 24 September, at 0.10.0;
T22-T26 closed between 21:57 and 23:44 the same evening, and 0.11.0 - milestone 0.11's release -
shipped at 00:35. One evening replaced the active day the table below allots for quality gates,
which is why the forecast is re-run rather than trusted.

**Assumptions.** An *active day* is a day of work like 23 or 24 September. About 4.5 active days
remain before the release candidate (the table above: 1.5 + 1.5, a 24 h soak among them, and
0.5). Three scenarios, re-run on 2026-09-25, the day after milestone 0.11 was reached:

| Scenario | Pace | 0.11 (reached) | 0.14 (reached) | 0.15 | 1.0-rc1 | **1.0** |
|---|---|---|---|---|---|---|
| Optimistic | 5 active days a week, fixes small (3.5 days) | 24-25 Sep | 25 Sep | Sep 28 | Sep 29 | **Oct 6** |
| **Likely** | 4 active days a week, 4.5 days of work | 24-25 Sep | 25 Sep | Sep 30 | Oct 1 | **Oct 8** |
| Pessimistic | 2-3 active days a week, the review or the soak finds real problems (6.5 days) | 24-25 Sep | 25 Sep | Oct 8 | Oct 12 | **Oct 19** |

**Target milestone: 1.0 around 8 October 2026**, with 6-19 October as the range.

**What would move it:**
- T33 (security review) finding a structural issue - the pessimistic line. T25 (lint and types)
  was the other candidate; it found 285 errors, all fixed on 24 September, and no structural
  surprise.
- T42 finding a P0 bug: fix it, and the week of real use starts again.
- Packaging (T40): `coord` is free on PyPI and `coord-client` on npm (checked 2026-09-24), so
  publishing is not blocked; the alternative is to keep the GitHub release assets.
- Available time: the forecast counts active days, so a quiet week shifts every date by that week.

## Keeping it current

Each milestone is a task: when its prerequisites are done, whoever it is for is told it is
unblocked (`task.unblocked`). Re-run the forecast at each milestone with the actual pace, and
update this file with the dates reached.

**Re-runs.** *2026-09-25 (0.13.0), twice over:* 0.11 reached a day early, its numbering settled
the same day (T104: a milestone takes the number of the release it ships in) - and then **0.14,
the contract frozen, reached the same day**: T105, T28, T29, T30, T31 and T32 all closed on
25 September, hours before the forecast's earliest (optimistic) date of 26 September. What is left
starts at the security review, the load test, the guides and the changelog. **1.0 still lands
around 8 October 2026** on every scenario: the remaining effort - 1.5 days of hardening, the 24 h
soak, the release candidate and the week of real use - did not shrink.
