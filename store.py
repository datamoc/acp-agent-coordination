"""SQLite coordination store (stdlib only) for the ACP prototype server.

One small database file (`coord.db`, WAL mode) backs the same agent API the
JSON implementation established (stable `#N` numbering, resolve flow,
locks with TTL, presence roster):

- `messages` - the shared mailbox. Inserts are transactional, so concurrent
  posts can no longer clobber each other the way whole-file rewrites could.
  Reads are indexed SQL (`#N`/`since`/`from`/limit) instead of full parses.
  The AUTOINCREMENT id *is* the stable message number: migration inserts
  the legacy rows in order, so old `#N` references keep pointing at the
  same message, and ids are never reused after pruning.
- `presence` - the live roster, upserted per heartbeat.
- `locks` - file/area ownership claims with expiry, so a crashed session
  cannot hold a claim forever.

The live mailbox is capped (`MAILBOX_CAP`); overflow spills to dated
`mailbox-archive-<date>.json` files next to the database (same layout the
JSON implementation used, so existing archives stay readable) before
deletion from the hot table.

Kept deliberately small: no ORM, no migrations framework (a
`schema_version` row plus `CREATE TABLE IF NOT EXISTS`), no FTS (`LIKE`
is plenty at this volume).

`path` arguments default to `None`, meaning the current `DB_PATH` global -
reassign `store.DB_PATH` to repoint everything (tests do exactly this).
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DIR = Path(__file__).parent
DB_PATH = DIR / "coord.db"

SCHEMA_VERSION = 1
MAILBOX_CAP = 500
MESSAGE_MAX_CHARS = 4000
SENDER_MAX_CHARS = 80
# A session counts as live if its last heartbeat is within this window.
# Generous on purpose: AI sessions can think for a long time between heartbeats.
PRESENCE_TTL_SECONDS = 1800
# Dead roster entries are pruned on each heartbeat after this long.
PRESENCE_PRUNE_DAYS = 7
# A file/area claim lapses after this long; re-claim to hold it longer.
LOCK_TTL_SECONDS = 7200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender TEXT NOT NULL,
    body TEXT NOT NULL,
    at TEXT NOT NULL,
    resolved_at TEXT,
    resolve_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_at ON messages(at);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender);
CREATE TABLE IF NOT EXISTS presence(
    session TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS locks(
    scope TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    note TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    until TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requester TEXT NOT NULL,
    task TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    closer TEXT,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    close_note TEXT
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_db(path: Path | None) -> Path:
    return DB_PATH if path is None else path


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Short-lived connection; use as a context manager (`with connect():`
    commits on clean exit, rolls back on error). `path=None` follows the
    current `DB_PATH` global."""
    db = sqlite3.connect(_resolve_db(path), timeout=30.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL;")
    db.execute("PRAGMA busy_timeout=30000;")
    return db


def init_db(path: Path | None = None) -> None:
    with connect(path) as db:
        db.executescript(_SCHEMA)
        db.execute(
            "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
            (SCHEMA_VERSION,),
        )


def _archive_path(day: str, path: Path | None = None) -> Path:
    return _resolve_db(path).parent / f"mailbox-archive-{day}.json"


def archive_files(path: Path | None = None) -> list[Path]:
    return sorted(_resolve_db(path).parent.glob("mailbox-archive-*.json"))


def archived_total(path: Path | None = None) -> int:
    total = 0
    for archive in archive_files(path):
        try:
            data = json.loads(archive.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, list):
            total += len(data)
    return total


def _archive_entries(
    db: sqlite3.Connection, pruned: list[sqlite3.Row], path: Path | None
) -> None:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    archive = _archive_path(day, path)
    try:
        archived = json.loads(archive.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        archived = []
    if not isinstance(archived, list):
        archived = []
    for row in pruned:
        entry: dict = {
            "n": row["id"],
            "from": row["sender"],
            "message": row["body"],
            "at": row["at"],
        }
        if row["resolved_at"]:
            entry["resolved_at"] = row["resolved_at"]
            entry["resolve_note"] = row["resolve_note"] or ""
        archived.append(entry)
    tmp = archive.with_suffix(archive.suffix + ".tmp")
    tmp.write_text(json.dumps(archived, indent=2), encoding="utf-8")
    tmp.replace(archive)


def _enforce_cap(db: sqlite3.Connection, path: Path | None) -> None:
    """Keep the newest MAILBOX_CAP rows live; spill the rest to the archive.
    Ids (the stable `#N` numbers) are never reused."""
    over = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] - MAILBOX_CAP
    if over <= 0:
        return
    pruned = db.execute(
        "SELECT id, sender, body, at, resolved_at, resolve_note "
        "FROM messages ORDER BY id LIMIT ?",
        (over,),
    ).fetchall()
    _archive_entries(db, pruned, path)
    db.execute("DELETE FROM messages WHERE id <= ?", (pruned[-1]["id"],))


def post_message(
    sender: str, body: str, path: Path | None = None
) -> dict:
    """Append one mailbox entry. Returns the stored entry (its sqlite id is
    the stable `#N`). Raises ValueError on empty/oversize input; callers
    turn that into an agent reply, not a traceback."""
    sender = (sender or "").strip()[:SENDER_MAX_CHARS] or "unknown"
    body = (body or "").strip()
    if not body:
        raise ValueError("empty message body - nothing posted")
    if len(body) > MESSAGE_MAX_CHARS:
        raise ValueError(
            f"message too long ({len(body)} > {MESSAGE_MAX_CHARS} chars)"
        )
    at = _now_iso()
    with connect(path) as db:
        cur = db.execute(
            "INSERT INTO messages(sender, body, at) VALUES (?, ?, ?)",
            (sender, body, at),
        )
        _enforce_cap(db, path)
        return {"n": cur.lastrowid, "from": sender, "message": body, "at": at}


def _row_to_entry(row: sqlite3.Row) -> dict:
    entry: dict = {
        "n": row["id"],
        "from": row["sender"],
        "message": row["body"],
        "at": row["at"],
    }
    if row["resolved_at"]:
        entry["resolved_at"] = row["resolved_at"]
        entry["resolve_note"] = row["resolve_note"] or ""
    return entry


def read_inbox(
    limit: int | None = None,
    since_n: int | None = None,
    since_t: str | None = None,
    sender: str | None = None,
    path: Path | None = None,
) -> list[dict]:
    """Newest-last entries matching the filters: `#N` floor, ISO timestamp
    floor (exclusive, like the JSON implementation), exact sender match."""
    query = (
        "SELECT id, sender, body, at, resolved_at, resolve_note FROM messages"
    )
    clauses: list[str] = []
    args: list = []
    if since_n is not None:
        clauses.append("id > ?")
        args.append(since_n)
    if since_t is not None:
        clauses.append("at > ?")
        args.append(since_t)
    if sender:
        clauses.append("sender = ?")
        args.append(sender)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    if limit is not None:
        query = f"SELECT * FROM ({query} ORDER BY id DESC LIMIT ?) ORDER BY id"
        args.append(limit)
    else:
        query += " ORDER BY id"
    with connect(path) as db:
        rows = db.execute(query, args).fetchall()
    return [_row_to_entry(r) for r in rows]


def parse_inbox_query(text: str) -> dict:
    """Parse the `inbox` agent input. Tokens combine: empty for everything,
    `"N"` for the last N, `"#N"` for messages since #N, `"since <iso>"` for
    messages after a timestamp, `"<session>"` (or `"from <session>"`) for
    one sender. A bare ISO timestamp works too. Unknown tokens are ignored
    (documented, not silent-by-accident: replies echo the parsed filter)."""
    result: dict = {"limit": None, "since_n": None, "since_t": None, "sender": None}
    tokens = (text or "").split()
    who_parts: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("#") and tok[1:].isdigit():
            result["since_n"] = int(tok[1:])
        elif tok.isdigit() and result["limit"] is None:
            result["limit"] = int(tok)
        elif tok.lower() == "since" and i + 1 < len(tokens):
            parsed = _parse_time(tokens[i + 1])
            if parsed is not None:
                result["since_t"] = tokens[i + 1]
                i += 1
            else:
                who_parts.append(tok)
        elif _parse_time(tok) is not None and result["since_t"] is None:
            result["since_t"] = tok
        elif tok.lower() == "from":
            who_parts.extend(tokens[i + 1 :])
            break
        else:
            who_parts.append(tok)
        i += 1
    if who_parts:
        result["sender"] = " ".join(who_parts)
    return result


def resolve_message(number: int, note: str, path: Path | None = None) -> dict:
    """Stamp a live entry resolved. Returns {"outcome": "ok" | "already" |
    "missing"} for the agent reply."""
    with connect(path) as db:
        row = db.execute(
            "SELECT id, resolved_at FROM messages WHERE id = ?", (number,)
        ).fetchone()
        if row is None:
            return {"outcome": "missing"}
        if row["resolved_at"]:
            return {"outcome": "already"}
        db.execute(
            "UPDATE messages SET resolved_at = ?, resolve_note = ? WHERE id = ?",
            (_now_iso(), (note or "").strip(), number),
        )
        return {"outcome": "ok"}


def heartbeat(session: str, status: str, path: Path | None = None) -> dict:
    """Upsert one roster entry and prune long-dead ones. Returns the entry."""
    session = (session or "").strip()[:SENDER_MAX_CHARS] or "unknown"
    status = (status or "").strip()[:400] or "live"
    now = _now_iso()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=PRESENCE_PRUNE_DAYS)).isoformat(
        timespec="seconds"
    )
    with connect(path) as db:
        db.execute(
            "INSERT INTO presence(session, status, last_seen) VALUES (?, ?, ?) "
            "ON CONFLICT(session) DO UPDATE SET status=excluded.status, "
            "last_seen=excluded.last_seen",
            (session, status, now),
        )
        db.execute("DELETE FROM presence WHERE last_seen < ?", (cutoff,))
    return {"session": session, "status": status, "last_seen": now}


def _parse_time(text: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def list_presence(
    window_seconds: int | None = None, path: Path | None = None
) -> list[dict]:
    """Roster entries live within the window (default PRESENCE_TTL_SECONDS);
    `window_seconds=None` lists every known session including stale ones."""
    with connect(path) as db:
        rows = db.execute(
            "SELECT session, status, last_seen FROM presence ORDER BY last_seen"
        ).fetchall()
    entries = [
        {"session": r["session"], "status": r["status"], "last_seen": r["last_seen"]}
        for r in rows
    ]
    if window_seconds is None:
        return entries
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    return [
        e
        for e in entries
        if (seen := _parse_time(e["last_seen"])) is not None and seen >= cutoff
    ]


def _valid_lock(row: sqlite3.Row | None) -> dict | None:
    """A lock row counts only while unexpired; expired rows are lazily removed."""
    if row is None:
        return None
    until = _parse_time(row["until"])
    if until is None or until <= datetime.now(timezone.utc):
        return None
    return {
        "scope": row["scope"],
        "owner": row["owner"],
        "note": row["note"],
        "claimed_at": row["claimed_at"],
        "until": row["until"],
    }


def claim_lock(
    owner: str,
    scope: str,
    note: str = "",
    ttl_seconds: int = LOCK_TTL_SECONDS,
    path: Path | None = None,
) -> dict:
    """Claim exclusive ownership of a file/area scope. Returns
    {"ok": True, lock} on success (re-claiming your own scope refreshes it),
    {"ok": False, "held_by": lock} when someone else's live claim blocks it.
    Raises ValueError on empty owner/scope."""
    owner = (owner or "").strip()[:SENDER_MAX_CHARS]
    scope = (scope or "").strip()[:400]
    if not owner:
        raise ValueError("empty owner")
    if not scope:
        raise ValueError("empty scope")
    note = (note or "").strip()[:400]
    now = datetime.now(timezone.utc)
    until = (now + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
    with connect(path) as db:
        row = db.execute(
            "SELECT scope, owner, note, claimed_at, until FROM locks WHERE scope = ?",
            (scope,),
        ).fetchone()
        held = _valid_lock(row)
        if held is not None and held["owner"] != owner:
            return {"ok": False, "held_by": held}
        if held is not None:
            db.execute(
                "UPDATE locks SET note = ?, claimed_at = ?, until = ? "
                "WHERE scope = ? AND owner = ?",
                (note, now.isoformat(timespec="seconds"), until, scope, owner),
            )
        else:
            db.execute("DELETE FROM locks WHERE scope = ?", (scope,))
            db.execute(
                "INSERT INTO locks(scope, owner, note, claimed_at, until) "
                "VALUES (?, ?, ?, ?, ?)",
                (scope, owner, note, now.isoformat(timespec="seconds"), until),
            )
        return {
            "ok": True,
            "lock": {"scope": scope, "owner": owner, "note": note, "until": until},
        }


def release_lock(owner: str, scope: str, path: Path | None = None) -> dict:
    """Release a claim. Returns {"ok": True} or {"ok": False, "held_by"} -
    only the holder may release a live claim; anyone may release an
    expired (or absent) one."""
    owner = (owner or "").strip()
    scope = (scope or "").strip()
    with connect(path) as db:
        row = db.execute(
            "SELECT scope, owner, note, claimed_at, until FROM locks WHERE scope = ?",
            (scope,),
        ).fetchone()
        held = _valid_lock(row)
        if held is None:
            db.execute("DELETE FROM locks WHERE scope = ?", (scope,))
            return {"ok": True, "was": "absent"}
        if held["owner"] != owner:
            return {"ok": False, "held_by": held}
        db.execute("DELETE FROM locks WHERE scope = ? AND owner = ?", (scope, owner))
        return {"ok": True, "was": "released"}


def list_locks(include_expired: bool = False, path: Path | None = None) -> list[dict]:
    """Active claims (oldest first); `include_expired=True` also shows lapsed
    rows still on record, marked."""
    with connect(path) as db:
        rows = db.execute(
            "SELECT scope, owner, note, claimed_at, until FROM locks ORDER BY claimed_at"
        ).fetchall()
    out = []
    for row in rows:
        lock = _valid_lock(row)
        if lock is not None:
            out.append(lock)
        elif include_expired:
            out.append(
                {
                    "scope": row["scope"],
                    "owner": row["owner"],
                    "note": row["note"] or "",
                    "claimed_at": row["claimed_at"],
                    "until": row["until"],
                    "expired": True,
                }
            )
    return out


def parse_claim(text: str, known_scopes: tuple = ()) -> dict:
    """Split "<owner>: <scope>[ | <note>]" (or the legacy second-colon
    form) into owner/scope/note. Resolution order: pipe form first, then
    an exact match against live scopes (so a claimed Windows path
    round-trips without its pipe), then the legacy second-colon split."""
    if ":" in text:
        owner, _, rest = text.partition(":")
    else:
        owner, rest = text.strip(), ""
    rest = rest.strip()
    if "|" in rest:
        scope, _, note = rest.partition("|")
    elif rest in known_scopes:
        scope, note = rest, ""
    else:
        scope, _, note = rest.partition(":")
    return {"owner": owner.strip(), "scope": scope.strip(), "note": note.strip()}


def message_count(path: Path | None = None) -> int:
    with connect(path) as db:
        return db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


INSTANCE_MAX_N = 99
INSTANCE_LEASE_NOTE = "reserved via whoami"


def claim_instance(family: str, path: Path | None = None) -> str:
    """Hand out the smallest free "<family>-NN" session name and heartbeat
    it immediately, so a concurrent claimant sees it as taken. A number
    counts as taken while its holder is live (inside PRESENCE_TTL_SECONDS);
    holding your number means heartbeating inside that window. Raises
    ValueError on a bad family name, RuntimeError when all 99 are taken.

    Serialized with BEGIN IMMEDIATE: two concurrent claimants cannot draw
    the same number - the second blocks, then sees the first's row."""
    import re

    family = (family or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", family):
        raise ValueError(
            "family must be 1-40 letters/digits/_/- characters"
        )
    name_pattern = re.compile(re.escape(family) + r"-(\d{1,3})$")
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=PRESENCE_TTL_SECONDS)
    )
    db = connect(path)
    try:
        db.isolation_level = None
        db.execute("BEGIN IMMEDIATE")
        taken = set()
        for (session, last_seen) in db.execute(
            "SELECT session, last_seen FROM presence"
        ).fetchall():
            match = name_pattern.fullmatch(session or "")
            if not match:
                continue
            seen = _parse_time(last_seen)
            if seen is not None and seen >= cutoff:
                taken.add(int(match.group(1)))
        free = next(
            (n for n in range(1, INSTANCE_MAX_N + 1) if n not in taken), None
        )
        if free is None:
            raise RuntimeError(f"no free numbers for family '{family}'")
        name = f"{family}-{free:02d}"
        now = _now_iso()
        db.execute(
            "INSERT INTO presence(session, status, last_seen) VALUES (?, ?, ?) "
            "ON CONFLICT(session) DO UPDATE SET status=excluded.status, "
            "last_seen=excluded.last_seen",
            (name, INSTANCE_LEASE_NOTE, now),
        )
        db.execute("COMMIT")
        return name
    except Exception:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        db.close()


def open_request(requester: str, task: str, path: Path | None = None) -> dict:
    """Open a task request for another agent. Request ids (`#R`) are a
    separate sequence from mailbox `#N` numbers. Raises ValueError on
    empty requester/task."""
    requester = (requester or "").strip()[:SENDER_MAX_CHARS] or "unknown"
    task = (task or "").strip()
    if not task:
        raise ValueError("empty task - request as '<session>: <task>'")
    if len(task) > MESSAGE_MAX_CHARS:
        raise ValueError(
            f"task too long ({len(task)} > {MESSAGE_MAX_CHARS} chars)"
        )
    with connect(path) as db:
        cur = db.execute(
            "INSERT INTO requests(requester, task, status, created_at) "
            "VALUES (?, ?, 'open', ?)",
            (requester, task, _now_iso()),
        )
        return {
            "id": cur.lastrowid,
            "requester": requester,
            "task": task,
            "status": "open",
        }


def list_requests(open_only: bool = True, path: Path | None = None) -> list[dict]:
    """Open requests oldest-first (a work queue); `open_only=False`
    includes closed ones."""
    query = (
        "SELECT id, requester, task, status, closer, created_at, "
        "closed_at, close_note FROM requests"
    )
    if open_only:
        query += " WHERE status = 'open'"
    query += " ORDER BY id"
    with connect(path) as db:
        rows = db.execute(query).fetchall()
    return [
        {
            "id": r["id"],
            "requester": r["requester"],
            "task": r["task"],
            "status": r["status"],
            "closer": r["closer"],
            "created_at": r["created_at"],
            "closed_at": r["closed_at"],
            "close_note": r["close_note"] or "",
        }
        for r in rows
    ]


def close_request(
    number: int, closer: str, note: str, path: Path | None = None
) -> dict:
    """Close a request. Returns {"outcome": "ok" | "already" | "missing"}."""
    with connect(path) as db:
        row = db.execute(
            "SELECT id, status FROM requests WHERE id = ?", (number,)
        ).fetchone()
        if row is None:
            return {"outcome": "missing"}
        if row["status"] != "open":
            return {"outcome": "already"}
        db.execute(
            "UPDATE requests SET status = 'done', closer = ?, closed_at = ?, "
            "close_note = ? WHERE id = ?",
            (
                (closer or "").strip()[:SENDER_MAX_CHARS] or "unknown",
                _now_iso(),
                (note or "").strip()[:400],
                number,
            ),
        )
        return {"outcome": "ok"}


def migrate_json(
    mailbox_path: Path | None = None,
    presence_path: Path | None = None,
    locks_path: Path | None = None,
    path: Path | None = None,
) -> dict:
    """One-time import of the legacy flat files (plus any archive files).
    Idempotent enough to rerun: mailbox rows are matched on (sender, body,
    at), presence/locks upsert by key. Explicit legacy ids are preserved so
    old `#N` references keep pointing at the same message."""
    base = DIR if mailbox_path is None else mailbox_path.parent
    mailbox_path = mailbox_path or DIR / "mailbox.json"
    presence_path = presence_path or DIR / "presence.json"
    locks_path = locks_path or DIR / "locks.json"
    init_db(path)
    imported_messages = 0
    imported_presence = 0
    imported_locks = 0

    def import_entries(entries: list) -> None:
        nonlocal imported_messages
        with connect(path) as db:
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                sender = str(entry.get("from", "unknown"))
                body = str(entry.get("message", ""))
                at = str(entry.get("at", _now_iso()))
                if not body:
                    continue
                exists = db.execute(
                    "SELECT 1 FROM messages WHERE sender = ? AND body = ? AND at = ?",
                    (sender, body, at),
                ).fetchone()
                if exists:
                    continue
                number = entry.get("n")
                payload = (
                    sender,
                    body,
                    at,
                    entry.get("resolved_at"),
                    entry.get("resolve_note", ""),
                )
                if isinstance(number, int) and number is not True:
                    try:
                        db.execute(
                            "INSERT INTO messages(id, sender, body, at, "
                            "resolved_at, resolve_note) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (number, *payload),
                        )
                    except sqlite3.IntegrityError:
                        # A stray row already holds that id (aborted run,
                        # test data) - append at the tail instead of dying.
                        db.execute(
                            "INSERT INTO messages(sender, body, at, "
                            "resolved_at, resolve_note) "
                            "VALUES (?, ?, ?, ?, ?)",
                            payload,
                        )
                else:
                    db.execute(
                        "INSERT INTO messages(sender, body, at, resolved_at, "
                        "resolve_note) VALUES (?, ?, ?, ?, ?)",
                        payload,
                    )
                imported_messages += 1

    for archive in sorted(base.glob("mailbox-archive-*.json")):
        try:
            data = json.loads(archive.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, list):
            import_entries(data)
    if mailbox_path.exists():
        try:
            entries = json.loads(mailbox_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            entries = []
        if isinstance(entries, list):
            import_entries(entries)
    if presence_path.exists():
        try:
            sessions = json.loads(presence_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            sessions = {}
        if isinstance(sessions, dict):
            with connect(path) as db:
                for key, value in sessions.items():
                    if not isinstance(value, dict):
                        continue
                    session = str(value.get("session", key))
                    status = str(value.get("status", "live"))
                    last_seen = str(value.get("last_seen", _now_iso()))
                    db.execute(
                        "INSERT INTO presence(session, status, last_seen) "
                        "VALUES (?, ?, ?) ON CONFLICT(session) DO UPDATE SET "
                        "status=excluded.status, "
                        "last_seen=MAX(presence.last_seen, excluded.last_seen)",
                        (session, status, last_seen),
                    )
                    imported_presence += 1
    if locks_path.exists():
        try:
            locks = json.loads(locks_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            locks = {}
        if isinstance(locks, dict):
            with connect(path) as db:
                for scope, value in locks.items():
                    if not isinstance(value, dict):
                        continue
                    db.execute(
                        "INSERT INTO locks(scope, owner, note, claimed_at, until) "
                        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(scope) DO UPDATE SET "
                        "owner=excluded.owner, note=excluded.note, "
                        "claimed_at=excluded.claimed_at, until=excluded.until",
                        (
                            str(scope),
                            str(value.get("owner", "unknown")),
                            str(value.get("note", "")),
                            str(value.get("claimed_at", _now_iso())),
                            str(
                                value.get(
                                    "until",
                                    (
                                        datetime.now(timezone.utc)
                                        + timedelta(seconds=LOCK_TTL_SECONDS)
                                    ).isoformat(timespec="seconds"),
                                )
                            ),
                        ),
                    )
                    imported_locks += 1
    return {
        "messages": imported_messages,
        "presence": imported_presence,
        "locks": imported_locks,
    }
