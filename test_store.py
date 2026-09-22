"""Stdlib-only tests for store.py. Run: uv run python test_store.py
Uses throwaway databases under the system temp dir - never touches coord.db.
"""

import json
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import store

PASS = []


def check(name, fn):
    fn()
    PASS.append(name)
    print(f"PASS {name}")


def fresh_db():
    tmp = Path(tempfile.mkdtemp())
    path = tmp / "test.db"
    store.init_db(path)
    return path


def test_post_and_read_order():
    path = fresh_db()
    first = store.post_message("alice", "hello", path)
    second = store.post_message("bob", "world", path)
    assert second["n"] == first["n"] + 1, "stable numbers are monotonic"
    entries = store.read_inbox(path=path)
    assert [e["from"] for e in entries] == ["alice", "bob"], "newest-last order"
    assert entries[0]["message"] == "hello"


def test_post_validation():
    path = fresh_db()
    for bad in ("   ", "x" * (store.MESSAGE_MAX_CHARS + 1)):
        try:
            store.post_message("alice", bad, path)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid body must raise")
    entry = store.post_message("", "no sender here", path)
    assert entry["from"] == "unknown", "missing sender defaults"
    assert store.message_count(path) == 1, "rejected posts store nothing"


def test_inbox_filters():
    path = fresh_db()
    store.post_message("alice", "one", path)
    store.post_message("bob", "two", path)
    store.post_message("alice", "three", path)
    assert len(store.read_inbox(limit=2, path=path)) == 2
    assert store.read_inbox(limit=2, path=path)[0]["message"] == "two"
    assert [e["message"] for e in store.read_inbox(sender="alice", path=path)] == [
        "one",
        "three",
    ]
    assert [e["n"] for e in store.read_inbox(since_n=1, path=path)] == [2, 3]
    # Same-second posts share a timestamp, so range-filtering needs distinct
    # timestamps - insert them directly rather than relying on the clock.
    path2 = fresh_db()
    with store.connect(path2) as db:
        for body, at in [
            ("old", "2026-09-20T10:00:00+00:00"),
            ("mid", "2026-09-21T10:00:00+00:00"),
            ("new", "2026-09-21T12:00:00+00:00"),
        ]:
            db.execute(
                "INSERT INTO messages(sender, body, at) VALUES (?, ?, ?)",
                ("s", body, at),
            )
    got = store.read_inbox(since_t="2026-09-21T10:00:00+00:00", path=path2)
    assert [e["message"] for e in got] == ["new"], "since is exclusive"
    assert store.read_inbox(since_t="2999-01-01T00:00:00+00:00", path=path2) == []


def test_inbox_query_grammar():
    assert store.parse_inbox_query("") == {
        "limit": None,
        "since_n": None,
        "since_t": None,
        "sender": None,
    }
    assert store.parse_inbox_query("5")["limit"] == 5
    assert store.parse_inbox_query("#12")["since_n"] == 12
    q = store.parse_inbox_query("bob #1")
    assert q["sender"] == "bob" and q["since_n"] == 1
    q = store.parse_inbox_query("from alice")
    assert q["sender"] == "alice" and q["limit"] is None
    q = store.parse_inbox_query("since 2026-09-21T13:00:00+00:00")
    assert q["since_t"] == "2026-09-21T13:00:00+00:00"
    # `from` consumes the rest of the line, so it comes last when combining.
    q = store.parse_inbox_query("since 2026-09-21T13:00:00+00:00 from alice")
    assert q["sender"] == "alice" and q["since_t"] == "2026-09-21T13:00:00+00:00"
    q = store.parse_inbox_query("5 from alice")
    assert q["sender"] == "alice" and q["limit"] == 5


def test_resolve_flow():
    path = fresh_db()
    store.post_message("alice", "do the thing", path)
    assert store.resolve_message(1, "done", path) == {"outcome": "ok"}
    assert store.resolve_message(1, "again", path) == {"outcome": "already"}
    assert store.resolve_message(99, "", path) == {"outcome": "missing"}
    entry = store.read_inbox(path=path)[0]
    assert entry["resolved_at"] and entry["resolve_note"] == "done"


def test_cap_and_archive():
    path = fresh_db()
    real_cap = store.MAILBOX_CAP
    store.MAILBOX_CAP = 5
    try:
        for i in range(8):
            store.post_message("s", f"msg-{i}", path)
        assert store.message_count(path) == 5, "live mailbox capped"
        live = store.read_inbox(path=path)
        assert [e["message"] for e in live] == [f"msg-{i}" for i in range(3, 8)]
        assert [e["n"] for e in live] == [4, 5, 6, 7, 8], "numbers stay stable"
        archives = sorted(path.parent.glob("mailbox-archive-*.json"))
        assert len(archives) == 1, archives
        archived = json.loads(archives[0].read_text(encoding="utf-8"))
        assert [e["n"] for e in archived] == [1, 2, 3]
        nxt = store.post_message("s", "after prune", path)
        assert nxt["n"] == 9, "ids never reused"
    finally:
        store.MAILBOX_CAP = real_cap


def test_heartbeat_and_presence():
    path = fresh_db()
    store.heartbeat("alice", "on task X", path)
    store.heartbeat("bob", "", path)
    live = store.list_presence(window_seconds=3600, path=path)
    assert {e["session"] for e in live} == {"alice", "bob"}
    assert [e for e in live if e["session"] == "bob"][0]["status"] == "live"
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    with store.connect(path) as db:
        db.execute(
            "INSERT INTO presence(session, status, last_seen) VALUES (?, ?, ?)",
            ("ghost", "gone", old),
        )
    assert "ghost" not in {
        e["session"]
        for e in store.list_presence(
            window_seconds=store.PRESENCE_TTL_SECONDS, path=path
        )
    }, "2h-old entry outside the 30min TTL"
    assert "ghost" in {
        e["session"] for e in store.list_presence(window_seconds=None, path=path)
    }
    ancient = (
        datetime.now(timezone.utc) - timedelta(days=8)
    ).isoformat(timespec="seconds")
    with store.connect(path) as db:
        db.execute(
            "INSERT INTO presence(session, status, last_seen) VALUES (?, ?, ?)",
            ("ancient", "gone", ancient),
        )
    store.heartbeat("alice", "still here", path)
    assert "ancient" not in {
        e["session"] for e in store.list_presence(window_seconds=None, path=path)
    }


def test_claim_release_locks():
    path = fresh_db()
    ok = store.claim_lock("alice", "src/foo.ts", "refactor", path=path)
    assert ok["ok"] is True
    denied = store.claim_lock("bob", "src/foo.ts", "also refactor", path=path)
    assert denied["ok"] is False
    assert denied["held_by"]["owner"] == "alice"
    again = store.claim_lock("alice", "src/foo.ts", "still at it", path=path)
    assert again["ok"] is True, "re-claiming your own scope refreshes"
    refused = store.release_lock("bob", "src/foo.ts", path=path)
    assert refused["ok"] is False
    assert store.release_lock("alice", "src/foo.ts", path=path)["ok"] is True
    assert store.claim_lock("bob", "src/foo.ts", path=path)["ok"] is True
    assert store.list_locks(path=path)[0]["owner"] == "bob"
    assert store.release_lock("nobody", "nothing", path=path) == {
        "ok": True,
        "was": "absent",
    }
    for owner, scope in (("", "src/bar.ts"), ("alice", "  ")):
        try:
            store.claim_lock(owner, scope, path=path)
        except ValueError:
            pass
        else:
            raise AssertionError("empty owner/scope must raise")


def test_claim_expiry():
    path = fresh_db()
    past = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(timespec="seconds")
    with store.connect(path) as db:
        db.execute(
            "INSERT INTO locks(scope, owner, note, claimed_at, until) "
            "VALUES (?, ?, ?, ?, ?)",
            ("src/old.ts", "ghost", "", past, past),
        )
    assert store.claim_lock("alice", "src/old.ts", path=path)["ok"] is True
    assert store.list_locks(path=path)[0]["owner"] == "alice"
    with store.connect(path) as db:
        db.execute(
            "INSERT INTO locks(scope, owner, note, claimed_at, until) "
            "VALUES (?, ?, ?, ?, ?)",
            ("src/older.ts", "ghost", "", past, past),
        )
    assert any(
        l["scope"] == "src/older.ts" for l in store.list_locks(True, path)
    ), "expired rows visible on request"


def test_claim_lock_race():
    # Concurrent first-time claimants on the same never-before-claimed
    # scope must not all win: exactly one gets ok=True. Regression test
    # for a TOCTOU race (SELECT-then-INSERT with no transaction) that
    # let every racer see no existing row and all insert successfully.
    path = fresh_db()
    outcomes = []
    barrier = threading.Barrier(8)

    def claim(i):
        barrier.wait()
        outcomes.append(
            store.claim_lock(f"agent{i}", "src/contested.ts", "racing", path=path)["ok"]
        )

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(outcomes) == 1, f"exactly one claimant should win, got {outcomes}"
    live = store.list_locks(path=path)
    assert len(live) == 1, live


def test_claim_grammar():
    assert store.parse_claim("alice: src/foo.ts") == {
        "owner": "alice",
        "scope": "src/foo.ts",
        "note": "",
    }
    # Legacy second-colon form still parses.
    assert store.parse_claim("alice: src/foo.ts: rework rolls") == {
        "owner": "alice",
        "scope": "src/foo.ts",
        "note": "rework rolls",
    }
    # Windows drive colons belong to the scope via the pipe form.
    parsed = store.parse_claim(r"alice: C:\proj\src\foo.ts | refactor slice")
    assert parsed["owner"] == "alice"
    assert parsed["scope"] == r"C:\proj\src\foo.ts"
    assert parsed["note"] == "refactor slice"
    # ...and a claimed scope round-trips bare through the known-scopes match.
    parsed = store.parse_claim(
        r"alice: C:\proj\src\foo.ts", (r"C:\proj\src\foo.ts",)
    )
    assert parsed["scope"] == r"C:\proj\src\foo.ts"
    assert parsed["note"] == ""


def test_migrate_json():
    tmp = Path(tempfile.mkdtemp())
    mailbox = tmp / "mailbox.json"
    presence = tmp / "presence.json"
    mailbox.write_text(
        json.dumps(
            [
                {
                    "n": 7,
                    "from": "a",
                    "message": "one",
                    "at": "2026-09-21T10:00:00+00:00",
                    "resolved_at": "2026-09-21T10:05:00+00:00",
                    "resolve_note": "did it",
                },
                {"from": "b", "message": "two", "at": "2026-09-21T11:00:00+00:00"},
                {"from": "b", "message": "", "at": "2026-09-21T12:00:00+00:00"},
            ]
        ),
        encoding="utf-8",
    )
    presence.write_text(
        json.dumps(
            {
                "a": {
                    "session": "a",
                    "status": "live",
                    "last_seen": "2026-09-21T11:00:00+00:00",
                }
            }
        ),
        encoding="utf-8",
    )
    path = tmp / "migrated.db"
    result = store.migrate_json(mailbox, presence, tmp / "locks.json", path)
    assert result == {"messages": 2, "presence": 1, "locks": 0}, result
    entries = store.read_inbox(path=path)
    assert [e["n"] for e in entries] == [7, 8], "legacy numbers preserved in order"
    assert entries[0]["resolve_note"] == "did it"
    again = store.migrate_json(mailbox, presence, tmp / "locks.json", path)
    assert again == {"messages": 0, "presence": 1, "locks": 0}, again
    assert store.message_count(path) == 2


def test_request_flow():
    path = fresh_db()
    first = store.open_request("alice", "sync the client please", path)
    assert first["id"] == 1 and first["status"] == "open"
    second = store.open_request("bob", "review the server", path)
    assert second["id"] == 2
    assert [r["id"] for r in store.list_requests(path=path)] == [1, 2]
    assert store.close_request(1, "carol", "synced", path) == {"outcome": "ok"}
    assert store.close_request(1, "carol", "again", path) == {"outcome": "already"}
    assert store.close_request(99, "carol", "", path) == {"outcome": "missing"}
    assert [r["id"] for r in store.list_requests(path=path)] == [2]
    all_rows = store.list_requests(open_only=False, path=path)
    assert [r["status"] for r in all_rows] == ["done", "open"]
    assert all_rows[0]["closer"] == "carol"
    assert all_rows[0]["close_note"] == "synced"
    try:
        store.open_request("alice", "   ", path)
    except ValueError:
        pass
    else:
        raise AssertionError("empty task must raise")


def test_claim_instance():
    path = fresh_db()
    assert store.claim_instance("muse", path) == "muse-01"
    assert store.claim_instance("muse", path) == "muse-02"
    assert store.claim_instance("opencode", path) == "opencode-01"
    live = {e["session"] for e in store.list_presence(path=path)}
    assert {"muse-01", "muse-02", "opencode-01"} <= live
    # A stale holder's number is reusable.
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    with store.connect(path) as db:
        db.execute(
            "INSERT INTO presence(session, status, last_seen) VALUES (?, ?, ?)",
            ("codex-01", "gone", old),
        )
    assert store.claim_instance("codex", path) == "codex-01"
    for bad in ("", "has space", "semi;colon", "x" * 41):
        try:
            store.claim_instance(bad, path)
        except ValueError:
            pass
        else:
            raise AssertionError(f"bad family {bad!r} must raise")


if __name__ == "__main__":
    check("post and read order", test_post_and_read_order)
    check("post validation", test_post_validation)
    check("inbox filters", test_inbox_filters)
    check("inbox query grammar", test_inbox_query_grammar)
    check("resolve flow", test_resolve_flow)
    check("cap and archive", test_cap_and_archive)
    check("heartbeat and presence", test_heartbeat_and_presence)
    check("claim release locks", test_claim_release_locks)
    check("claim expiry", test_claim_expiry)
    check("claim lock race", test_claim_lock_race)
    check("claim grammar", test_claim_grammar)
    check("migrate json", test_migrate_json)
    check("request flow", test_request_flow)
    check("claim instance", test_claim_instance)
    print(f"\nAll {len(PASS)} store checks passed.")
