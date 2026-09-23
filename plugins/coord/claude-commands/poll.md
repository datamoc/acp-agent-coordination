---
description: Check coord for new messages, tasks and discussions since the last poll
---

Run coord with `node "${CLAUDE_PLUGIN_ROOT}/client/cli.js"`. (If `${CLAUDE_PLUGIN_ROOT}` is not expanded in this agent, use the `client/cli.js` two directories up from the `coord` skill's SKILL.md.) Follow the `coord` skill. Use the session id this conversation took with `whoami`; if there is none, run `/coord:join` first.

Run `COORD_SESSION=<session_id> ... poll`. Summarize only what matters: messages to this session or touching its work, tasks it could take, discussions awaiting it. Say "nothing new" if that's the case.
