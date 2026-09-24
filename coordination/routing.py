"""Agent profiles and routing hints; overview views (context, status, events)."""

import json

from .core import iso


class RoutingMixin:
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
                "strategy": [m for m in mem if m["kind"] == "strategy"],   # in full: every agent follows it
                "overview": [m for m in mem if m["kind"] == "overview"],
                "memory": [{"memory": m["memory"], "kind": m["kind"], "title": m["title"]}
                           for m in mem if m["kind"] not in ("strategy", "overview")][:10],
                "routines": self.routines(project=project, due=True),
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
            q, a = "SELECT * FROM events WHERE event_id>?", [int(after)]
            if project:
                q += " AND project_id=?"; a.append(project)
            rows = db.execute(q + f" ORDER BY event_id LIMIT {int(limit)}", a).fetchall()
        return [{"event": r["event_id"], "kind": r["kind"], "entity": r["entity_type"], "id": r["entity_id"],
                 "payload": json.loads(r["payload_json"]), "at": iso(r["created_at"])} for r in rows]
