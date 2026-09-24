"""Messages: post, reply, inbox, threads, resolution, poll."""

import time

from .core import MSG_RECOMMENDED, MSG_MAX, INBOX_DEFAULT, MESSAGE_KINDS, CoordError, iso, parse_id


class MessagesMixin:
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
                + self.tasks(project=project, status="offered", assigned_session=session)
                + self.tasks(project=project, status="accepted", assigned_session=session),
                "discussions": self.discussions(project=project),
                "routines": self.routines(project=project, due=True)}
