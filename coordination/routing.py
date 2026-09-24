"""Agent profiles and routing hints; overview views (context, status, events)."""

import json
from datetime import datetime

from .core import CLAIM_RENEW_MARGIN, WAKE_KEEPALIVE, WAKE_UNANSWERED, CoordBase, iso


class RoutingMixin(CoordBase):
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
    def wake(self, session: str) -> dict:
        """When this session should look again - the server cannot wake anyone, but it knows when there
        will be something to do. Agents that can schedule themselves (a loop, a cron, a wake-up at quota
        reset) use `next_at`; the keep-alive default stays under the session TTL."""
        now = self.clock()
        with self._read() as db:
            me = self._session(db, session)
            project = me["project_id"]
            hints = [(me["heartbeat_at"] + WAKE_KEEPALIVE, "keep the session alive (poll)")]
            offered = db.execute("SELECT task_id, title FROM tasks WHERE assigned_to=? AND status='offered'",
                                 (session,)).fetchall()
            hints += [(now, f"T{t['task_id']} offered to you: accept or decline") for t in offered]
            for c in db.execute("SELECT claim_id, scope, expires_at FROM claims WHERE owner_session_id=? AND"
                                " released_at IS NULL AND expires_at>?", (session, now)).fetchall():
                hints.append((c["expires_at"] - CLAIM_RENEW_MARGIN, f"renew or release C{c['claim_id']}"))
            for d in db.execute("SELECT d.id, d.topic, d.deadline FROM discussions d WHERE d.status='open' AND"
                                " d.deadline IS NOT NULL AND d.deadline>? AND d.project_id=? AND (d.created_by=? OR"
                                " EXISTS(SELECT 1 FROM discussion_participants p WHERE p.discussion_id=d.id AND"
                                " p.session_id=?))", (now, project, session, session)).fetchall():
                hints.append((d["deadline"], f"D{d['id']} deadline: {d['topic']}"))
            # Broadcast questions/warnings only: a directed one (task offer, discussion invite, `ask`) already
            # has its own hint (offered task, discussion deadline) and its own resolution path, not `resolve`.
            for q in db.execute("SELECT id, kind, from_name, created_at FROM messages WHERE project_id=? AND"
                                " resolved_at IS NULL AND kind IN ('question','warning') AND to_session_id IS NULL"
                                " AND from_session_id!=? AND created_at<=? AND NOT EXISTS(SELECT 1 FROM discussions"
                                " WHERE message_id=messages.id)",
                                (project, session, now - WAKE_UNANSWERED)).fetchall():
                hints.append((now, f"M{q['id']} unresolved {q['kind']} from {q['from_name']}: reply or `coord resolve {q['id']}`"))
        for r in self.routines(project=project):
            if r["status"] != "active" or r["running"]:
                continue
            if r["due"]:
                hints.append((now, f"{r['routine']} due: {r['title']}"))
            elif r["next_due"]:
                hints.append((datetime.fromisoformat(r["next_due"]).timestamp(), f"{r['routine']} due: {r['title']}"))
        hints.sort(key=lambda h: h[0])
        at, reason = hints[0]
        at = max(at, now)
        return {"next_at": iso(at), "in_seconds": int(at - now), "reason": reason,
                "upcoming": [{"at": iso(max(t, now)), "reason": why} for t, why in hints[1:5]]}

    def context(self, session: str) -> dict:
        with self._read() as db:
            me = self._session(db, session)
            unread = db.execute("SELECT COUNT(*) FROM messages WHERE id>? AND project_id=? AND"
                                " (to_session_id IS NULL OR to_session_id=?)",
                                (me["cursor"], me["project_id"], session)).fetchone()[0]
        project = me["project_id"]
        mem = self.memory(project=project)
        return {"me": {"name": me["display_name"], "generation": me["generation"], "project": project},
                "strategy": [m for m in mem if m["kind"] == "strategy"],   # in full: every agent follows it
                "overview": [m for m in mem if m["kind"] == "overview"],
                "memory": [{"memory": m["memory"], "kind": m["kind"], "title": m["title"]}
                           for m in mem if m["kind"] not in ("strategy", "overview")][:10],
                "routines": self.routines(project=project, due=True),
                "wake": self.wake(session),
                "open_tasks": self.tasks(project=project, status="open")[:10],
                "my_tasks": self.tasks(project=project, status="offered", assigned_session=session)
                + self.tasks(project=project, status="accepted", assigned_session=session),
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
                "open_discussions": len(self.discussions(project)),
                "due_routines": len(self.routines(project, due=True))}

    def events(self, after: int = 0, project: str | None = None, limit: int = 100) -> list[dict]:
        with self._read() as db:
            q: str = "SELECT * FROM events WHERE event_id>?"
            a: list = [int(after)]
            if project:
                q += " AND project_id=?"; a.append(project)
            rows = db.execute(q + f" ORDER BY event_id LIMIT {int(limit)}", a).fetchall()
        return [{"event": r["event_id"], "kind": r["kind"], "entity": r["entity_type"], "id": r["entity_id"],
                 "payload": json.loads(r["payload_json"]), "at": iso(r["created_at"])} for r in rows]
