# CLAUDE.md

Project guidance for Claude Code sessions.

## Agent efficiency: scripts first, agents for reasoning

**Core principle: any task automatable by a script, command, or tool should be done that way—not by spawning an agent.** Agents cost tokens and latency. In multi-agent coordination, spawning agents for automatable work wastes tokens on coordination overhead instead of actual work.

**Use scripts for:** running tests, linting, building, git operations, file transformations, searching/grepping, data processing, environment setup, log inspection, data validation, batch operations.

**Use agents only for:** bug diagnosis, system design, code review, architecture decisions, writing new code when the shape is unclear, complex multi-file changes requiring cross-file consistency, judgment calls on naming/API design, reasoning about trade-offs.

**Token economy:** In a coordination scenario (multiple agents working together), every agent call is a token cost. Before spawning an agent to do something, ask: "Could a 5-line Python script do this?" If yes, run the script instead. This keeps tokens for actual reasoning work, not coordination overhead.

Examples:
- ❌ Spawn an agent to run tests → ✅ `uv run test_coord.py`
- ❌ Spawn an agent to search a file → ✅ `grep pattern file` or `find . -name "*.py"`
- ❌ Spawn an agent to fix merge conflicts → ✅ `git status`, manual resolution, `git add`
- ✅ Spawn an agent to review a complex refactor
- ✅ Spawn an agent to design a new system
- ✅ Spawn an agent to debug a subtle race condition

**Default to scripts.** Escalate to an agent only when you're stuck or the problem genuinely requires reasoning.

## coord: state lives in the server, not in the chat

The full session protocol is AGENTS.md `## Using coord (agents)` and README `## For agents`.
What changed recently and an agent should not have to rediscover:

- **Where things stand:** `coord dashboard` (per project: asleep agents with work, waiting
  answers, blocked discussions, failed wake-ups, candidates, next milestones),
  `coord activity --since 2h`, `coord tasks --view ready|blocked|milestones`,
  `coord unblock-points`, `coord milestones`.
- **Receipts:** a directed or `--priority high|urgent` message asks to be acknowledged -
  `coord ack 42 taken|done|declined "why"`, `coord receipts 42`. Priority is attention, never
  authority.
- **Sleeping agents:** `coord agents`, `coord pause "why"`,
  `coord wake request <agent> --reason task --ref T12`,
  `coord wake answer W3 refuse "why"`.
- **Permissions:** a project with no members is open to everyone; once it has members the rights
  stack `viewer` → `contributor` → `decider` → `admin` (`coord members`). A `forbidden` names
  the admins to ask - do not retry.
- **Milestones:** reached ones with dates, then upcoming ones with criteria met and tasks left;
  a date projection only when there is enough history. No invented percentages, no completion
  guesses - say "not reached" when that is the answer.
