---
description: Join ACP coordination - take a session name, heartbeat, catch up on mail and claims
argument-hint: "[family] [status]"
---

Join the ACP coordination server with `python "${CLAUDE_PLUGIN_ROOT}/skills/acp/scripts/acp.py"`. (If `${CLAUDE_PLUGIN_ROOT}` is not expanded in this agent, use the `scripts/acp.py` next to the `acp` skill's SKILL.md.) Follow the `acp` skill's session protocol.

1. Run `whoami "<family>"`, where family is the first word of the arguments given or, if empty, your own agent kind (`claude`, `codex`, ...). Check the bracketed UUID in the reply matches the one the client attached, then adopt the returned `<family>-NN` as this conversation's session name.
2. Run `heartbeat "<session>: <status>"`, with status from the rest of the arguments given or a one-line summary of the current task.
3. Run `inbox 10` and `locks`.

Report the session name, then briefly summarize anything addressed to you, open items and live claims that overlap the current work.
