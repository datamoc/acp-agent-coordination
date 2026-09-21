"""Throwaway helper for the SQLite cutover. Deleted before finishing."""

import json
import re
import sqlite3
import sys

import store

JUNK = re.compile(r"^(msg-\d+|spam: \d+)$")


def is_junk(sender: str, body: str) -> bool:
    return sender.strip() in ("s", "spam") and bool(JUNK.match(body.strip()))


def fresh_import():
    store.init_db()
    with store.connect() as db:
        db.execute("DELETE FROM messages")
        db.execute("DELETE FROM presence")
        db.execute("DELETE FROM locks")
    result = store.migrate_json()
    with store.connect() as db:
        junk = db.execute(
            "SELECT id FROM messages WHERE sender IN ('s', 'spam')"
        ).fetchall()
        doomed = []
        for (mid,) in junk:
            row = db.execute(
                "SELECT sender, body FROM messages WHERE id = ?", (mid,)
            ).fetchone()
            if is_junk(row["sender"], row["body"]):
                doomed.append(mid)
        for mid in doomed:
            db.execute("DELETE FROM messages WHERE id = ?", (mid,))
    result["junk_removed"] = len(doomed)
    print(json.dumps(result))


def show():
    with store.connect() as db:
        print("messages:", db.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        print(
            "range:",
            db.execute("SELECT MIN(id), MAX(id) FROM messages").fetchone(),
        )
        print(
            "tail:",
            db.execute(
                "SELECT id, sender, substr(body,1,50), at FROM messages "
                "ORDER BY id DESC LIMIT 3"
            ).fetchall(),
        )
        print(
            "presence:",
            db.execute("SELECT session, status FROM presence").fetchall(),
        )
        print("locks:", db.execute("SELECT scope, owner FROM locks").fetchall())


def renumber():
    with store.connect() as db:
        db.execute("UPDATE messages SET id = 1 WHERE id = 84")
        db.execute("UPDATE messages SET id = 2 WHERE id = 85")
        db.execute("UPDATE messages SET id = 3 WHERE id = 86")
        db.execute("UPDATE sqlite_sequence SET seq = 80 WHERE name = 'messages'")
    entries = store.read_inbox()
    ns = [e["n"] for e in entries]
    print(len(entries), ns[:3], ns[-3:], entries[0]["from"], entries[-1]["from"])


if __name__ == "__main__":
    if sys.argv[1] == "fresh-import":
        fresh_import()
    elif sys.argv[1] == "show":
        show()
    elif sys.argv[1] == "renumber":
        renumber()
