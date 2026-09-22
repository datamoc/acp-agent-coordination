"""Smoke test: agents round-trip against a temp database.

No live server needed - it repoints store.DB_PATH at a tmp dir and drives
the agents directly. Run: uv run smoke_test.py
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import store
import ACP_server as s
from acp_sdk.models import Message, MessagePart


def msg(text: str) -> list[Message]:
    return [Message(parts=[MessagePart(content=text, content_type="text/plain")])]


async def out(agent, text: str) -> str:
    parts = []
    async for item in agent(msg(text), None):
        if isinstance(item, dict):
            continue
        for part in item.parts:
            parts.append(part.content)
    return "\n".join(parts)


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="acp-smoke-"))
    store.DB_PATH = tmp / "coord.db"
    store.init_db()

    r = await out(s.post, "alice: first")
    assert r == "posted #1 from alice", r
    r = await out(s.post, "bob: second")
    assert r == "posted #2 from bob", r

    r = await out(s.inbox, "")
    assert "#1" in r and "#2" in r and "alice" in r and "bob" in r, r
    r = await out(s.inbox, "1")
    assert "#2" in r and "#1" not in r, r
    r = await out(s.inbox, "#1")
    assert "#2" in r and "#1" not in r, r
    r = await out(s.inbox, "alice")
    assert "alice" in r and "bob" not in r, r
    r = await out(s.inbox, "bob #1")
    assert "bob" in r and "alice" not in r and "#2" in r, r
    r = await out(s.inbox, "nobody")
    assert r == "(mailbox empty)", r

    r = await out(s.resolve, "#1: done it")
    assert r == "#1 resolved: done it", r
    r = await out(s.resolve, "#1")
    assert r == "#1 already resolved", r
    r = await out(s.resolve, "#99")
    assert "no message #99" in r, r
    r = await out(s.resolve, "bogus")
    assert r.startswith("usage:"), r
    r = await out(s.inbox, "")
    assert "[resolved: done it]" in r, r

    r = await out(s.heartbeat, "alice: working")
    assert r == "heartbeat from alice", r
    r = await out(s.presence, "")
    assert "alice" in r, r

    # Numbered instances: families draw sequential names, concurrently safe.
    r = await out(s.whoami, "muse")
    assert r == "you are muse-01", r
    r = await out(s.whoami, "muse")
    assert r == "you are muse-02", r
    r = await out(s.whoami, "has space")
    assert r.startswith("usage:"), r
    # Caller UUIDs are echoed so replies can be matched to requests.
    r = await out(s.whoami, "muse 12345678-1234-5678-1234-567812345678")
    assert r == "you are muse-03 [12345678-1234-5678-1234-567812345678]", r
    r = await out(s.whoami, "muse not-a-uuid")
    assert r.startswith("usage:"), r
    r = await out(s.whoami, "muse")
    assert r == "you are muse-04", r
    r = await out(s.presence, "")
    assert "muse-01" in r and "muse-02" in r, r

    # Post validation: empty and oversized bodies are rejected, not stored.
    before = store.message_count()
    r = await out(s.post, "alice: ")
    assert r == "post rejected: empty message body", r
    r = await out(s.post, "")
    assert r == "post rejected: empty message body", r
    r = await out(s.post, "alice: " + "x" * 4001)
    assert r.startswith("post rejected: message too long"), r
    assert store.message_count() == before, "rejected post was stored"

    # ISO-time and from-keyword inbox filters.
    r = await out(s.post, "carol: timed")
    assert r.startswith("posted #"), r
    r = await out(s.inbox, "since 2000-01-01T00:00:00+00:00")
    assert "carol" in r, r
    r = await out(s.inbox, "since 2999-01-01T00:00:00+00:00")
    assert r == "(mailbox empty)", r
    r = await out(s.inbox, "from carol")
    assert "carol" in r and "alice" not in r, r

    # Locks: claim, conflict, release, takeover of expired, listing.
    r = await out(s.claim, "alice: src/combat.ts: rework rolls")
    assert "claimed by alice" in r, r
    r = await out(s.claim, "bob: src/combat.ts")
    assert "already claimed by alice" in r, r
    r = await out(s.locks, "")
    assert "src/combat.ts <- alice" in r, r
    r = await out(s.release, "bob: src/combat.ts")
    assert "only they can release it" in r, r
    r = await out(s.release, "alice: src/combat.ts")
    assert r == "scope 'src/combat.ts' released", r
    r = await out(s.locks, "")
    assert r == "(no active claims)", r
    r = await out(s.claim, "alice: ")
    assert r.startswith("usage:"), r
    r = await out(s.release, "alice: nothing")
    assert "is not claimed" in r, r
    # Windows drive-colon scopes survive via the pipe form.
    r = await out(s.claim, r"alice: C:\proj\src\win.ts | line endings")
    assert "claimed by alice" in r, r
    r = await out(s.locks, "")
    assert r"C:\proj\src\win.ts <- alice: line endings" in r, r
    r = await out(s.release, r"alice: C:\proj\src\win.ts")
    assert r == r"scope 'C:\proj\src\win.ts' released", r
    # Expired claims can be taken over and released by anyone.
    with store.connect() as db:
        db.execute(
            "INSERT INTO locks(scope, owner, note, claimed_at, until) "
            "VALUES (?, ?, ?, ?, ?)",
            ("old.ts", "ghost", "", "2000-01-01T00:00:00+00:00",
             "2000-01-01T00:00:00+00:00"),
        )
    r = await out(s.locks, "")
    assert r == "(no active claims)", r
    r = await out(s.locks, "all")
    assert "[expired]" in r, r
    r = await out(s.claim, "bob: old.ts: taking over")
    assert "claimed by bob" in r, r
    r = await out(s.release, "bob: old.ts")
    assert "released" in r, r

    # Requests: open, list, close, repeat-close, unknown id.
    r = await out(s.request, "alice: sync the client please")
    assert r == "request #1 opened", r
    r = await out(s.request, "bob: ")
    assert r.startswith("usage:"), r
    r = await out(s.requests, "")
    assert "#1" in r and "sync the client" in r, r
    r = await out(s.done, "carol: #1: synced")
    assert r == "request #1 closed: synced", r
    r = await out(s.done, "carol: #1")
    assert r == "request #1 already closed", r
    r = await out(s.done, "carol: #99")
    assert r == "no request #99", r
    r = await out(s.done, "bogus")
    assert r.startswith("usage:"), r
    r = await out(s.requests, "")
    assert r == "(no open requests)", r
    r = await out(s.requests, "all")
    assert "[done by carol: synced]" in r, r

    # Presence pruning: week-old entries die on the next heartbeat.
    with store.connect() as db:
        db.execute(
            "INSERT INTO presence(session, status, last_seen) VALUES (?, ?, ?)",
            ("ancient", "gone", "2000-01-01T00:00:00+00:00"),
        )
    await out(s.heartbeat, "alice: still here")
    assert "ancient" not in {
        e["session"] for e in store.list_presence(window_seconds=None)
    }, "stale presence not pruned"

    # Status triage line.
    r = await out(s.status, "")
    assert "mailbox live" in r and "claims active" in r and "sessions live" in r, r

    # Pruning: cap the live mailbox, archive the rest, keep numbers stable.
    store.MAILBOX_CAP = 10
    try:
        for i in range(12):
            await out(s.post, f"spam: {i}")
        live = store.read_inbox()
        assert len(live) == 10, len(live)
        assert [e["n"] for e in live] == list(range(6, 16)), [e["n"] for e in live]
        archives = list(tmp.glob("mailbox-archive-*.json"))
        assert len(archives) == 1, archives
        archived = json.loads(archives[0].read_text())
        assert [e["n"] for e in archived] == [1, 2, 3, 4, 5], [e["n"] for e in archived]
        r = await out(s.post, "alice: after prune")
        assert r == "posted #16 from alice", r
        assert not list(tmp.glob("*.tmp")), "tmp file left behind"

        # Restart resilience: reload from disk and re-read.
        assert store.message_count() == 10, "mailbox did not survive reload"
        assert "alice" in {
            e["session"] for e in store.list_presence(window_seconds=None)
        }, "presence did not survive reload"
    finally:
        store.MAILBOX_CAP = 5000

    print("SMOKE OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"SMOKE FAIL: {e}")
        raise SystemExit(1)
