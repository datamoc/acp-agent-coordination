---
description: Release ACP claims held by this session
argument-hint: "[scope]  (empty = every claim this session holds)"
---

Use the `<family>-NN` session name this conversation already took with `whoami`; if there is none yet, run `/acp:join` first.

With `python "${CLAUDE_PLUGIN_ROOT}/skills/acp/scripts/acp.py"`: (If `${CLAUDE_PLUGIN_ROOT}` is not expanded in this agent, use the `scripts/acp.py` next to the `acp` skill's SKILL.md.) Follow the `acp` skill's session protocol.

- If a scope is given as arguments, run `release "<session>: <arguments>"`.
- If empty, run `locks`, then `release` every live claim held by this session.

Then, if work was finished, offer to post a compact `D` summary (`@sha`, what changed, what's open, `ok:`/`x:` gates, `R claim`); otherwise post `R <scope>`.
