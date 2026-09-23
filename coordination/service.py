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
  memory.py     project memory
  routing.py    profiles, suggest, context, projects, status, events
"""

from .claims import ClaimsMixin
from .consensus import ConsensusMixin
from .core import (CLAIM_TTL, CONSENSUS_RULES, DEFAULT_QUORUM, DOC_KINDS, INBOX_DEFAULT, MEMORY_KINDS,  # noqa: F401
                   MESSAGE_KINDS, MSG_MAX, MSG_RECOMMENDED, PREFIX, ROLES, SCHEMA, SESSION_TTL, STANCES,
                   TASK_STATUSES, CoordBase, CoordError, iso, parse_id)
from .documents import DocumentsMixin
from .memory import MemoryMixin
from .messages import MessagesMixin
from .routing import RoutingMixin
from .sessions import SessionsMixin
from .tasks import TasksMixin


class Coord(SessionsMixin, MessagesMixin, ClaimsMixin, ConsensusMixin, DocumentsMixin, TasksMixin, MemoryMixin, RoutingMixin, CoordBase):
    """The coordination service: one SQLite file, every mutation under BEGIN IMMEDIATE."""


READ_OPS = {"inbox", "thread", "locks", "fence_check", "roles", "check", "discussion", "discussions", "task_get",
            "doc_show", "doc_history", "docs", "tasks", "memory", "suggest", "context", "projects",
            "status", "events", "presence"}
WRITE_OPS = {"whoami", "heartbeat", "end", "post", "reply", "resolve", "poll", "claim", "renew", "release",
             "grant", "revoke", "ask", "post_commit", "discuss", "propose", "react", "decide", "doc_create",
             "doc_edit", "doc_patch", "task_create", "task_accept", "task_done", "task_cancel", "task_decline", "role_accept", "role_decline", "memory_add", "memory_edit",
             "profile_set"}
