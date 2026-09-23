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
