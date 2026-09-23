---
description: Claim a file or directory (trailing / = whole tree) before editing it
argument-hint: "<path>[/] [note]"
---

Run coord with `node "${CLAUDE_PLUGIN_ROOT}/client/cli.js"`. (If `${CLAUDE_PLUGIN_ROOT}` is not expanded in this agent, use the `client/cli.js` two directories up from the `coord` skill's SKILL.md.) Follow the `coord` skill. Use this conversation's session id (`/coord:join` first if none).

Run `COORD_SESSION=<session_id> ... claim <path> --note "<note>"` with the path and note from the arguments given. On `conflict`, do not edit: report who holds it and offer to `ask` them. Otherwise report the claim id (C..).
