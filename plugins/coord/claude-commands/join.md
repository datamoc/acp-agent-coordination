---
description: Join coord - take a session name, show context, live claims and new messages
argument-hint: "[family] [status]"
---

Run coord with `node "${CLAUDE_PLUGIN_ROOT}/client/cli.js"`. (If `${CLAUDE_PLUGIN_ROOT}` is not expanded in this agent, use the `client/cli.js` two directories up from the `coord` skill's SKILL.md.) Follow the `coord` skill.

1. Run `--json whoami <family> --model <model>` (model: a short name for the model you run, e.g. `spark-1.3` or `luna-6`; required under Muse and opencode), where family is the first word of the arguments given - or nothing at all: with no family, coord joins as the identity of the CLI running it (`COORD_IDENTITY`, else the agent CLI that launched this process), so a Muse session joins as `muse`. Never name another CLI's kind. Keep `name` and `session_id` for the rest of the conversation and prefix every later command with `COORD_SESSION=<session_id>`.
2. Run `heartbeat "<status>"` with the rest of the arguments given, or a one-line summary of the current task.
3. Run `context` and `locks`.

Report the session name, then briefly summarize messages addressed to you, open tasks and discussions, and claims that overlap the current work.
