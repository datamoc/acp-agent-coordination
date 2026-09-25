"""Project membership: who may view, participate, decide or administer a project.

A project with no rows in `project_members` is open - everyone keeps exactly the rights
they had before this table existed. The first member of an open project can only be the
session naming itself (self-elevation to admin); from then on only admins grant or change
roles, and removing the last member reopens the project."""

from .core import PROJECT_ROLES, CoordBase, CoordError


class MembersMixin(CoordBase):
    def members(self, session: str | None = None, project: str | None = None) -> dict:
        """The roster of a project and whether it is restricted (default: the caller's project)."""
        with self._read() as db:
            if session:
                me = self._session(db, session)
                project = project or me["project_id"]
            if not project:
                raise CoordError("bad_args", "pass a session or a project")
            self._view(db, project, session)
            rows = db.execute("SELECT name, role, granted_by, created_at FROM project_members"
                              " WHERE project_id=? ORDER BY created_at, name", (project,)).fetchall()
            return {"project": project, "restricted": bool(rows),
                    "members": [{"name": r["name"], "role": r["role"], "granted_by": r["granted_by"]} for r in rows]}

    def member_set(self, session: str, name: str, role: str, project: str | None = None,
                   client_id: str | None = None) -> dict:
        """Grant or change a member's role (admin). In an open project the first member can only
        be the session itself; that admin then adds everyone else."""
        name = (name or "").strip()
        if not name:
            raise CoordError("bad_args", "a member needs a name (a display name like michel/claude/sonnet)")
        if role not in PROJECT_ROLES:
            raise CoordError("bad_role", f"role must be one of {', '.join(PROJECT_ROLES)}")

        def fn(db):
            me, p = self._access(db, session, project)      # resolves project, session must be live
            roster = self._roster(db, p)
            if not roster:
                if name != me["display_name"]:
                    raise CoordError("forbidden",
                                     "the first member of an open project can only be yourself "
                                     f"(`coord member set {me['display_name']} --role admin`); "
                                     "that admin then adds everyone else", {"project": p})
            else:
                self._access(db, session, p, "admin")
            db.execute("INSERT INTO project_members(project_id, name, role, granted_by, created_at)"
                       " VALUES(?,?,?,?,?) ON CONFLICT(project_id, name)"
                       " DO UPDATE SET role=excluded.role, granted_by=excluded.granted_by,"
                       " created_at=excluded.created_at",
                       (p, name, role, me["display_name"], self.clock()))
            self._event(db, p, "member.set", session, "member", name, role=role)
            return {"project": p, "name": name, "role": role, "restricted": True,
                    "members": len(self._roster(db, p))}
        return self._mutate("member_set", client_id, fn)

    def member_remove(self, session: str, name: str, project: str | None = None,
                      client_id: str | None = None) -> dict:
        """Remove a member (admin). Removing the last one reopens the project."""
        def fn(db):
            me, p = self._access(db, session, project, "admin")
            roster = self._roster(db, p)
            if not roster:
                raise CoordError("missing", f"project '{p}' has no members (it is open)")
            if not any(r["name"] == name for r in roster):
                raise CoordError("missing", f"{name} is not a member of '{p}'")
            db.execute("DELETE FROM project_members WHERE project_id=? AND name=?", (p, name))
            self._event(db, p, "member.removed", session, "member", name)
            left = len(self._roster(db, p))
            return {"project": p, "name": name, "removed": True, "members": left,
                    "restricted": bool(left)}
        return self._mutate("member_remove", client_id, fn)
