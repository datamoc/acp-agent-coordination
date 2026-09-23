"""Coordination service v2: one SQLite file, every mutation under BEGIN IMMEDIATE.

Identity is the session UUID, never the display name: a recycled name gets a
new session_id and a higher generation, so it cannot touch the old claims.
"""

import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import scopes

SESSION_TTL = 1800
CLAIM_TTL = 7200
MSG_RECOMMENDED = 300
MSG_MAX = 10000
INBOX_DEFAULT = 20

MESSAGE_KINDS = ("info", "question", "advice", "proposal", "decision", "review", "warning", "done")
ROLES = ("advisor", "reviewer", "coeditor", "delegate")
STANCES = ("support", "object", "abstain", "need-more-info")
DOC_KINDS = ("note", "diagnosis", "plan", "proposal", "decision", "review", "adr")
MEMORY_KINDS = ("overview", "convention", "architecture", "decision", "pitfall", "glossary")
TASK_STATUSES = ("open", "accepted", "done", "cancelled")

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
    granted_by TEXT NOT NULL, granted_at REAL NOT NULL,
    PRIMARY KEY(claim_id, session_id, role));
CREATE TABLE IF NOT EXISTS requests(
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
    from_session_id TEXT NOT NULL, assigned_to TEXT, body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS discussions(
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
    created_by TEXT NOT NULL, created_by_name TEXT NOT NULL, topic TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', claim_id INTEGER, message_id INTEGER,
    decision TEXT, consensus INTEGER, decided_by TEXT, decided_at REAL,
    decision_document_id INTEGER, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS proposals(
    id INTEGER PRIMARY KEY AUTOINCREMENT, discussion_id INTEGER NOT NULL,
    author_session_id TEXT NOT NULL, author_name TEXT NOT NULL, body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', created_at REAL NOT NULL);
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
CREATE TABLE IF NOT EXISTS agent_profiles(
    session_id TEXT PRIMARY KEY, provider TEXT, model_id TEXT, model_family TEXT,
    category TEXT, reasoning_level TEXT, capabilities_json TEXT NOT NULL DEFAULT '[]',
    updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS idempotency(
    client_id TEXT PRIMARY KEY, op TEXT NOT NULL, result_json TEXT NOT NULL,
    created_at REAL NOT NULL);
"""

PREFIX = {"claim": "C", "discussion": "D", "proposal": "P", "document": "DOC",
          "task": "T", "memory": "M", "message": "#"}


class CoordError(Exception):
    def __init__(self, code: str, message: str, data: dict | None = None):
        super().__init__(message)
        self.code = code
        self.data = data or {}


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def parse_id(kind: str, value) -> int:
    s = str(value).strip().upper()
    p = PREFIX[kind]
    if s.startswith(p):
        s = s[len(p):]
    if not s.isdigit():
        raise CoordError("bad_id", f"expected a {kind} id like {p}12, got {value!r}")
    return int(s)


class Coord:
    def __init__(self, path=None, clock=time.time, case_insensitive: bool | None = None):
        self.path = Path(path or os.environ.get("COORD_DB") or Path.cwd() / "coord2.db")
        self.clock = clock
        self.case_insensitive = case_insensitive
        with self._connect() as db:
            db.executescript(SCHEMA)
            db.execute("INSERT OR IGNORE INTO meta VALUES('schema_version','2')")
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

    def _resolve_name(self, db, name: str, project: str) -> sqlite3.Row:
        cutoff = self.clock() - SESSION_TTL
        row = db.execute(
            "SELECT * FROM sessions WHERE display_name=? AND ended_at IS NULL AND heartbeat_at>=?"
            " ORDER BY generation DESC LIMIT 1", (name, cutoff)).fetchone()
        if row is None:
            raise CoordError("unknown_recipient", f"no live session named {name}")
        return row

    def _norm(self, raw: str, tree: bool | None) -> tuple[str, str]:
        try:
            path, is_dir = scopes.normalize(raw, self.case_insensitive)
        except scopes.ScopeError as e:
            raise CoordError("bad_scope", str(e))
        return ("tree" if (tree or is_dir) else "exact"), path

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

    def _active_claims(self, db, project):
        now = self.clock()
        return db.execute(
            "SELECT * FROM claims WHERE project_id=? AND released_at IS NULL AND expires_at>?" + self._LIVE_OWNER,
            (project, now, now - SESSION_TTL)).fetchall()

    def _roles(self, db, claim_id: int, session_id: str) -> set[str]:
        return {r[0] for r in db.execute(
            "SELECT role FROM claim_roles WHERE claim_id=? AND session_id=?",
            (claim_id, session_id))}

    @staticmethod
    def _claim_dict(r) -> dict:
        return {"claim": f"C{r['claim_id']}", "claim_id": r["claim_id"], "owner": r["owner_name"],
                "owner_session_id": r["owner_session_id"], "scope_type": r["scope_type"],
                "scope": scopes.display(r["scope_type"], r["scope"]), "note": r["note"],
                "fence": r["fence"], "expires_at": iso(r["expires_at"]),
                "release_on_commit": bool(r["release_on_commit"]), "project": r["project_id"],
                "released": r["released_at"] is not None}

    @staticmethod
    def _msg_dict(r) -> dict:
        d = {"id": r["id"], "from": r["from_name"], "to": r["to_name"], "kind": r["kind"],
             "body": r["body"], "at": iso(r["created_at"]), "thread": r["thread_id"],
             "reply_to": r["reply_to"], "project": r["project_id"]}
        if r["claim_id"]:
            d["claim"] = f"C{r['claim_id']}"
        if r["resolved_at"]:
            d.update(resolved_at=iso(r["resolved_at"]), resolved_by=r["resolved_by"],
                     resolution=r["resolution"])
        return d

    # --- sessions -------------------------------------------------------
    def whoami(self, family: str, project: str = "default", principal: str | None = None,
               client_id: str | None = None) -> dict:
        family = (family or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", family):
            raise CoordError("bad_family", "family must be 1-40 letters/digits/_/-")
        project = project or "default"

        def fn(db):
            now = self.clock()
            self._reap(db)
            db.execute("INSERT OR IGNORE INTO repos VALUES(?,?,?)",
                       (project, project.split("/")[0] if "/" in project else None, now))
            taken = set()
            for (name,) in db.execute(
                    "SELECT display_name FROM sessions WHERE family=? AND ended_at IS NULL", (family,)):
                m = re.fullmatch(re.escape(family) + r"-(\d+)", name)
                if m:
                    taken.add(int(m.group(1)))
            n = next(i for i in range(1, 10000) if i not in taken)
            name = f"{family}-{n:02d}"
            gen = 1 + (db.execute("SELECT COALESCE(MAX(generation),0) FROM sessions WHERE display_name=?",
                                  (name,)).fetchone()[0])
            sid = str(uuid.uuid4())
            db.execute("INSERT INTO sessions(session_id, display_name, family, generation, project_id,"
                       " principal, status, started_at, heartbeat_at, cursor) VALUES(?,?,?,?,?,?,?,?,?,"
                       " (SELECT COALESCE(MAX(id),0) FROM messages))",
                       (sid, name, family, gen, project, principal, "started", now, now))
            self._event(db, project, "session.started", sid, "session", sid, name=name, generation=gen)
            return {"session_id": sid, "name": name, "generation": gen, "project": project}
        return self._mutate("whoami", client_id, fn)

    def check_principal(self, session: str, principal: str | None) -> None:
        with self._read() as db:
            row = db.execute("SELECT principal FROM sessions WHERE session_id=?", (session,)).fetchone()
        if row is not None and row["principal"] and row["principal"] != principal:
            raise CoordError("forbidden", "session is bound to a different principal")

    def session_project(self, session: str) -> str | None:
        with self._read() as db:
            row = db.execute("SELECT project_id FROM sessions WHERE session_id=?", (session,)).fetchone()
        return row["project_id"] if row else None

    def heartbeat(self, session: str, status: str = "") -> dict:
        with self._tx() as db:
            me = self._session(db, session)
            db.execute("UPDATE sessions SET heartbeat_at=?, status=? WHERE session_id=?",
                       (self.clock(), (status or "")[:400], session))
            return {"name": me["display_name"], "status": status}

    def end(self, session: str) -> dict:
        with self._tx() as db:
            me = self._session(db, session)
            now = self.clock()
            n = db.execute("UPDATE claims SET released_at=?, released_by=? WHERE owner_session_id=?"
                           " AND released_at IS NULL", (now, session, session)).rowcount
            db.execute("UPDATE sessions SET ended_at=? WHERE session_id=?", (now, session))
            self._event(db, me["project_id"], "session.ended", session, "session", session)
            return {"name": me["display_name"], "released_claims": n}

    def presence(self, project: str | None = None, include_dead: bool = False) -> list[dict]:
        with self._read() as db:
            rows = db.execute("SELECT * FROM sessions" + (" WHERE project_id=?" if project else "")
                              + " ORDER BY heartbeat_at DESC", (project,) if project else ()).fetchall()
            return [{"name": r["display_name"], "generation": r["generation"], "status": r["status"],
                     "project": r["project_id"], "live": self._live(r), "seen": iso(r["heartbeat_at"])}
                    for r in rows if include_dead or self._live(r)]

    # --- messages -------------------------------------------------------
    def post(self, session: str, body: str, kind: str = "info", to: str | None = None,
             reply_to: int | None = None, claim: str | None = None,
             client_id: str | None = None) -> dict:
        body = (body or "").strip()
        if not body:
            raise CoordError("empty", "empty message")
        if len(body) > MSG_MAX:
            raise CoordError("too_long", f"message is {len(body)} chars; the limit is {MSG_MAX} "
                             "- put long content in a document (`coord doc create`)")
        if kind not in MESSAGE_KINDS:
            raise CoordError("bad_kind", f"kind must be one of {', '.join(MESSAGE_KINDS)}")

        def fn(db):
            me = self._session(db, session)
            project = me["project_id"]
            to_row = self._resolve_name(db, to, project) if to else None
            thread = None
            if reply_to is not None:
                parent = db.execute("SELECT * FROM messages WHERE id=?", (int(reply_to),)).fetchone()
                if parent is None:
                    raise CoordError("missing", f"no message #{reply_to}")
                thread = parent["thread_id"] or parent["id"]
            claim_id = parse_id("claim", claim) if claim else None
            cur = db.execute(
                "INSERT INTO messages(project_id, from_session_id, from_name, to_session_id, to_name,"
                " thread_id, reply_to, kind, body, claim_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (project, session, me["display_name"], to_row["session_id"] if to_row else None,
                 to_row["display_name"] if to_row else None, thread,
                 int(reply_to) if reply_to is not None else None, kind, body, claim_id, self.clock()))
            mid = cur.lastrowid
            if thread is None:
                db.execute("UPDATE messages SET thread_id=? WHERE id=?", (mid, mid))
            self._event(db, project, "message.posted", session, "message", mid, kind=kind)
            out = {"id": mid, "thread": thread or mid, "to": to_row["display_name"] if to_row else None}
            if len(body) > MSG_RECOMMENDED:
                out["warning"] = (f"{len(body)} chars (> {MSG_RECOMMENDED} recommended); "
                                  "consider a document for long analyses")
            return out
        return self._mutate("post", client_id, fn)

    def reply(self, session: str, message: int, body: str, kind: str = "info",
              client_id: str | None = None) -> dict:
        with self._read() as db:
            parent = db.execute("SELECT * FROM messages WHERE id=?", (int(message),)).fetchone()
        if parent is None:
            raise CoordError("missing", f"no message #{message}")
        to = None
        if parent["from_session_id"] != session:
            with self._read() as db:
                s = db.execute("SELECT * FROM sessions WHERE session_id=?",
                               (parent["from_session_id"],)).fetchone()
            to = parent["from_name"] if s is not None and self._live(s) else None
        return self.post(session, body, kind=kind, to=to, reply_to=int(message), client_id=client_id)

    def _visible(self, session: str | None) -> tuple[str, tuple]:
        if session:
            return ("(to_session_id IS NULL OR to_session_id=? OR from_session_id=?)", (session, session))
        return ("to_session_id IS NULL", ())

    def inbox(self, session: str | None = None, after: int | None = None, to_me: bool = False,
              sender: str | None = None, kind: str | None = None, project: str | None = None,
              limit: int | None = INBOX_DEFAULT, unresolved: bool = False) -> list[dict]:
        with self._read() as db:
            if session and project is None:
                row = db.execute("SELECT project_id FROM sessions WHERE session_id=?", (session,)).fetchone()
                project = row["project_id"] if row else None
            where, args = [], []
            vis, vargs = self._visible(session)
            where.append(vis)
            args += vargs
            if project:
                where.append("project_id=?"); args.append(project)
            if after is not None:
                where.append("id>?"); args.append(int(after))
            if to_me and session:
                where.append("to_session_id=?"); args.append(session)
            if sender:
                where.append("from_name=?"); args.append(sender)
            if kind:
                where.append("kind=?"); args.append(kind)
            if unresolved:
                where.append("resolved_at IS NULL")
            q = "SELECT * FROM messages WHERE " + " AND ".join(where) + " ORDER BY id DESC"
            if limit:
                q += f" LIMIT {int(limit)}"
            rows = db.execute(q, args).fetchall()
        return [self._msg_dict(r) for r in reversed(rows)]

    def thread(self, message: int, session: str | None = None) -> list[dict]:
        with self._read() as db:
            m = db.execute("SELECT thread_id FROM messages WHERE id=?", (int(message),)).fetchone()
            if m is None:
                raise CoordError("missing", f"no message #{message}")
            vis, vargs = self._visible(session)
            rows = db.execute(f"SELECT * FROM messages WHERE thread_id=? AND {vis} ORDER BY id",
                              (m["thread_id"], *vargs)).fetchall()
        return [self._msg_dict(r) for r in rows]

    def resolve(self, session: str, message: int, resolution: str = "",
                client_id: str | None = None) -> dict:
        def fn(db):
            me = self._session(db, session)
            m = db.execute("SELECT * FROM messages WHERE id=?", (int(message),)).fetchone()
            if m is None:
                raise CoordError("missing", f"no message #{message}")
            if m["resolved_at"]:
                return {"id": m["id"], "outcome": "already", "resolved_by": m["resolved_by"]}
            db.execute("UPDATE messages SET resolved_at=?, resolved_by=?, resolution=? WHERE id=?",
                       (self.clock(), me["display_name"], resolution, m["id"]))
            self._event(db, me["project_id"], "message.resolved", session, "message", m["id"])
            return {"id": m["id"], "outcome": "ok", "resolved_by": me["display_name"]}
        return self._mutate("resolve", client_id, fn)

    def poll(self, session: str) -> dict:
        """Everything new since this session's cursor (by message id, never time)."""
        with self._tx() as db:
            me = self._session(db, session)
            vis, vargs = self._visible(session)
            rows = db.execute(f"SELECT * FROM messages WHERE id>? AND project_id=? AND {vis}"
                              " ORDER BY id", (me["cursor"], me["project_id"], *vargs)).fetchall()
            if rows:
                db.execute("UPDATE sessions SET cursor=? WHERE session_id=?", (rows[-1]["id"], session))
            db.execute("UPDATE sessions SET heartbeat_at=? WHERE session_id=?", (self.clock(), session))
            project = me["project_id"]
        return {"messages": [self._msg_dict(r) for r in rows],
                "my_claims": self.locks(project=project, owner_session=session),
                "tasks": [t for t in self.tasks(project=project, status="open")]
                + self.tasks(project=project, status="accepted", assigned_session=session),
                "discussions": self.discussions(project=project)}

    # --- claims ---------------------------------------------------------
    def claim(self, session: str, scope: str, tree: bool | None = None, note: str = "",
              ttl: int = CLAIM_TTL, release_on_commit: bool = False,
              client_id: str | None = None) -> dict:
        stype, path = self._norm(scope, tree)

        def fn(db):
            me = self._session(db, session)
            project = me["project_id"]
            self._reap(db)
            for c in self._active_claims(db, project):
                if not scopes.overlaps(stype, path, c["scope_type"], c["scope"]):
                    continue
                if c["owner_session_id"] == session:
                    if c["scope_type"] == stype and c["scope"] == path:
                        raise CoordError("already_held", f"you already hold C{c['claim_id']}; "
                                         f"use `coord renew C{c['claim_id']}`", self._claim_dict(c))
                    continue
                if "delegate" in self._roles(db, c["claim_id"], session) and \
                        scopes.contains(c["scope_type"], c["scope"], stype, path):
                    continue
                raise CoordError("conflict", f"{scopes.display(stype, path)} overlaps C{c['claim_id']} "
                                 f"({scopes.display(c['scope_type'], c['scope'])}) held by "
                                 f"{c['owner_name']}", {"held_by": self._claim_dict(c)})
            now = self.clock()
            fence = self._next_fence(db)
            cur = db.execute(
                "INSERT INTO claims(project_id, owner_session_id, owner_name, scope_type, scope, note,"
                " claimed_at, expires_at, fence, release_on_commit) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (project, session, me["display_name"], stype, path, note or "", now, now + ttl,
                 fence, int(bool(release_on_commit))))
            row = db.execute("SELECT * FROM claims WHERE claim_id=?", (cur.lastrowid,)).fetchone()
            self._event(db, project, "claim.acquired", session, "claim", cur.lastrowid,
                        scope=scopes.display(stype, path), fence=fence)
            return self._claim_dict(row)
        return self._mutate("claim", client_id, fn)

    def _owned(self, db, session, claim) -> sqlite3.Row:
        cid = parse_id("claim", claim)
        row = db.execute("SELECT * FROM claims WHERE claim_id=?", (cid,)).fetchone()
        if row is None:
            raise CoordError("missing", f"no claim C{cid}")
        if row["owner_session_id"] != session:
            raise CoordError("not_owner", f"C{cid} is owned by {row['owner_name']} "
                             f"(session {row['owner_session_id'][:8]}), not you")
        if row["released_at"] is not None:
            raise CoordError("released", f"C{cid} was already released")
        return row

    def renew(self, session: str, claim: str, ttl: int = CLAIM_TTL) -> dict:
        with self._tx() as db:
            me = self._session(db, session)
            row = self._owned(db, session, claim)
            if row["expires_at"] <= self.clock():
                raise CoordError("expired", f"C{row['claim_id']} expired; claim the scope again "
                                 "(you will get a new fence)")
            db.execute("UPDATE claims SET expires_at=? WHERE claim_id=?",
                       (self.clock() + ttl, row["claim_id"]))
            self._event(db, me["project_id"], "claim.renewed", session, "claim", row["claim_id"])
            return self._claim_dict(db.execute("SELECT * FROM claims WHERE claim_id=?",
                                               (row["claim_id"],)).fetchone())

    def release(self, session: str, claim: str | None = None, all: bool = False) -> dict:
        with self._tx() as db:
            me = self._session(db, session)
            now = self.clock()
            if all:
                ids = [r[0] for r in db.execute(
                    "SELECT claim_id FROM claims WHERE owner_session_id=? AND released_at IS NULL",
                    (session,))]
            else:
                if not claim:
                    raise CoordError("usage", "give a claim id (C12) or --all")
                ids = [self._owned(db, session, claim)["claim_id"]]
            for cid in ids:
                db.execute("UPDATE claims SET released_at=?, released_by=? WHERE claim_id=?",
                           (now, session, cid))
                self._event(db, me["project_id"], "claim.released", session, "claim", cid)
            return {"released": [f"C{i}" for i in ids]}

    def locks(self, project: str | None = None, all: bool = False,
              owner_session: str | None = None) -> list[dict]:
        with self._read() as db:
            q, args = "SELECT * FROM claims WHERE 1=1", []
            if project:
                q += " AND project_id=?"; args.append(project)
            if not all:
                q += " AND released_at IS NULL AND expires_at>?" + self._LIVE_OWNER
                args += [self.clock(), self.clock() - SESSION_TTL]
            if owner_session:
                q += " AND owner_session_id=?"; args.append(owner_session)
            rows = db.execute(q + " ORDER BY claim_id", args).fetchall()
        return [self._claim_dict(r) for r in rows]

    def fence_check(self, claim: str, fence: int) -> dict:
        """Guard a write with the fence you got at claim time: stale leases fail."""
        cid = parse_id("claim", claim)
        with self._read() as db:
            row = db.execute("SELECT * FROM claims WHERE claim_id=?", (cid,)).fetchone()
        if row is None or row["released_at"] is not None or row["expires_at"] <= self.clock():
            raise CoordError("stale_fence", f"C{cid} is no longer active")
        if int(fence) != row["fence"]:
            raise CoordError("stale_fence", f"fence {fence} is stale for C{cid} (current {row['fence']})")
        return {"ok": True, "claim": f"C{cid}", "fence": row["fence"]}

    def grant(self, session: str, claim: str, to: str, role: str) -> dict:
        if role not in ROLES:
            raise CoordError("bad_role", f"role must be one of {', '.join(ROLES)}")
        with self._tx() as db:
            me = self._session(db, session)
            row = self._owned(db, session, claim)
            target = self._resolve_name(db, to, me["project_id"])
            db.execute("INSERT OR IGNORE INTO claim_roles VALUES(?,?,?,?,?)",
                       (row["claim_id"], target["session_id"], role, session, self.clock()))
            self._event(db, me["project_id"], "claim.role_granted", session, "claim", row["claim_id"],
                        to=target["display_name"], role=role)
            return {"claim": f"C{row['claim_id']}", "to": target["display_name"], "role": role}

    def revoke(self, session: str, claim: str, to: str, role: str) -> dict:
        with self._tx() as db:
            me = self._session(db, session)
            row = self._owned(db, session, claim)
            target = self._resolve_name(db, to, me["project_id"])
            db.execute("DELETE FROM claim_roles WHERE claim_id=? AND session_id=? AND role=?",
                       (row["claim_id"], target["session_id"], role))
            return {"claim": f"C{row['claim_id']}", "to": target["display_name"], "revoked": role}

    def roles(self, claim: str) -> list[dict]:
        cid = parse_id("claim", claim)
        with self._read() as db:
            rows = db.execute("SELECT r.role, s.display_name FROM claim_roles r JOIN sessions s"
                              " ON s.session_id=r.session_id WHERE claim_id=?", (cid,)).fetchall()
        return [{"session": r["display_name"], "role": r["role"]} for r in rows]

    def ask(self, session: str, claim: str, to: str, body: str, role: str = "advisor",
            kind: str = "question", client_id: str | None = None) -> dict:
        """Ask for help on a claim you keep: grants a non-owner role, never releases."""
        if role not in ROLES:
            raise CoordError("bad_role", f"role must be one of {', '.join(ROLES)}")
        with self._read() as db:
            row = self._owned(db, session, claim)
            if row["expires_at"] <= self.clock():
                raise CoordError("expired", f"C{row['claim_id']} expired")
        out = self.post(session, body, kind=kind, to=to, claim=claim, client_id=client_id)
        if not out.get("replayed"):
            self.grant(session, claim, to, role)
        out.update(claim=f"C{row['claim_id']}", role=role, kept=True)
        return out

    def check(self, session: str, files: list[str]) -> dict:
        """Pre-commit: files claimed by someone else (owner/delegate only may write)."""
        with self._read() as db:
            me = self._session(db, session)
            active = self._active_claims(db, me["project_id"])
            conflicts = []
            for f in files:
                try:
                    stype, path = self._norm(f, False)
                except CoordError:
                    continue
                for c in active:
                    if c["owner_session_id"] == session:
                        continue
                    if not scopes.overlaps("exact", path, c["scope_type"], c["scope"]):
                        continue
                    if "delegate" in self._roles(db, c["claim_id"], session):
                        continue
                    conflicts.append({"file": path, "claim": f"C{c['claim_id']}", "owner": c["owner_name"],
                                      "scope": scopes.display(c["scope_type"], c["scope"])})
        return {"ok": not conflicts, "conflicts": conflicts}

    def post_commit(self, session: str, sha: str, files: list[str]) -> dict:
        with self._tx() as db:
            me = self._session(db, session)
            paths = set()
            for f in files:
                try:
                    paths.add(self._norm(f, False)[1])
                except CoordError:
                    pass
            released = []
            for c in db.execute("SELECT * FROM claims WHERE owner_session_id=? AND released_at IS NULL"
                                " AND scope_type='exact' AND release_on_commit=1", (session,)).fetchall():
                if c["scope"] in paths:
                    db.execute("UPDATE claims SET released_at=?, released_by=? WHERE claim_id=?",
                               (self.clock(), session, c["claim_id"]))
                    released.append(f"C{c['claim_id']}")
            self._event(db, me["project_id"], "commit.created", session, "commit", sha,
                        files=sorted(paths), released=released)
            return {"sha": sha, "released": released}

    # --- discussions / consensus ---------------------------------------
    def discuss(self, session: str, topic: str, claim: str | None = None,
                client_id: str | None = None) -> dict:
        def fn(db):
            me = self._session(db, session)
            cid = parse_id("claim", claim) if claim else None
            cur = db.execute("INSERT INTO discussions(project_id, created_by, created_by_name, topic,"
                             " claim_id, created_at) VALUES(?,?,?,?,?,?)",
                             (me["project_id"], session, me["display_name"], topic, cid, self.clock()))
            did = cur.lastrowid
            m = db.execute("INSERT INTO messages(project_id, from_session_id, from_name, kind, body,"
                           " claim_id, created_at) VALUES(?,?,?,?,?,?,?)",
                           (me["project_id"], session, me["display_name"], "question",
                            f"[D{did}] {topic}", cid, self.clock())).lastrowid
            db.execute("UPDATE messages SET thread_id=? WHERE id=?", (m, m))
            db.execute("UPDATE discussions SET message_id=? WHERE id=?", (m, did))
            self._event(db, me["project_id"], "discussion.opened", session, "discussion", did)
            return {"discussion": f"D{did}", "id": did, "message": m}
        return self._mutate("discuss", client_id, fn)

    def propose(self, session: str, discussion: str, body: str, client_id: str | None = None) -> dict:
        def fn(db):
            me = self._session(db, session)
            did = parse_id("discussion", discussion)
            d = db.execute("SELECT * FROM discussions WHERE id=?", (did,)).fetchone()
            if d is None:
                raise CoordError("missing", f"no discussion D{did}")
            if d["status"] != "open":
                raise CoordError("closed", f"D{did} is {d['status']}")
            pid = db.execute("INSERT INTO proposals(discussion_id, author_session_id, author_name,"
                             " body, created_at) VALUES(?,?,?,?,?)",
                             (did, session, me["display_name"], body, self.clock())).lastrowid
            m = db.execute("INSERT INTO messages(project_id, from_session_id, from_name, kind, body,"
                           " thread_id, reply_to, created_at) VALUES(?,?,?,?,?,?,?,?)",
                           (d["project_id"], session, me["display_name"], "proposal",
                            f"[P{pid} on D{did}] {body}", d["message_id"], d["message_id"],
                            self.clock())).lastrowid
            self._event(db, d["project_id"], "proposal.created", session, "proposal", pid)
            return {"proposal": f"P{pid}", "id": pid, "message": m}
        return self._mutate("propose", client_id, fn)

    def react(self, session: str, proposal: str, stance: str, comment: str = "") -> dict:
        if stance not in STANCES:
            raise CoordError("bad_stance", f"stance must be one of {', '.join(STANCES)}")
        with self._tx() as db:
            me = self._session(db, session)
            pid = parse_id("proposal", proposal)
            p = db.execute("SELECT p.*, d.status AS dstatus, d.project_id FROM proposals p JOIN"
                           " discussions d ON d.id=p.discussion_id WHERE p.id=?", (pid,)).fetchone()
            if p is None:
                raise CoordError("missing", f"no proposal P{pid}")
            if p["dstatus"] != "open":
                raise CoordError("closed", "discussion is closed")
            db.execute("INSERT INTO reactions VALUES(?,?,?,?,?,?) ON CONFLICT(proposal_id,"
                       " author_session_id) DO UPDATE SET stance=excluded.stance,"
                       " comment=excluded.comment, created_at=excluded.created_at",
                       (pid, session, me["display_name"], stance, comment, self.clock()))
            self._event(db, p["project_id"], "proposal.reacted", session, "proposal", pid, stance=stance)
            return {"proposal": f"P{pid}", "stance": stance}

    def discussion(self, discussion: str) -> dict:
        did = parse_id("discussion", discussion)
        with self._read() as db:
            d = db.execute("SELECT * FROM discussions WHERE id=?", (did,)).fetchone()
            if d is None:
                raise CoordError("missing", f"no discussion D{did}")
            props = []
            for p in db.execute("SELECT * FROM proposals WHERE discussion_id=? ORDER BY id", (did,)):
                rs = db.execute("SELECT * FROM reactions WHERE proposal_id=? ORDER BY created_at",
                                (p["id"],)).fetchall()
                tally = {s: 0 for s in STANCES}
                for r in rs:
                    tally[r["stance"]] += 1
                props.append({"proposal": f"P{p['id']}", "author": p["author_name"], "body": p["body"],
                              "status": p["status"], "tally": tally,
                              "reactions": [{"by": r["author_name"], "stance": r["stance"],
                                             "comment": r["comment"]} for r in rs]})
        return {"discussion": f"D{did}", "topic": d["topic"], "status": d["status"],
                "created_by": d["created_by_name"], "claim": f"C{d['claim_id']}" if d["claim_id"] else None,
                "thread": d["message_id"], "proposals": props, "decision": d["decision"],
                "consensus": None if d["consensus"] is None else bool(d["consensus"]),
                "decided_by": d["decided_by"], "decided_at": iso(d["decided_at"]),
                "decision_document": f"DOC{d['decision_document_id']}" if d["decision_document_id"] else None}

    def discussions(self, project: str | None = None, status: str = "open") -> list[dict]:
        with self._read() as db:
            q, a = "SELECT * FROM discussions WHERE status=?", [status]
            if project:
                q += " AND project_id=?"; a.append(project)
            rows = db.execute(q + " ORDER BY id", a).fetchall()
        return [{"discussion": f"D{r['id']}", "topic": r["topic"], "by": r["created_by_name"]} for r in rows]

    def decide(self, session: str, discussion: str, decision: str, proposal: str | None = None,
               consensus: bool = True) -> dict:
        """Close a discussion explicitly: who decided, when, whether it was consensus."""
        with self._tx() as db:
            me = self._session(db, session)
            did = parse_id("discussion", discussion)
            d = db.execute("SELECT * FROM discussions WHERE id=?", (did,)).fetchone()
            if d is None:
                raise CoordError("missing", f"no discussion D{did}")
            if d["created_by"] != session:
                raise CoordError("forbidden", f"only {d['created_by_name']} (who opened D{did}) can decide")
            if d["status"] != "open":
                raise CoordError("closed", f"D{did} is already {d['status']}")
            pid = parse_id("proposal", proposal) if proposal else None
            if pid is not None:
                if db.execute("SELECT 1 FROM proposals WHERE id=? AND discussion_id=?", (pid, did)).fetchone() is None:
                    raise CoordError("missing", f"P{pid} is not part of D{did}")
                db.execute("UPDATE proposals SET status=CASE WHEN id=? THEN 'accepted' ELSE 'rejected' END"
                           " WHERE discussion_id=?", (pid, did))
            now = self.clock()
            content = (f"# Decision: {d['topic']}\n\n{decision}\n\n"
                       f"- discussion: D{did}\n- accepted proposal: {f'P{pid}' if pid else 'none'}\n"
                       f"- consensus: {'yes' if consensus else 'no'}\n- decided by: {me['display_name']}\n")
            doc = self._doc_insert(db, me, f"Decision D{did}: {d['topic']}", "decision", content)
            db.execute("UPDATE documents SET status='final' WHERE id=?", (doc,))
            db.execute("UPDATE discussions SET status='decided', decision=?, consensus=?, decided_by=?,"
                       " decided_at=?, decision_document_id=? WHERE id=?",
                       (decision, int(bool(consensus)), me["display_name"], now, doc, did))
            db.execute("INSERT INTO messages(project_id, from_session_id, from_name, kind, body, thread_id,"
                       " reply_to, created_at) VALUES(?,?,?,?,?,?,?,?)",
                       (d["project_id"], session, me["display_name"], "decision",
                        f"[D{did} decided -> DOC{doc}] {decision}", d["message_id"], d["message_id"], now))
            self._event(db, d["project_id"], "discussion.decided", session, "discussion", did, document=doc)
            return {"discussion": f"D{did}", "document": f"DOC{doc}", "consensus": bool(consensus)}

    # --- documents ------------------------------------------------------
    def _doc_insert(self, db, me, title, kind, content, message="created") -> int:
        now = self.clock()
        did = db.execute("INSERT INTO documents(project_id, title, kind, created_by, revision, content,"
                         " created_at, updated_at) VALUES(?,?,?,?,1,?,?,?)",
                         (me["project_id"], title, kind, me["display_name"], content, now, now)).lastrowid
        db.execute("INSERT INTO document_revisions VALUES(?,?,?,?,?,?,?)",
                   (did, 1, me["session_id"], me["display_name"], content, message, now))
        self._event(db, me["project_id"], "document.created", me["session_id"], "document", did)
        return did

    def doc_create(self, session: str, title: str, kind: str = "note", content: str = "",
                   client_id: str | None = None) -> dict:
        if kind not in DOC_KINDS:
            raise CoordError("bad_kind", f"document kind must be one of {', '.join(DOC_KINDS)}")

        def fn(db):
            me = self._session(db, session)
            did = self._doc_insert(db, me, title, kind, content)
            return {"document": f"DOC{did}", "id": did, "revision": 1}
        return self._mutate("doc_create", client_id, fn)

    def doc_show(self, document: str, revision: int | None = None) -> dict:
        did = parse_id("document", document)
        with self._read() as db:
            d = db.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
            if d is None:
                raise CoordError("missing", f"no document DOC{did}")
            content, rev = d["content"], d["revision"]
            if revision is not None:
                r = db.execute("SELECT * FROM document_revisions WHERE document_id=? AND revision=?",
                               (did, int(revision))).fetchone()
                if r is None:
                    raise CoordError("missing", f"DOC{did} has no revision {revision}")
                content, rev = r["content"], r["revision"]
        return {"document": f"DOC{did}", "title": d["title"], "kind": d["kind"], "status": d["status"],
                "revision": rev, "latest_revision": d["revision"], "created_by": d["created_by"],
                "updated_at": iso(d["updated_at"]), "content": content}

    def doc_edit(self, session: str, document: str, base_revision: int, content: str,
                 message: str = "", client_id: str | None = None) -> dict:
        """Optimistic concurrency: succeeds only if nobody edited since base_revision."""
        def fn(db):
            me = self._session(db, session)
            did = parse_id("document", document)
            d = db.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
            if d is None:
                raise CoordError("missing", f"no document DOC{did}")
            if d["status"] == "final":
                raise CoordError("final", f"DOC{did} is final; create a new document")
            if d["revision"] != int(base_revision):
                raise CoordError("revision_conflict",
                                 f"DOC{did} is at revision {d['revision']}, you edited {base_revision}; "
                                 "merge with the current content and retry with --base-revision "
                                 f"{d['revision']}",
                                 {"current_revision": d["revision"], "current_content": d["content"]})
            rev = d["revision"] + 1
            now = self.clock()
            db.execute("UPDATE documents SET revision=?, content=?, updated_at=? WHERE id=? AND revision=?",
                       (rev, content, now, did, d["revision"]))
            db.execute("INSERT INTO document_revisions VALUES(?,?,?,?,?,?,?)",
                       (did, rev, session, me["display_name"], content, message, now))
            self._event(db, d["project_id"], "document.edited", session, "document", did, revision=rev)
            return {"document": f"DOC{did}", "revision": rev}
        return self._mutate("doc_edit", client_id, fn)

    def doc_history(self, document: str) -> list[dict]:
        did = parse_id("document", document)
        with self._read() as db:
            rows = db.execute("SELECT * FROM document_revisions WHERE document_id=? ORDER BY revision",
                              (did,)).fetchall()
        if not rows:
            raise CoordError("missing", f"no document DOC{did}")
        return [{"revision": r["revision"], "author": r["author_name"], "message": r["message"],
                 "at": iso(r["created_at"]), "chars": len(r["content"])} for r in rows]

    def docs(self, project: str | None = None, kind: str | None = None) -> list[dict]:
        with self._read() as db:
            q, a = "SELECT * FROM documents WHERE 1=1", []
            if project:
                q += " AND project_id=?"; a.append(project)
            if kind:
                q += " AND kind=?"; a.append(kind)
            rows = db.execute(q + " ORDER BY id", a).fetchall()
        return [{"document": f"DOC{r['id']}", "title": r["title"], "kind": r["kind"],
                 "revision": r["revision"], "status": r["status"]} for r in rows]

    # --- tasks ----------------------------------------------------------
    def task_create(self, session: str, title: str, description: str = "", priority: int = 0,
                    claim: str | None = None, assign: str | None = None, category: str | None = None,
                    client_id: str | None = None) -> dict:
        def fn(db):
            me = self._session(db, session)
            target = self._resolve_name(db, assign, me["project_id"]) if assign else None
            now = self.clock()
            tid = db.execute("INSERT INTO tasks(project_id, created_by, assigned_to, assigned_name, title,"
                             " description, status, priority, related_claim_id, category, created_at,"
                             " updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                             (me["project_id"], me["display_name"], target["session_id"] if target else None,
                              target["display_name"] if target else None, title, description,
                              "accepted" if target else "open", int(priority),
                              parse_id("claim", claim) if claim else None, category, now, now)).lastrowid
            self._event(db, me["project_id"], "task.created", session, "task", tid)
            return {"task": f"T{tid}", "id": tid}
        return self._mutate("task_create", client_id, fn)

    def tasks(self, project: str | None = None, status: str | None = None,
              assigned_session: str | None = None) -> list[dict]:
        with self._read() as db:
            q, a = "SELECT * FROM tasks WHERE 1=1", []
            if project:
                q += " AND project_id=?"; a.append(project)
            if status:
                q += " AND status=?"; a.append(status)
            if assigned_session:
                q += " AND assigned_to=?"; a.append(assigned_session)
            rows = db.execute(q + " ORDER BY priority DESC, task_id", a).fetchall()
        return [{"task": f"T{r['task_id']}", "title": r["title"], "status": r["status"],
                 "priority": r["priority"], "assigned": r["assigned_name"], "created_by": r["created_by"],
                 "claim": f"C{r['related_claim_id']}" if r["related_claim_id"] else None,
                 "category": r["category"]} for r in rows]

    def _task_update(self, session, task, status, note=None, require_assignee=False):
        with self._tx() as db:
            me = self._session(db, session)
            tid = parse_id("task", task)
            t = db.execute("SELECT * FROM tasks WHERE task_id=?", (tid,)).fetchone()
            if t is None:
                raise CoordError("missing", f"no task T{tid}")
            if status == "accepted" and t["status"] != "open":
                raise CoordError("taken", f"T{tid} is {t['status']} ({t['assigned_name'] or '-'})")
            if require_assignee and t["assigned_to"] not in (None, session):
                raise CoordError("forbidden", f"T{tid} is assigned to {t['assigned_name']}")
            db.execute("UPDATE tasks SET status=?, assigned_to=COALESCE(assigned_to, ?),"
                       " assigned_name=COALESCE(assigned_name, ?), note=COALESCE(?, note), updated_at=?"
                       " WHERE task_id=?", (status, session, me["display_name"], note, self.clock(), tid))
            self._event(db, t["project_id"], f"task.{status}", session, "task", tid)
            return {"task": f"T{tid}", "status": status}

    def task_accept(self, session: str, task: str) -> dict:
        return self._task_update(session, task, "accepted")

    def task_done(self, session: str, task: str, note: str = "") -> dict:
        return self._task_update(session, task, "done", note, require_assignee=True)

    # --- project memory -------------------------------------------------
    def memory_add(self, session: str, kind: str, title: str, content: str, source: str = "",
                   client_id: str | None = None) -> dict:
        if kind not in MEMORY_KINDS:
            raise CoordError("bad_kind", f"memory kind must be one of {', '.join(MEMORY_KINDS)}")

        def fn(db):
            me = self._session(db, session)
            now = self.clock()
            mid = db.execute("INSERT INTO project_memory(project_id, kind, title, content, source,"
                             " created_by, updated_by, revision, created_at, updated_at)"
                             " VALUES(?,?,?,?,?,?,?,1,?,?)",
                             (me["project_id"], kind, title, content, source, me["display_name"],
                              me["display_name"], now, now)).lastrowid
            db.execute("INSERT INTO memory_revisions VALUES(?,?,?,?,?)",
                       (mid, 1, content, me["display_name"], now))
            self._event(db, me["project_id"], "memory.added", session, "memory", mid)
            return {"memory": f"M{mid}", "revision": 1}
        return self._mutate("memory_add", client_id, fn)

    def memory_edit(self, session: str, memory: str, base_revision: int, content: str,
                    status: str | None = None) -> dict:
        with self._tx() as db:
            me = self._session(db, session)
            mid = parse_id("memory", memory)
            m = db.execute("SELECT * FROM project_memory WHERE memory_id=?", (mid,)).fetchone()
            if m is None:
                raise CoordError("missing", f"no memory M{mid}")
            if m["revision"] != int(base_revision):
                raise CoordError("revision_conflict", f"M{mid} is at revision {m['revision']}",
                                 {"current_revision": m["revision"], "current_content": m["content"]})
            rev, now = m["revision"] + 1, self.clock()
            db.execute("UPDATE project_memory SET content=?, revision=?, updated_by=?, updated_at=?,"
                       " status=COALESCE(?, status) WHERE memory_id=?",
                       (content, rev, me["display_name"], now, status, mid))
            db.execute("INSERT INTO memory_revisions VALUES(?,?,?,?,?)", (mid, rev, content, me["display_name"], now))
            return {"memory": f"M{mid}", "revision": rev}

    def memory(self, project: str | None = None, kind: str | None = None, query: str | None = None,
               include_archived: bool = False) -> list[dict]:
        with self._read() as db:
            q, a = "SELECT * FROM project_memory WHERE 1=1", []
            if project:
                q += " AND project_id=?"; a.append(project)
            if kind:
                q += " AND kind=?"; a.append(kind)
            if not include_archived:
                q += " AND status='active'"
            for word in (query or "").split():
                q += " AND (title LIKE ? OR content LIKE ?)"; a += [f"%{word}%", f"%{word}%"]
            rows = db.execute(q + " ORDER BY kind, memory_id", a).fetchall()
        return [{"memory": f"M{r['memory_id']}", "kind": r["kind"], "title": r["title"],
                 "content": r["content"], "source": r["source"], "revision": r["revision"],
                 "created_by": r["created_by"], "updated_by": r["updated_by"],
                 "updated_at": iso(r["updated_at"])} for r in rows]

    # --- profiles & routing --------------------------------------------
    def profile_set(self, session: str, provider: str | None = None, model_id: str | None = None,
                    model_family: str | None = None, category: str | None = None,
                    reasoning_level: str | None = None, capabilities: list[str] | None = None) -> dict:
        with self._tx() as db:
            me = self._session(db, session)
            db.execute("INSERT INTO agent_profiles VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE"
                       " SET provider=excluded.provider, model_id=excluded.model_id,"
                       " model_family=excluded.model_family, category=excluded.category,"
                       " reasoning_level=excluded.reasoning_level,"
                       " capabilities_json=excluded.capabilities_json, updated_at=excluded.updated_at",
                       (session, provider, model_id, model_family, category, reasoning_level,
                        json.dumps(sorted(set(capabilities or []))), self.clock()))
            return {"name": me["display_name"], "category": category, "capabilities": capabilities or []}

    def suggest(self, project: str | None = None, category: str | None = None,
                capability: list[str] | None = None, reasoning_level: str | None = None,
                exclude_session: str | None = None) -> list[dict]:
        """Rank live sessions by declared profile. Hints only: a human/agent chooses."""
        need = set(capability or [])
        with self._read() as db:
            rows = db.execute("SELECT s.*, p.provider, p.model_id, p.category, p.reasoning_level,"
                              " p.capabilities_json FROM sessions s LEFT JOIN agent_profiles p"
                              " ON p.session_id=s.session_id").fetchall()
        out = []
        for r in rows:
            if not self._live(r) or r["session_id"] == exclude_session:
                continue
            if project and r["project_id"] != project:
                continue
            caps = set(json.loads(r["capabilities_json"] or "[]"))
            score = (2 if category and r["category"] == category else 0) + len(need & caps) \
                + (1 if reasoning_level and r["reasoning_level"] == reasoning_level else 0)
            out.append({"name": r["display_name"], "score": score, "provider": r["provider"],
                        "model": r["model_id"], "category": r["category"],
                        "reasoning_level": r["reasoning_level"], "capabilities": sorted(caps),
                        "status": r["status"]})
        out.sort(key=lambda x: -x["score"])
        return out

    # --- overview -------------------------------------------------------
    def context(self, session: str) -> dict:
        with self._read() as db:
            me = self._session(db, session)
            unread = db.execute("SELECT COUNT(*) FROM messages WHERE id>? AND project_id=? AND"
                                " (to_session_id IS NULL OR to_session_id=?)",
                                (me["cursor"], me["project_id"], session)).fetchone()[0]
        project = me["project_id"]
        mem = self.memory(project=project)
        return {"me": {"name": me["display_name"], "generation": me["generation"], "project": project},
                "overview": [m for m in mem if m["kind"] == "overview"],
                "memory": [{"memory": m["memory"], "kind": m["kind"], "title": m["title"]}
                           for m in mem if m["kind"] != "overview"][:10],
                "open_tasks": self.tasks(project=project, status="open")[:10],
                "my_tasks": self.tasks(project=project, status="accepted", assigned_session=session),
                "discussions": self.discussions(project=project),
                "my_claims": self.locks(project=project, owner_session=session),
                "unread": unread}

    def projects(self) -> list[dict]:
        with self._read() as db:
            names = [r[0] for r in db.execute(
                "SELECT project_id FROM repos UNION SELECT project_id FROM sessions ORDER BY 1")]
        return [self.status(p) for p in names]

    def status(self, project: str | None = None) -> dict:
        with self._read() as db:
            f, a = (" WHERE project_id=?", (project,)) if project else ("", ())
            msgs = db.execute("SELECT COUNT(*), COALESCE(SUM(resolved_at IS NULL AND kind IN"
                              " ('question','warning')),0) FROM messages" + f, a).fetchone()
        return {"project": project or "(all)", "messages": msgs[0], "open_questions": msgs[1],
                "live_sessions": len(self.presence(project)), "active_claims": len(self.locks(project)),
                "open_tasks": len(self.tasks(project, "open")),
                "open_discussions": len(self.discussions(project))}

    def events(self, after: int = 0, project: str | None = None, limit: int = 100) -> list[dict]:
        with self._read() as db:
            q, a = "SELECT * FROM events WHERE event_id>?", [int(after)]
            if project:
                q += " AND project_id=?"; a.append(project)
            rows = db.execute(q + f" ORDER BY event_id LIMIT {int(limit)}", a).fetchall()
        return [{"event": r["event_id"], "kind": r["kind"], "entity": r["entity_type"], "id": r["entity_id"],
                 "payload": json.loads(r["payload_json"]), "at": iso(r["created_at"])} for r in rows]


READ_OPS = {"inbox", "thread", "locks", "fence_check", "roles", "check", "discussion", "discussions",
            "doc_show", "doc_history", "docs", "tasks", "memory", "suggest", "context", "projects",
            "status", "events", "presence"}
WRITE_OPS = {"whoami", "heartbeat", "end", "post", "reply", "resolve", "poll", "claim", "renew", "release",
             "grant", "revoke", "ask", "post_commit", "discuss", "propose", "react", "decide", "doc_create",
             "doc_edit", "task_create", "task_accept", "task_done", "memory_add", "memory_edit",
             "profile_set"}
