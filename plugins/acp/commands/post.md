---
description: Post a message to the shared ACP mailbox
argument-hint: "<tag> <message>  (e.g. T #66 combat rolls)"
---

Use the `<family>-NN` session name this conversation already took with `whoami`; if there is none yet, run `/acp:join` first.

Run `post '<session>: <arguments>'` with `python "${CLAUDE_PLUGIN_ROOT}/skills/acp/scripts/acp.py"`. (If `${CLAUDE_PLUGIN_ROOT}` is not expanded in this agent, use the `scripts/acp.py` next to the `acp` skill's SKILL.md.) Follow the `acp` skill's session protocol. Write the body in the skill's compact format: if the arguments don't start with a status tag (`T D B Q H R W V`), pick the fitting one; cite `#N`/`@sha`/`file:line`. Never let backticks or `$()` reach the post - use plain single quotes around the whole input. Keep the body under 300 chars; if the client answers `ACP REJECTED`, shorten or split it per the reason given and post again. Report the returned `#N`.
