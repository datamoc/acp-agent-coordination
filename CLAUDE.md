# CLAUDE.md

Project guidance for Claude Code sessions.

## Agent efficiency: use scripts, not agents, for automatable work

**Any task that can be done by a script, shell command, or local tool should be done that way, not by spawning an agent.**

Examples of what **not** to delegate to agents:
- Running tests, linters, type checkers: `uv run test_coord.py`, `mypy`, `pytest`
- Building, deploying, packaging: `uv build`, `cargo build`, `docker build`
- Git operations: `git rebase -i`, `git cherry-pick`, log inspection
- File transformations: regex replacements, bulk edits, code generation scripts
- Searching/grepping: `grep`, `find`, searching through logs or output
- Data processing: `jq`, `python -c`, CSV transforms
- Setting up environments: installing dependencies, configuring tools

Agents are expensive (tokens + latency) and best reserved for:
- **Reasoning**: understanding a bug, designing a system, reviewing complex code
- **Judgment calls**: deciding between architectural options, naming, API design
- **Complex multi-step work**: implementing a feature that requires reading multiple files, cross-file consistency, or domain knowledge
- **Writing from scratch**: generating new code, documentation, tests when the shape is unclear

**Default to scripts.** If a task feels automatable (regex, data munging, running CLI tools, traversing a file tree), write a Python/shell script and run it. Only escalate to an agent if you're stuck or the problem requires reasoning.

When in doubt: before spawning an agent, ask "could I automate this in 5 lines of Python?" If yes, do that instead.
