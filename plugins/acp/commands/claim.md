---
description: Claim a file or area in ACP before editing it (2h hold)
argument-hint: "<file-or-area> [: note]"
---

Use the `<family>-NN` session name this conversation already took with `whoami`; if there is none yet, run `/acp:join` first.

1. Run `locks` with `python "${CLAUDE_PLUGIN_ROOT}/skills/acp/scripts/acp.py"` and check whether a live claim by another session overlaps the arguments given. If one does, do not claim: report the holder and suggest posting to them. (If `${CLAUDE_PLUGIN_ROOT}` is not expanded in this agent, use the `scripts/acp.py` next to the `acp` skill's SKILL.md.) Follow the `acp` skill's session protocol.
2. Otherwise run `claim "<session>: <arguments>"`. If the scope itself contains colons (Windows paths), use the pipe form `"<session>: <scope> | <note>"`.

Report the scope and expiry, and offer to post `T <scope> <note>` so peers see it.
