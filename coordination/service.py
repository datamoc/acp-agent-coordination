"""coord service: the `Coord` class every transport uses (HTTP /call, A2A, coord-local).

Identity is the session UUID, never the display name: a recycled name gets a
new session_id and a higher generation, so it cannot touch the old claims.
Each topic lives in its own module; `Coord` combines them:

  core.py       schema, constants, helpers, connections/transactions (CoordBase)
  sessions.py   whoami, heartbeat, end, presence
  messages.py   post, reply, inbox, thread, resolve, poll
  claims.py     claim, renew, release, locks, fences, ask/grant/revoke, check, post-commit
  consensus.py  discuss, propose, react, discussion(s), decide
  documents.py  doc create/show/edit/patch/history
  tasks.py      tasks and A2A push-notification configs
  memory.py     project memory (the `strategy` kind leads every context)
  routines.py   routines: recurring work (security review, docs) due by interval or commit
  routing.py    profiles, suggest, context, projects, status, events
"""

from .claims import ClaimsMixin
from .consensus import ConsensusMixin
from .core import (  # noqa: F401
    CLAIM_TTL,
    CONSENSUS_RULES,
    DEFAULT_QUORUM,
    DOC_KINDS,
    FEATURES,
    INBOX_DEFAULT,
    MEMORY_KINDS,
    MESSAGE_KINDS,
    MSG_MAX,
    MSG_RECOMMENDED,
    NEWS,
    NOTE_CONTEXTS,
    PREFIX,
    PROJECT_ROLES,
    ROLES,
    ROUTINE_OUTCOMES,
    ROUTINE_STATUSES,
    SCHEMA,
    SERVER_NAME,
    SESSION_TTL,
    STANCES,
    TASK_STATUSES,
    CoordBase,
    CoordError,
    iso,
    parse_id,
    server_version,
    version_key,
)
from .documents import DocumentsMixin
from .members import MembersMixin
from .memory import MemoryMixin
from .messages import MessagesMixin
from .routines import MIN_EVERY, RUN_LEASE, RoutinesMixin
from .routing import RoutingMixin
from .sessions import SessionsMixin
from .tasks import TasksMixin


class Coord(SessionsMixin, MessagesMixin, ClaimsMixin, ConsensusMixin, DocumentsMixin, TasksMixin, MemoryMixin, RoutinesMixin,
            RoutingMixin, MembersMixin, CoordBase):
    """The coordination service: one SQLite file, every mutation under BEGIN IMMEDIATE."""

    def server_info(self) -> dict:
        """What this server is and can do: version, features, what is new, its ops and limits."""
        v = server_version()
        return {"name": "coord", "version": v, "features": list(FEATURES), "news": NEWS.get(v),
                "ops": {"read": sorted(READ_OPS), "write": sorted(WRITE_OPS)},
                "limits": {"session_ttl": SESSION_TTL, "claim_ttl": CLAIM_TTL, "message_recommended": MSG_RECOMMENDED,
                           "message_max": MSG_MAX, "routine_lease": RUN_LEASE, "routine_min_every": MIN_EVERY}}

    def announce_version(self, version: str | None = None) -> list[str]:
        """At server start: when the version differs from the last one this database saw, tell every
        project (one info message each, with what is new since). Returns the projects told."""
        version = version or server_version()
        with self._tx() as db:
            row = db.execute("SELECT value FROM meta WHERE key='server_version'").fetchone()
            old = row["value"] if row else None
            db.execute("INSERT OR REPLACE INTO meta VALUES('server_version', ?)", (version,))
            if old == version or version == "0":
                return []
            news = [f"{v}: {text}" for v, text in NEWS.items() if version_key(v) <= version_key(version)
                    and (version_key(v) > version_key(old) if old else v == version)]
            body = (f"coord server {'upgraded ' + old + ' -> ' if old else 'now '}{version}"
                    + (". New - " + "; ".join(news) if news else "") + ". Details: coord server")
            projects = [r[0] for r in db.execute("SELECT project_id FROM repos UNION SELECT project_id FROM sessions")]
            now = self.clock()
            for p in projects:
                db.execute("INSERT INTO messages(project_id, from_session_id, from_name, kind, body, created_at)"
                           " VALUES(?,?,?,?,?,?)", (p, SERVER_NAME, SERVER_NAME, "info", body, now))
            return projects


READ_OPS = {"inbox", "thread", "locks", "fence_check", "roles", "check", "discussion", "discussions", "task_get",
            "doc_show", "doc_history", "docs", "tasks", "memory", "suggest", "context", "projects",
            "status", "events", "presence", "routines", "routine_get", "server_info", "members"}
WRITE_OPS = {"whoami", "heartbeat", "end", "post", "reply", "resolve", "poll", "claim", "renew", "release",
             "grant", "revoke", "ask", "post_commit", "discuss", "propose", "react", "decide", "doc_create",
             "doc_edit", "doc_patch", "doc_import", "task_create", "task_link", "task_accept", "task_done", "task_cancel", "task_decline", "role_accept", "role_decline", "memory_add", "memory_edit",
             "profile_set", "routine_create", "routine_start", "routine_done", "routine_update",
             "member_set", "member_remove"}
