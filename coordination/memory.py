"""Project memory: versioned, attributed entries."""

from .core import MEMORY_KINDS, CoordBase, CoordError, iso, parse_id


class MemoryMixin(CoordBase):
    def memory_add(self, session: str, kind: str, title: str, content: str, source: str = "",
                   client_id: str | None = None) -> dict:
        if kind not in MEMORY_KINDS:
            raise CoordError("bad_kind", f"memory kind must be one of {', '.join(MEMORY_KINDS)}")

        def fn(db):
            me, _ = self._access(db, session, None, "participate")
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
            mid = parse_id("memory", memory)
            m = db.execute("SELECT * FROM project_memory WHERE memory_id=?", (mid,)).fetchone()
            if m is None:
                raise CoordError("missing", f"no memory M{mid}")
            me, _ = self._access(db, session, m["project_id"], "participate")
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
               include_archived: bool = False, session: str | None = None) -> list[dict]:
        with self._read() as db:
            q, a = "SELECT * FROM project_memory WHERE 1=1", []
            if project:
                self._view(db, project, session)
                q += " AND project_id=?"; a.append(project)
            else:
                f, fargs = self._view_filter(db, session)
                q += f" AND {f}"; a += list(fargs)
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
