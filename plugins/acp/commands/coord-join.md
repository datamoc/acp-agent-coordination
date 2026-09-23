---
description: Join coord v2 coordination - take a session, show context, live claims and new messages
argument-hint: "[family]"
---

Follow the `coord` skill. Run `coord --json whoami <family>`, where family is the first word of the arguments given or, if empty, your own agent kind (`claude`, `codex`, ...). Keep `name` and `session_id` for the rest of the conversation and prefix every later command with `COORD_SESSION=<session_id>`.

Then run `COORD_SESSION=<session_id> coord context` and `coord locks`.

Report the session name, then briefly summarize messages addressed to you, open tasks and discussions, and claims that overlap the current work.
