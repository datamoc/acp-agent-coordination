"""Sessions: whoami, heartbeat, end, presence; identity binding."""

import re
import uuid

from .core import CoordError, iso


class SessionsMixin:
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
