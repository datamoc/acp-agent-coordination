---
description: Check ACP for new mail, open requests and active claims since the last poll
---

Use the `<family>-NN` session name this conversation already took with `whoami`; if there is none yet, run `/acp:join` first.

Run `poll <session>` with `python "${CLAUDE_PLUGIN_ROOT}/skills/acp/scripts/acp.py"`. (If `${CLAUDE_PLUGIN_ROOT}` is not expanded in this agent, use the `scripts/acp.py` next to the `acp` skill's SKILL.md.) Follow the `acp` skill's session protocol.

Summarize only what matters: messages addressed to this session or touching its work, open requests it could take, and claims overlapping its files. Say "nothing new" if that's the case.
