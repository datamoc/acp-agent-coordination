"""Sessions: whoami, heartbeat, end, presence; identity binding."""

import re
import uuid

from .core import FEATURES, SESSION_TTL, CoordError, iso, server_version


class SessionsMixin:
    def whoami(self, family: str, project: str = "default", principal: str | None = None,
               user: str | None = None, model: str | None = None, client_id: str | None = None) -> dict:
        """Take a session. With `user` and/or `model` the session is the association user + CLI + model,
        named `user/family/model` (michel/claude/sonnet), and a whoami for the same association (same
        project and identity) resumes its live session instead of opening another. Without them the
        name is `family-NN` and every call opens a new session, as before."""
        family = (family or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", family):
            raise CoordError("bad_family", "family must be 1-40 letters/digits/_/-")
        if re.fullmatch(r".+-\d+", family):                   # "codex-01" is a session name, not a family
            base = re.sub(r"-\d+$", "", family)
            raise CoordError("bad_family", f"{family!r} looks like a session name: join with your agent's family "
                             f"({base!r}), whoami gives you a new name", {"family": base})
        project = project or "default"
        tidy = lambda v, n: re.sub(r"[^a-z0-9_.-]", "-", v.strip().lower())[:n].strip("-") or None if v else None
        user, model = tidy(user, 32), tidy(model, 40)

        def fn(db):
            now = self.clock()
            self._reap(db)
            if "/" not in project and project != "default":   # a bare name that is the tail of a known repo
                known = [r[0] for r in db.execute("SELECT project_id FROM repos WHERE lower(project_id) LIKE ?",
                                                  (f"%/{project.lower()}",))]
                if known:
                    raise CoordError("unknown_project", f"no project {project!r}: did you mean {known[0]!r}? The client "
                                     "reads it from the git remote - don't pass --project; if git refuses the checkout, "
                                     "tell the human", {"did_you_mean": known})
            db.execute("INSERT OR IGNORE INTO repos VALUES(?,?,?)",
                       (project, project.split("/")[0] if "/" in project else None, now))
            if user or model:
                same = db.execute(
                    "SELECT * FROM sessions WHERE project_id=? AND family=? AND ended_at IS NULL AND heartbeat_at>=?"
                    " AND principal IS ? AND user IS ? AND model IS ? ORDER BY heartbeat_at DESC LIMIT 1",
                    (project, family, now - SESSION_TTL, principal, user, model)).fetchone()
                if same is not None:                              # the same association: resume it
                    db.execute("UPDATE sessions SET heartbeat_at=? WHERE session_id=?", (now, same["session_id"]))
                    self._event(db, project, "session.resumed", same["session_id"], "session", same["session_id"])
                    return {"session_id": same["session_id"], "name": same["display_name"],
                            "generation": same["generation"], "project": project, "resumed": True,
                            "server": {"version": server_version(), "features": list(FEATURES)}}
                base = "/".join(x for x in (user, family, model) if x)
                live = {r[0] for r in db.execute("SELECT display_name FROM sessions WHERE ended_at IS NULL AND"
                                                 " heartbeat_at>=?", (now - SESSION_TTL,))}
                name = base if base not in live else next(f"{base}#{i}" for i in range(2, 1000) if f"{base}#{i}" not in live)
            else:
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
                       " principal, status, started_at, heartbeat_at, cursor, user, model) VALUES(?,?,?,?,?,?,?,?,?,"
                       " (SELECT COALESCE(MAX(id),0) FROM messages),?,?)",
                       (sid, name, family, gen, project, principal, "", now, now, user, model))
            self._event(db, project, "session.started", sid, "session", sid, name=name, generation=gen)
            return {"session_id": sid, "name": name, "generation": gen, "project": project,
                    "server": {"version": server_version(), "features": list(FEATURES)}}
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
                     "project": r["project_id"], "live": self._live(r), "seen": iso(r["heartbeat_at"]),
                     "user": r["user"], "family": r["family"], "model": r["model"]}
                    for r in rows if include_dead or self._live(r)]
