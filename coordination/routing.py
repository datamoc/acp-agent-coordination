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
                exclude_session: str | None = None, session: str | None = None) -> list[dict]:
        """Rank live sessions by declared profile. Hints only: a human/agent chooses."""
        need = set(capability or [])
        with self._read() as db:
            if project:
                self._view(db, project, session)
            restricted = {x[0] for x in db.execute("SELECT DISTINCT project_id FROM project_members")}
            mine: set[str] = set()
            if session and restricted:
                me = self._session(db, session)
                mine = {x[0] for x in db.execute("SELECT project_id FROM project_members WHERE name=?",
                                                 (me["display_name"],))}
            rows = db.execute("SELECT s.*, p.provider, p.model_id, p.category, p.reasoning_level,"
                              " p.capabilities_json FROM sessions s LEFT JOIN agent_profiles p"
                              " ON p.session_id=s.session_id").fetchall()
        out = []
        for r in rows:
            if not self._live(r) or r["session_id"] == exclude_session:
                continue
            if project and r["project_id"] != project:
                continue
            if r["project_id"] in restricted and r["project_id"] not in mine:
                continue                                  # a restricted project is not a talent pool
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
            me, project = self._access(db, session, None, "view")
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
            for m in self._awaiting(db, session, me["display_name"]):
                if m.get("priority") in ("high", "urgent"):
                    hints.append((now, f"#{m['id']} {m['priority']} from {m['from']}: coord ack {m['id']} taken|done, or reply"))
            for w in db.execute("SELECT id, reason, ref FROM wake_requests WHERE project_id=? AND target_name=? AND"
                                " status IN ('requested','delivered','woken')", (project, me["display_name"])).fetchall():
                hints.append((now, f"W{w['id']} asks you to resume ({w['reason']} {w['ref'] or ''}): "
                                   f"coord wake answer W{w['id']} accept|refuse"))
            for q in db.execute("SELECT id, kind, from_name, created_at FROM messages WHERE project_id=? AND"
                                " resolved_at IS NULL AND kind IN ('question','warning') AND to_session_id IS NULL AND listed=0"
                                " AND from_session_id!=? AND created_at<=? AND NOT EXISTS(SELECT 1 FROM discussions"
                                " WHERE message_id=messages.id)",
                                (project, session, now - WAKE_UNANSWERED)).fetchall():
                hints.append((now, f"M{q['id']} unresolved {q['kind']} from {q['from_name']}: reply or `coord resolve {q['id']}`"))
        for r in self.routines(project=project, session=session):
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
            me, project = self._access(db, session, None, "view")
            vis, vargs = self._visible(session)
            unread = db.execute(f"SELECT COUNT(*) FROM messages WHERE id>? AND project_id=? AND {vis}",
                                (me["cursor"], me["project_id"], *vargs)).fetchone()[0]
            awaiting = self._awaiting(db, session, me["display_name"])
        mem = self.memory(project=project, session=session)
        return {"me": {"name": me["display_name"], "generation": me["generation"], "project": project},
                "strategy": [m for m in mem if m["kind"] == "strategy"],   # in full: every agent follows it
                "policies": [m for m in mem if m["kind"] == "policy"],     # organisational rules: not put to a vote
                "awaiting": awaiting,
                "wake_requests": self.wake_requests(project=project, target=me["display_name"], open_only=True,
                                                    session=session),
                "overview": [m for m in mem if m["kind"] == "overview"],
                "memory": [{"memory": m["memory"], "kind": m["kind"], "title": m["title"]}
                           for m in mem if m["kind"] not in ("strategy", "overview", "policy")][:10],
                "routines": self.routines(project=project, due=True, session=session),
                "wake": self.wake(session),
                "open_tasks": self.tasks(project=project, status="open", session=session)[:10],
                "my_tasks": self.tasks(project=project, status="offered", assigned_session=session, session=session)
                + self.tasks(project=project, status="accepted", assigned_session=session, session=session),
                "discussions": self.discussions(project=project, session=session),
                "my_claims": self.locks(project=project, owner_session=session, session=session),
                "unread": unread}

    def projects(self, session: str | None = None) -> list[dict]:
        """Every project this caller may see: open ones, plus the restricted ones it belongs to."""
        with self._read() as db:
            names = [r[0] for r in db.execute(
                "SELECT project_id FROM repos UNION SELECT project_id FROM sessions ORDER BY 1")]
            restricted = {x[0] for x in db.execute("SELECT DISTINCT project_id FROM project_members")}
            mine: set[str] = set()
            if session and restricted:
                me = self._session(db, session)
                mine = {x[0] for x in db.execute("SELECT project_id FROM project_members WHERE name=?",
                                                 (me["display_name"],))}
        return [self.status(p, session=session) for p in names
                if p not in restricted or p in mine]

    def status(self, project: str | None = None, session: str | None = None) -> dict:
        with self._read() as db:
            if project:
                self._view(db, project, session)
                f, a = (" WHERE project_id=?", (project,))
            else:                       # an aggregate: only the projects this caller may view
                filt, fargs = self._view_filter(db, session)
                f, a = (f" WHERE {filt}", list(fargs))
            msgs = db.execute("SELECT COUNT(*), COALESCE(SUM(resolved_at IS NULL AND kind IN"
                              " ('question','warning')),0) FROM messages" + f, a).fetchone()
        return {"project": project or "(all)", "messages": msgs[0], "open_questions": msgs[1],
                "live_sessions": len(self.presence(project, session=session)),
                "active_claims": len(self.locks(project, session=session)),
                "open_tasks": len(self.tasks(project, "open", session=session)),
                "open_discussions": len(self.discussions(project, session=session)),
                "due_routines": len(self.routines(project, due=True, session=session))}

    def events(self, after: int = 0, project: str | None = None, limit: int = 100,
               session: str | None = None) -> list[dict]:
        with self._read() as db:
            q: str = "SELECT * FROM events WHERE event_id>?"
            a: list = [int(after)]
            if project:
                self._view(db, project, session)
                q += " AND project_id=?"; a.append(project)
            else:
                f, fargs = self._view_filter(db, session)
                q += f" AND {f}"; a += list(fargs)
            rows = db.execute(q + f" ORDER BY event_id LIMIT {int(limit)}", a).fetchall()
        return [{"event": r["event_id"], "kind": r["kind"], "entity": r["entity_type"], "id": r["entity_id"],
                 "payload": json.loads(r["payload_json"]), "at": iso(r["created_at"])} for r in rows]
