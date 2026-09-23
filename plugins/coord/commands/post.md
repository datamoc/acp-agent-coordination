---
description: Post a coord message (a direct one with --to <session>)
argument-hint: "<message> [--to <session>] [--kind question|proposal|warning|done|...]"
---

Run coord with `node "${CLAUDE_PLUGIN_ROOT}/client/cli.js"`. (If `${CLAUDE_PLUGIN_ROOT}` is not expanded in this agent, use the `client/cli.js` two directories up from the `coord` skill's SKILL.md.) Follow the `coord` skill. Use this conversation's session id (`/coord:join` first if none).

Run `COORD_SESSION=<session_id> ... post "<message>"` with the message and options from the arguments given. Keep it under 300 characters; put long content in `doc create` instead. Report the message number.
