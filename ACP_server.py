"""ACP (Agent Communication Protocol) coordination server - PROTOTYPE.

Run: uv run ACP_server.py [-v]
Listens on http://localhost:1337 (loopback only - never expose it;
see README.md "Security"). Default log output shows state-changing
calls (post/resolve/claim/release/request/done/heartbeat/whoami) plus
warnings/errors; -v/--verbose adds the read traffic and framework chatter.

Storage is one SQLite file (`coord.db`, WAL mode - see store.py). The
agent API below is the contract clients rely on: reply shapes and input
grammars stay stable; `mailbox.json`/`presence.json`/`locks.json` are
legacy inputs for the one-time migration only.
"""

import argparse
import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

from acp_sdk.models import Message, MessagePart
from acp_sdk.server import Context, RunYield, RunYieldResume, Server

import store

server = Server()

store.init_db()

# Server start, for the status agent.
STARTED_AT = datetime.now(timezone.utc)

# Terminal visibility: the SDK's executor only logs bare "Run started /
# Run completed" (no agent name, no payload), and uvicorn's access log
# only shows "POST /runs 200 OK". Log one line per agent call with the
# (truncated, single-line) input and one per reply so `uv run
# ACP_server.py -v` shows actual traffic. Uses the "acp" logger the SDK
# configures, so output lands on the same terminal stream.
logger = logging.getLogger("acp.coord")

# High-frequency reads polled every few minutes by idle sessions: useful
# when debugging, noise the rest of the time. Everything else (posts,
# resolves, claims, releases, requests, dones, heartbeats, names) is the
# coordination state itself and stays at INFO so the default view shows
# what matters. -v drops the level to DEBUG so the reads show again.
_READ_ONLY = frozenset({"echo", "inbox", "locks", "presence", "requests", "status"})


def _note(agent: str, message: str) -> None:
    logger.log(
        logging.DEBUG if agent in _READ_ONLY else logging.INFO,
        "%s %s", agent, message,
    )


def _short(text: str, limit: int = 300) -> str:
    one_line = " ".join((text or "").split())
    if len(one_line) <= limit:
        return one_line
    return f"{one_line[:limit]}… [+{len(one_line) - limit} chars]"


def _reply(agent: str, content: str) -> Message:
    _note(agent, f"out: {_short(content)}")
    return Message(parts=[MessagePart(
        content=content,
        content_type="text/plain",
    )])


def _text_of(input: list[Message]) -> str:
    parts = []
    for message in input:
        for part in message.parts:
            if part.content is not None:
                parts.append(part.content)
    return "\n".join(parts).strip()


def _render(entry: dict) -> str:
    line = f"#{entry['n']} [{entry['at']}] {entry['from']}: {entry['message']}"
    if entry.get("resolved_at"):
        note = (entry.get("resolve_note") or "").strip()
        line += f" [resolved{(': ' + note) if note else ''}]"
    return line


@server.agent()
async def echo(
    input: list[Message], context: Context
) -> AsyncGenerator[RunYield, RunYieldResume]:
    """Quickstart sanity check: echoes everything back."""
    text = _text_of(input)
    _note("echo", f"in: {_short(text)}")
    for message in input:
        await asyncio.sleep(0.2)
        yield {"thought": "I should echo everything"}
        await asyncio.sleep(0.2)
        _note("echo", f"out: {_short(text)}")
        yield message


@server.agent()
async def post(
    input: list[Message], context: Context
) -> AsyncGenerator[RunYield, RunYieldResume]:
    """Post a coordination message. Input: "<session-name>: <message text>"
    (session name before the first colon; the rest is the message).
    Appends to the shared mailbox and returns "posted #N from <who>".
    The live mailbox keeps the last 500; older entries roll into
    mailbox-archive-<date>.json, and numbers stay stable."""
    text = _text_of(input)
    _note("post", f"in: {_short(text)}")
    if ":" in text:
        who, _, body = text.partition(":")
        who, body = who.strip(), body.strip()
    else:
        who, body = "unknown", text
    if not body:
        yield _reply("post", "post rejected: empty message body")
        return
    if len(body) > store.MESSAGE_MAX_CHARS:
        yield _reply(
            "post",
            f"post rejected: message too long ({len(body)} > {store.MESSAGE_MAX_CHARS} chars)",
        )
        return
    try:
        entry = store.post_message(who, body)
    except ValueError as e:
        yield _reply("post", f"post rejected: {e}")
        return
    yield _reply("post", f"posted #{entry['n']} from {entry['from']}")


@server.agent()
async def inbox(
    input: list[Message], context: Context
) -> AsyncGenerator[RunYield, RunYieldResume]:
    """Read the mailbox. Input forms (plain text, tokens combine):
    empty for everything; "N" for only the last N; "#N" for messages
    since #N; "since <iso-time>" for messages after a timestamp;
    "<session>" (or "from <session>") for one sender's messages.
    Returns them newest-last, one per line, as "#N [at] from: message"."""
    text = _text_of(input)
    _note("inbox", f"in: {_short(text)}")
    query = store.parse_inbox_query(text)
    entries = store.read_inbox(
        limit=query["limit"],
        since_n=query["since_n"],
        since_t=query["since_t"],
        sender=query["sender"],
    )
    if not entries:
        rendered = "(mailbox empty)"
    else:
        rendered = "\n".join(_render(e) for e in entries)
    _note(
        "inbox",
        f"out: {len(entries)} entries query={query} "
        f"first={_short(rendered, 200)}",
    )
    yield Message(parts=[MessagePart(content=rendered, content_type="text/plain")])


@server.agent()
async def resolve(
    input: list[Message], context: Context
) -> AsyncGenerator[RunYield, RunYieldResume]:
    """Mark a mailbox message done. Input: "#N" or "#N: note".
    Stamps the live entry so inbox shows it as [resolved]."""
    raw = _text_of(input)
    _note("resolve", f"in: {_short(raw)}")
    text = raw.strip().lstrip("#")
    num, _, note = text.partition(":")
    num, note = num.strip(), note.strip()
    if not num.isdigit():
        yield _reply("resolve", 'usage: resolve "#N" or "#N: note"')
        return
    target = int(num)
    outcome = store.resolve_message(target, note)
    if outcome["outcome"] == "missing":
        yield _reply(
            "resolve",
            f"no message #{target} in the live mailbox (may be archived)",
        )
        return
    if outcome["outcome"] == "already":
        yield _reply("resolve", f"#{target} already resolved")
        return
    yield _reply("resolve", f"#{target} resolved" + (f": {note}" if note else ""))


@server.agent()
async def claim(
    input: list[Message], context: Context
) -> AsyncGenerator[RunYield, RunYieldResume]:
    """Claim a file/area so others don't touch it. Input:
    "<session>: <scope>[: <note>]". Holds for 2h; re-claim to extend.
    An expired claim can be taken over. Scopes containing colons
    (Windows paths) should use the pipe form: "<session>: <scope> | <note>";
    a claimed scope also round-trips bare, matched against live claims."""
    raw = _text_of(input)
    _note("claim", f"in: {_short(raw)}")
    known = tuple(l["scope"] for l in store.list_locks(include_expired=True))
    parsed = store.parse_claim(raw, known)
    who, scope, note = parsed["owner"], parsed["scope"], parsed["note"]
    if not scope:
        yield _reply("claim", 'usage: claim "<session>: <scope>[: <note>]"')
        return
    try:
        result = store.claim_lock(who or "unknown", scope, note)
    except ValueError:
        yield _reply("claim", 'usage: claim "<session>: <scope>[: <note>]"')
        return
    if not result["ok"]:
        held = result["held_by"]
        yield _reply(
            "claim",
            f"scope '{scope}' already claimed by {held.get('owner')} until {held.get('until')}",
        )
        return
    yield _reply(
        "claim",
        f"scope '{scope}' claimed by {result['lock']['owner']} until {result['lock']['until']}",
    )


@server.agent()
async def release(
    input: list[Message], context: Context
) -> AsyncGenerator[RunYield, RunYieldResume]:
    """Release a claim. Input: "<session>: <scope>". Only the holder
    (or anyone, once expired) can release. A claimed scope round-trips
    bare even when it contains colons."""
    raw = _text_of(input)
    _note("release", f"in: {_short(raw)}")
    known = tuple(l["scope"] for l in store.list_locks(include_expired=True))
    parsed = store.parse_claim(raw, known)
    who, scope = parsed["owner"], parsed["scope"]
    if not scope:
        yield _reply("release", 'usage: release "<session>: <scope>"')
        return
    result = store.release_lock(who or "unknown", scope)
    if not result["ok"]:
        held = result["held_by"]
        yield _reply(
            "release",
            f"scope '{scope}' is claimed by {held.get('owner')}; only they can release it",
        )
        return
    if result.get("was") == "absent":
        yield _reply("release", f"scope '{scope}' is not claimed")
        return
    yield _reply("release", f"scope '{scope}' released")


@server.agent()
async def locks(
    input: list[Message], context: Context
) -> AsyncGenerator[RunYield, RunYieldResume]:
    """List claims. Empty input shows live claims; "all" includes expired
    ones. One per line: "[until] scope <- owner[: note]"."""
    raw = _text_of(input)
    _note("locks", f"in: {_short(raw)}")
    show_all = raw.strip().lower() == "all"
    lines = []
    for e in store.list_locks(include_expired=show_all):
        note = (e.get("note") or "").strip()
        lines.append(
            f"[{e.get('until', '?')}] {e.get('scope', '?')} <- {e.get('owner', '?')}"
            + (f": {note}" if note else "")
            + (" [expired]" if e.get("expired") else "")
        )
    rendered = "\n".join(lines) if lines else "(no active claims)"
    _note("locks", f"out: {len(lines)} claims")
    yield Message(parts=[MessagePart(content=rendered, content_type="text/plain")])


@server.agent()
async def request(
    input: list[Message], context: Context
) -> AsyncGenerator[RunYield, RunYieldResume]:
    """Open a task request for another agent. Input: "<session>: <task>".
    Returns "request #R opened" - request numbers are a separate sequence
    from mailbox #N. Check `requests` for the queue, `done` to close."""
    text = _text_of(input)
    _note("request", f"in: {_short(text)}")
    if ":" in text:
        who, _, task = text.partition(":")
        who, task = who.strip(), task.strip()
    else:
        who, task = "unknown", text.strip()
    if not task:
        yield _reply("request", 'usage: request "<session>: <task>"')
        return
    try:
        opened = store.open_request(who or "unknown", task)
    except ValueError as e:
        yield _reply("request", f"request rejected: {e}")
        return
    yield _reply("request", f"request #{opened['id']} opened")


@server.agent()
async def requests(
    input: list[Message], context: Context
) -> AsyncGenerator[RunYield, RunYieldResume]:
    """List task requests, oldest first. Empty input shows open ones;
    "all" includes closed ones: "#R [at] requester: task[ [done by X: note]]"."""
    raw = _text_of(input)
    _note("requests", f"in: {_short(raw)}")
    show_all = raw.strip().lower() == "all"
    rows = store.list_requests(open_only=not show_all)
    lines = []
    for r in rows:
        line = f"#{r['id']} [{r['created_at']}] {r['requester']}: {r['task']}"
        if r["status"] != "open":
            note = (r["close_note"] or "").strip()
            line += f" [done by {r['closer'] or 'unknown'}{(': ' + note) if note else ''}]"
        lines.append(line)
    if not lines:
        rendered = "(no open requests)" if show_all else "(no open requests)"
        if show_all and not rows:
            rendered = "(no requests)"
    else:
        rendered = "\n".join(lines)
    _note("requests", f"out: {len(lines)} rows")
    yield Message(parts=[MessagePart(content=rendered, content_type="text/plain")])


@server.agent()
async def done(
    input: list[Message], context: Context
) -> AsyncGenerator[RunYield, RunYieldResume]:
    """Close a task request. Input: "<session>: #R[: <note>]".
    The session prefix records who did the work."""
    text = _text_of(input)
    _note("done", f"in: {_short(text)}")
    if ":" in text:
        who, _, rest = text.partition(":")
        who = who.strip() or "unknown"
    else:
        who, rest = "unknown", text
    num, _, note = rest.strip().lstrip("#").partition(":")
    num, note = num.strip(), note.strip()
    if not num.isdigit():
        yield _reply("done", 'usage: done "<session>: #R[: <note>]"')
        return
    target = int(num)
    outcome = store.close_request(target, who, note)
    if outcome["outcome"] == "missing":
        yield _reply("done", f"no request #{target}")
        return
    if outcome["outcome"] == "already":
        yield _reply("done", f"request #{target} already closed")
        return
    yield _reply("done", f"request #{target} closed" + (f": {note}" if note else ""))


@server.agent()
async def whoami(
    input: list[Message], context: Context
) -> AsyncGenerator[RunYield, RunYieldResume]:
    """Take a numbered session name. Input: a family ("muse", "opencode",
    "codex", ...), optionally followed by a caller-chosen UUID
    ("<family> <uuid>"). Returns "you are <family>-NN" - the smallest free
    number, heartbeated immediately so concurrent starters cannot draw
    the same one. A     number stays yours while you heartbeat inside the
    presence TTL; a stale holder's number is reusable. When a UUID is
    given it is echoed back ("you are <family>-NN [<uuid>]") so the caller
    can verify the reply is theirs. Use the returned
    name for every other agent."""
    raw = _text_of(input).strip()
    _note("whoami", f"in: {_short(raw)}")
    tokens = raw.split()
    family = tokens[0] if tokens else ""
    nonce = tokens[1] if len(tokens) > 1 else ""
    if nonce:
        try:
            uuid.UUID(nonce)
        except ValueError:
            yield _reply(
                "whoami", 'usage: whoami "<family> [<uuid>]" (family is '
                'letters/digits/_/-, e.g. "muse")'
            )
            return
    try:
        name = store.claim_instance(family)
    except ValueError:
        yield _reply(
            "whoami", 'usage: whoami "<family> [<uuid>]" (family is '
            'letters/digits/_/-, e.g. "muse")'
        )
        return
    except RuntimeError as e:
        yield _reply("whoami", str(e))
        return
    yield _reply("whoami", f"you are {name}" + (f" [{nonce}]" if nonce else ""))


@server.agent()
async def heartbeat(
    input: list[Message], context: Context
) -> AsyncGenerator[RunYield, RunYieldResume]:
    """Mark a session as live. Input: "<session-name>[: <status>]".
    The roster survives restarts; entries dead over a week are pruned."""
    text = _text_of(input)
    _note("heartbeat", f"in: {_short(text)}")
    if ":" in text:
        who, _, status = text.partition(":")
        who, status = who.strip(), status.strip() or "live"
    else:
        who, status = text.strip(), "live"
    if not who:
        who = "unknown"
    entry = store.heartbeat(who, status)
    yield _reply("heartbeat", f"heartbeat from {entry['session']}")


@server.agent()
async def presence(
    input: list[Message], context: Context
) -> AsyncGenerator[RunYield, RunYieldResume]:
    """List live sessions. Input: empty for the default TTL window,
    an integer N for live within the last N seconds, or "all" for
    every known session including stale ones."""
    text = _text_of(input)
    _note("presence", f"in: {_short(text)}")
    lowered = text.strip().lower()
    if lowered == "all":
        selected = store.list_presence(window_seconds=None)
    else:
        window = store.PRESENCE_TTL_SECONDS
        if lowered.isdigit():
            window = int(lowered)
        selected = store.list_presence(window_seconds=window)
    if not selected:
        rendered = "(no live sessions)"
    else:
        rendered = "\n".join(
            f"[{s.get('last_seen', '?')}] {s.get('session', '?')}: {s.get('status', 'live')}"
            for s in selected
        )
    _note("presence", f"out: {len(selected)} sessions")
    yield Message(parts=[MessagePart(content=rendered, content_type="text/plain")])


@server.agent()
async def status(
    input: list[Message], context: Context
) -> AsyncGenerator[RunYield, RunYieldResume]:
    """Quick triage: uptime, live mailbox size, archived total,
    live sessions, active claims."""
    raw = _text_of(input)
    _note("status", f"in: {_short(raw)}")
    now = datetime.now(timezone.utc)
    live_count = store.message_count()
    archived_total = store.archived_total()
    sessions = store.list_presence(window_seconds=None)
    cutoff_count = len(store.list_presence(window_seconds=store.PRESENCE_TTL_SECONDS))
    active_locks = len(store.list_locks())
    uptime = now - STARTED_AT
    content = (f"uptime {uptime} | mailbox live {live_count} / archived {archived_total} | "
               f"sessions live {cutoff_count}/{len(sessions)} | claims active {active_locks}")
    _note("status", f"out: {content}")
    yield Message(parts=[MessagePart(content=content, content_type="text/plain")])


def _configure_logging(verbose: bool) -> None:
    """Quiet the per-request chatter unless -v was passed.

    The SDK executor logs bare "Run started / Run completed" at INFO and
    uvicorn logs every POST /runs at INFO: no agent name, no payload, no
    status beyond 200 OK. Both go through the "acp" logger (ours,
    "acp.coord", is its child), so one level switch covers both loggers;
    uvicorn's own access log is disabled separately in main(). Our own
    logger gets an explicit level too: it is a child of "acp", so without
    this the WARNING above would hide the INFO traffic lines as well.
    Warnings and errors always show, in either mode."""
    logging.getLogger("acp").setLevel(logging.INFO if verbose else logging.WARNING)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="also log read traffic and framework chatter "
        "(default: state-changing calls plus warnings/errors)",
    )
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    # 8000/8100 are commonly taken by other dev tools; 1337 is this
    # project's own port until it moves into a dedicated setup.
    if args.verbose:
        server.run(port=1337)
    else:
        print("serving on http://localhost:1337 (quiet; -v for the traffic log)")
        server.run(port=1337, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
