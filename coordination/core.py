"""coord core: schema, constants, helpers and the database plumbing every topic module uses.

One SQLite file, every mutation under BEGIN IMMEDIATE.

Identity is the session UUID, never the display name: a recycled name gets a
new session_id and a higher generation, so it cannot touch the old claims.
"""

import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


SESSION_TTL = 1800
CLAIM_TTL = 7200
WAKE_KEEPALIVE = 1500        # poll again within 25 min: the session dies after 30
CLAIM_RENEW_MARGIN = 600     # wake 10 min before a claim expires
MSG_RECOMMENDED = 300
MSG_MAX = 10000
INBOX_DEFAULT = 20

MESSAGE_KINDS = ("info", "question", "advice", "proposal", "decision", "review", "warning", "done")
ROLES = ("advisor", "reviewer", "coeditor", "delegate")
STANCES = ("support", "support-with-reservation", "object", "abstain", "need-more-info")
SUPPORTING = ("support", "support-with-reservation")   # both count for consensus; reservations are listed
CONSENSUS_RULES = ("unanimous", "majority", "no-objection")
DEFAULT_QUORUM = 2   # consensus always involves someone besides the decider
DOC_KINDS = ("note", "diagnosis", "plan", "proposal", "decision", "review", "adr")
MEMORY_KINDS = ("strategy", "overview", "convention", "architecture", "decision", "pitfall", "glossary")
TASK_STATUSES = ("open", "offered", "accepted", "done", "cancelled")   # offered: assigned, not yet accepted
ROLES_BY_CONSENT = ("coeditor", "delegate")   # carry write duties: the grantee must accept them
ROUTINE_STATUSES = ("active", "paused", "retired")
ROUTINE_OUTCOMES = ("ok", "issues", "failed")   # issues/failed also post a warning

# What this server can do - published in whoami, `coord server` (server_info) and the Agent Card.
FEATURES = ("sessions", "messages", "claims", "fences", "roles", "discussions", "consensus", "documents",
            "document-patches", "tasks", "a2a", "push-notifications", "memory", "strategy", "routines",
            "server-info", "wake-hints", "delegation-scopes", "reservations", "superseding", "event-stream", "ui")
# What each release brought agents: announced to every project when the server starts on a newer version.
NEWS = {
    "0.4.0": "one certificate per agent CLI; plugins for Muse, Gemini, Qwen, opencode, Kilo and Crush",
    "0.5.0": "strategy (memory kind, first in context) and routines (recurring work: coord routines)",
    "0.6.0": "the server publishes its version and features (whoami, coord server) and announces upgrades",
    "0.6.1": "Deep Code support (tools/agent_plugins.py install deepcode, with its own certificate)",
    "0.7.0": "wake hints in poll/context (when to look again); support-with-reservation; objections need a "
             "reason; propose --supersedes; delegate a sub-scope; coord-db export/prune/vacuum",
    "0.8.0": "coord-server --ui: a window for humans to follow and join the work; live events over SSE "
             "(GET /events/stream, coord events --follow)",
}
SERVER_NAME = "coord-server"   # sender of the server's own messages (upgrade notices)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS repos(
    project_id TEXT PRIMARY KEY, provider TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
    session_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, family TEXT NOT NULL,
    generation INTEGER NOT NULL, project_id TEXT NOT NULL, principal TEXT,
    status TEXT NOT NULL DEFAULT '', started_at REAL NOT NULL,
    heartbeat_at REAL NOT NULL, ended_at REAL, cursor INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_sessions_name ON sessions(display_name);
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
    from_session_id TEXT NOT NULL, from_name TEXT NOT NULL,
    to_session_id TEXT, to_name TEXT, thread_id INTEGER, reply_to INTEGER,
    kind TEXT NOT NULL, body TEXT NOT NULL, claim_id INTEGER,
    created_at REAL NOT NULL, resolved_at REAL, resolved_by TEXT, resolution TEXT);
CREATE INDEX IF NOT EXISTS ix_messages_project ON messages(project_id, id);
CREATE INDEX IF NOT EXISTS ix_messages_thread ON messages(thread_id);
CREATE TABLE IF NOT EXISTS claims(
    claim_id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
    owner_session_id TEXT NOT NULL, owner_name TEXT NOT NULL,
    scope_type TEXT NOT NULL CHECK(scope_type IN ('exact','tree')), scope TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '', claimed_at REAL NOT NULL, expires_at REAL NOT NULL,
    fence INTEGER NOT NULL, release_on_commit INTEGER NOT NULL DEFAULT 0,
    released_at REAL, released_by TEXT);
CREATE INDEX IF NOT EXISTS ix_claims_active ON claims(project_id, released_at);
CREATE TABLE IF NOT EXISTS claim_roles(
    claim_id INTEGER NOT NULL, session_id TEXT NOT NULL, role TEXT NOT NULL,
    granted_by TEXT NOT NULL, granted_at REAL NOT NULL, accepted INTEGER NOT NULL DEFAULT 1,
    scope_type TEXT, scope TEXT, PRIMARY KEY(claim_id, session_id, role));
CREATE TABLE IF NOT EXISTS discussions(
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
    created_by TEXT NOT NULL, created_by_name TEXT NOT NULL, topic TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', claim_id INTEGER, message_id INTEGER,
    decision TEXT, consensus INTEGER, decided_by TEXT, decided_at REAL,
    decision_document_id INTEGER, created_at REAL NOT NULL,
    rule TEXT NOT NULL DEFAULT 'unanimous', quorum INTEGER NOT NULL DEFAULT 2,
    decision_reason TEXT, consensus_detail TEXT, deadline REAL);
CREATE TABLE IF NOT EXISTS discussion_participants(
    discussion_id INTEGER NOT NULL, session_id TEXT NOT NULL, name TEXT NOT NULL,
    PRIMARY KEY (discussion_id, session_id));
CREATE TABLE IF NOT EXISTS proposals(
    id INTEGER PRIMARY KEY AUTOINCREMENT, discussion_id INTEGER NOT NULL,
    author_session_id TEXT NOT NULL, author_name TEXT NOT NULL, body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', created_at REAL NOT NULL, supersedes_id INTEGER);
CREATE TABLE IF NOT EXISTS reactions(
    proposal_id INTEGER NOT NULL, author_session_id TEXT NOT NULL,
    author_name TEXT NOT NULL, stance TEXT NOT NULL, comment TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL, PRIMARY KEY(proposal_id, author_session_id));
CREATE TABLE IF NOT EXISTS documents(
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, title TEXT NOT NULL,
    kind TEXT NOT NULL, created_by TEXT NOT NULL, revision INTEGER NOT NULL,
    content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
    created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS document_revisions(
    document_id INTEGER NOT NULL, revision INTEGER NOT NULL,
    author_session_id TEXT NOT NULL, author_name TEXT NOT NULL,
    content TEXT NOT NULL, message TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
    PRIMARY KEY(document_id, revision));
CREATE TABLE IF NOT EXISTS events(
    event_id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
    kind TEXT NOT NULL, actor_session_id TEXT, entity_type TEXT, entity_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS project_memory(
    memory_id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
    kind TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL, updated_by TEXT NOT NULL,
    revision INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memory_revisions(
    memory_id INTEGER NOT NULL, revision INTEGER NOT NULL, content TEXT NOT NULL,
    author TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(memory_id, revision));
CREATE TABLE IF NOT EXISTS tasks(
    task_id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
    created_by TEXT NOT NULL, assigned_to TEXT, assigned_name TEXT,
    title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open', priority INTEGER NOT NULL DEFAULT 0,
    related_claim_id INTEGER, discussion_id INTEGER, category TEXT, note TEXT,
    created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS task_push(
    config_id TEXT PRIMARY KEY, task_id INTEGER NOT NULL, url TEXT NOT NULL, token TEXT,
    auth_scheme TEXT, auth_credentials TEXT, principal TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS agent_profiles(
    session_id TEXT PRIMARY KEY, provider TEXT, model_id TEXT, model_family TEXT,
    category TEXT, reasoning_level TEXT, capabilities_json TEXT NOT NULL DEFAULT '[]',
    updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS routines(
    routine_id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, title TEXT NOT NULL,
    instructions TEXT NOT NULL DEFAULT '', every REAL, on_commit INTEGER NOT NULL DEFAULT 0,
    paths TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'active', created_by TEXT NOT NULL,
    pending INTEGER NOT NULL DEFAULT 1, pending_reason TEXT,
    runner_session_id TEXT, runner_name TEXT, run_expires REAL,
    last_run_at REAL, last_run_by TEXT, last_outcome TEXT, last_result TEXT,
    created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS routine_runs(
    routine_id INTEGER NOT NULL, run INTEGER NOT NULL, session_id TEXT NOT NULL, name TEXT NOT NULL,
    trigger TEXT, started_at REAL NOT NULL, finished_at REAL, outcome TEXT, result TEXT,
    PRIMARY KEY(routine_id, run));
CREATE TABLE IF NOT EXISTS idempotency(
    client_id TEXT PRIMARY KEY, op TEXT NOT NULL, result_json TEXT NOT NULL,
    created_at REAL NOT NULL);
"""

PREFIX = {"claim": "C", "discussion": "D", "proposal": "P", "document": "DOC",
          "task": "T", "memory": "M", "message": "#", "routine": "R"}


class CoordError(Exception):
    def __init__(self, code: str, message: str, data: dict | None = None):
        super().__init__(message)
        self.code = code
        self.data = data or {}


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def server_version() -> str:
    try:
        from importlib.metadata import version
        return version("coord")
    except Exception:
        return "0"


def version_key(v: str) -> tuple[int, ...]:
    """"0.10.1" -> (0, 10, 1); anything unparsable sorts first."""
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return ()


def parse_when(value: str, now: float) -> float:
    """A deadline: "90m", "48h", "3d", "30s" from now, or an ISO 8601 date/time (UTC if no zone)."""
    v = str(value).strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhd])", v)
    if m:
        return now + float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise CoordError("bad_deadline", f"deadline {v!r}: use 90m, 48h, 3d or an ISO date like 2026-10-01T12:00Z")
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()


def parse_id(kind: str, value) -> int:
    s = str(value).strip().upper()
    p = PREFIX[kind]
    if s.startswith(p):
        s = s[len(p):]
    if not s.isdigit():
        raise CoordError("bad_id", f"expected a {kind} id like {p}12, got {value!r}")
    return int(s)


class CoordBase:
    def __init__(self, path=None, clock=time.time, case_insensitive: bool | None = None):
        self.path = Path(path or os.environ.get("COORD_DB") or Path.cwd() / "coord2.db")
        self.clock = clock
        self.case_insensitive = case_insensitive
        with self._connect() as db:
            db.executescript(SCHEMA)
            db.execute("INSERT OR IGNORE INTO meta VALUES('schema_version','2')")
            self._migrate(db)
            db.execute("INSERT OR IGNORE INTO meta VALUES('fence','0')")

    # --- plumbing -------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @contextmanager
    def _tx(self):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    @contextmanager
    def _read(self):
        db = self._connect()
        try:
            db.execute("BEGIN")
            yield db
            db.execute("COMMIT")
        finally:
            db.close()

    def _mutate(self, op: str, client_id: str | None, fn):
        with self._tx() as db:
            if client_id:
                row = db.execute(
                    "SELECT op, result_json FROM idempotency WHERE client_id=?", (client_id,)
                ).fetchone()
                if row:
                    if row["op"] != op:
                        raise CoordError("idempotency_conflict",
                                         f"client_id {client_id} already used for {row['op']}")
                    result = json.loads(row["result_json"])
                    result["replayed"] = True
                    return result
            result = fn(db)
            if client_id:
                db.execute("INSERT INTO idempotency VALUES(?,?,?,?)",
                           (client_id, op, json.dumps(result), self.clock()))
            return result

    def _event(self, db, project, event_kind, actor, etype=None, eid=None, **payload):
        db.execute(
            "INSERT INTO events(project_id, kind, actor_session_id, entity_type, entity_id,"
            " payload_json, created_at) VALUES(?,?,?,?,?,?,?)",
            (project, event_kind, actor, etype, None if eid is None else str(eid),
             json.dumps(payload), self.clock()),
        )

    def _next_fence(self, db) -> int:
        db.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key='fence'")
        return int(db.execute("SELECT value FROM meta WHERE key='fence'").fetchone()[0])

    def _live(self, row) -> bool:
        return row["ended_at"] is None and row["heartbeat_at"] >= self.clock() - SESSION_TTL

    def _session(self, db, session_id) -> sqlite3.Row:
        if not session_id:
            raise CoordError("no_session", "no session: run `coord whoami <family>` first")
        row = db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise CoordError("unknown_session", f"unknown session {session_id}")
        if not self._live(row):
            raise CoordError("dead_session",
                             f"session {row['display_name']} (gen {row['generation']}) has "
                             "ended or expired; run `coord whoami` again")
        return row

    # Columns added after a table first shipped: (table, column, DDL). Existing databases get them
    # on open; CREATE TABLE above already has them for new ones.
    _MIGRATIONS = (
        ("discussions", "rule", "TEXT NOT NULL DEFAULT 'unanimous'"),
        ("discussions", "quorum", "INTEGER NOT NULL DEFAULT 2"),
        ("discussions", "decision_reason", "TEXT"),
        ("discussions", "consensus_detail", "TEXT"),
        ("discussions", "deadline", "REAL"),
        ("claim_roles", "accepted", "INTEGER NOT NULL DEFAULT 1"),   # grants made before stay in effect
        ("claim_roles", "scope_type", "TEXT"),                       # a delegate's sub-scope (NULL: the whole claim)
        ("claim_roles", "scope", "TEXT"),
        ("proposals", "supersedes_id", "INTEGER"),
    )

    @classmethod
    def _migrate(cls, db) -> None:
        # `requests` (planned in v2, replaced by tasks) was never used: drop it - only if empty
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='requests'").fetchone() \
                and db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0:
            db.execute("DROP TABLE requests")
        for table, column, ddl in cls._MIGRATIONS:
            have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _notify(self, db, sender, to_session_id: str | None, body: str, kind: str = "info",
                claim_id: int | None = None) -> int | None:
        """A direct message written in the caller's transaction (invitations, offers, decisions).
        `sender` is a sessions row; a recipient that is gone (None / unknown) is skipped."""
        to = db.execute("SELECT * FROM sessions WHERE session_id=?", (to_session_id,)).fetchone() if to_session_id else None
        if to is None or to["session_id"] == sender["session_id"]:
            return None
        return db.execute(
            "INSERT INTO messages(project_id, from_session_id, from_name, to_session_id, to_name, kind, body,"
            " claim_id, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (sender["project_id"], sender["session_id"], sender["display_name"], to["session_id"],
             to["display_name"], kind, body, claim_id, self.clock())).lastrowid

    def _live_by_name(self, db, name: str | None):
        """The live session currently holding a display name, or None."""
        if not name:
            return None
        return db.execute("SELECT * FROM sessions WHERE display_name=? AND ended_at IS NULL AND heartbeat_at>=?"
                          " ORDER BY generation DESC LIMIT 1", (name, self.clock() - SESSION_TTL)).fetchone()

    def _resolve_name(self, db, name: str, project: str) -> sqlite3.Row:
        cutoff = self.clock() - SESSION_TTL
        row = db.execute(
            "SELECT * FROM sessions WHERE display_name=? AND ended_at IS NULL AND heartbeat_at>=?"
            " ORDER BY generation DESC LIMIT 1", (name, cutoff)).fetchone()
        if row is None:
            raise CoordError("unknown_recipient", f"no live session named {name}")
        return row

    def _reap(self, db) -> None:
        """End sessions past their TTL and release what they held."""
        now = self.clock()
        dead = [r[0] for r in db.execute(
            "SELECT session_id FROM sessions WHERE ended_at IS NULL AND heartbeat_at<?", (now - SESSION_TTL,))]
        for sid in dead:
            db.execute("UPDATE sessions SET ended_at=? WHERE session_id=?", (now, sid))
        db.execute("UPDATE claims SET released_at=?, released_by='reaper' WHERE released_at IS NULL AND"
                   " owner_session_id IN (SELECT session_id FROM sessions WHERE ended_at IS NOT NULL)", (now,))

    _LIVE_OWNER = (" AND owner_session_id IN (SELECT session_id FROM sessions WHERE ended_at IS NULL"
                   " AND heartbeat_at>=?)")
