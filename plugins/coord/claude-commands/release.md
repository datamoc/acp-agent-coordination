---
description: Release a coord claim (or all of this session's claims)
argument-hint: "[C.. | --all]"
---

Run coord with `node "${CLAUDE_PLUGIN_ROOT}/client/cli.js"`. (If `${CLAUDE_PLUGIN_ROOT}` is not expanded in this agent, use the `client/cli.js` two directories up from the `coord` skill's SKILL.md.) Follow the `coord` skill. Use this conversation's session id.

Run `COORD_SESSION=<session_id> ... release <claim>`, or `release --all` when the arguments given are empty or say all. Report what was released.
