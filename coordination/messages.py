"""Messages - the unified chat: post, reply, inbox, threads, resolution, poll; recipients, priorities,
receipts (delivered / read / taken / answered / done), links to tasks, documents and discussions,
and contact policies."""

import json

from .core import (
    ACK_STATES,
    CONTACT_POLICIES,
    INBOX_DEFAULT,
    MESSAGE_KINDS,
    MESSAGE_PRIORITIES,
    MSG_MAX,
    MSG_RECOMMENDED,
    CoordBase,
    CoordError,
    iso,
    parse_id,
)

PENDING = ("sent", "delivered", "read")      # a receipt in one of these still waits for the recipient


class MessagesMixin(CoordBase):
    @staticmethod
    def _msg_dict(r) -> dict:
        d = {"id": r["id"], "from": r["from_name"], "to": r["to_name"], "kind": r["kind"],
             "body": r["body"], "at": iso(r["created_at"]), "thread": r["thread_id"],
             "reply_to": r["reply_to"], "project": r["project_id"]}
        if r["priority"] and r["priority"] != "normal":
            d["priority"] = r["priority"]
        if r["claim_id"]:
            d["claim"] = f"C{r['claim_id']}"
        for col, key, prefix in (("task_id", "task", "T"), ("document_id", "document", "DOC"),
                                 ("discussion_id", "discussion", "D")):
            if r[col]:
                d[key] = f"{prefix}{r[col]}"
        if r["resolved_at"]:
            d.update(resolved_at=iso(r["resolved_at"]), resolved_by=r["resolved_by"],
                     resolution=r["resolution"])
        return d

    # --- contact policies -----------------------------------------------------------
    def _policy(self, db, project: str, name: str) -> str:
        r = db.execute("SELECT policy FROM contact_policies WHERE project_id=? AND name=?", (project, name)).fetchone()
        return r["policy"] if r else "open"

    def _may_contact(self, db, project: str, sender, to_row, replying: bool) -> str | None:
        """None when `sender` may write to `to_row` directly, else why not (a handshake is requested)."""
        name, peer = to_row["display_name"], sender["display_name"]
        policy = self._policy(db, project, name)
        if replying or policy == "open":
            return None
        if policy == "auto":
            db.execute("INSERT OR IGNORE INTO contacts VALUES(?,?,?,?,?)", (project, name, peer, "accepted", self.clock()))
            return None
        c = db.execute("SELECT status FROM contacts WHERE project_id=? AND name=? AND peer=?",
                       (project, name, peer)).fetchone()
        if policy == "block_all":
            return f"{name} accepts no direct message (block_all): write to the project instead"
        if c is not None and c["status"] == "accepted":
            return None
        if c is not None and c["status"] == "blocked":
            return f"{name} does not accept direct messages from {peer}"
        if c is None:                                 # the handshake: one request, then the person decides
            self._contact_request(db, project, sender, to_row)
        return f"{name} accepts direct messages from contacts only; a contact request was sent to {name}"

    def _contact_request(self, db, project: str, sender, to_row) -> None:
        db.execute("INSERT OR IGNORE INTO contacts VALUES(?,?,?,?,?)",
                   (project, to_row["display_name"], sender["display_name"], "requested", self.clock()))
        self._notify(db, sender, to_row["session_id"], f"{sender['display_name']} asks to write to you directly - "
                     f"coord contact accept {sender['display_name']} (or block {sender['display_name']})", kind="question")

    def contact_policy(self, session: str, policy: str) -> dict:
        """Who may write to you directly in this project: open (anyone), auto (anyone, recorded as a
        contact), contacts_only (a first message becomes a request you accept), block_all (nobody -
        broadcasts, task offers and invitations still reach you)."""
        if policy not in CONTACT_POLICIES:
            raise CoordError("bad_policy", f"policy must be one of {', '.join(CONTACT_POLICIES)}")
        with self._tx() as db:
            me, project = self._access(db, session, None, "view")
            db.execute("INSERT INTO contact_policies VALUES(?,?,?,?) ON CONFLICT(project_id, name) DO UPDATE"
                       " SET policy=excluded.policy, set_at=excluded.set_at",
                       (project, me["display_name"], policy, self.clock()))
            self._event(db, project, "contact.policy", session, "member", me["display_name"], policy=policy)
            return {"name": me["display_name"], "project": project, "policy": policy}

    def contact(self, session: str, peer: str, action: str = "accept") -> dict:
        """Answer a contact request, or decide ahead: accept, block or remove `peer`."""
        if action not in ("accept", "block", "remove"):
            raise CoordError("bad_args", "action must be accept, block or remove")
        with self._tx() as db:
            me, project = self._access(db, session, None, "view")
            if action == "remove":
                db.execute("DELETE FROM contacts WHERE project_id=? AND name=? AND peer=?", (project, me["display_name"], peer))
            else:
                db.execute("INSERT INTO contacts VALUES(?,?,?,?,?) ON CONFLICT(project_id, name, peer) DO UPDATE"
                           " SET status=excluded.status, created_at=excluded.created_at",
                           (project, me["display_name"], peer, "accepted" if action == "accept" else "blocked",
                            self.clock()))
            self._event(db, project, f"contact.{action}", session, "member", peer)
            return {"name": me["display_name"], "peer": peer, "action": action}

    def contacts(self, session: str) -> dict:
        """Your contact policy and contacts in this project (requests waiting for you included)."""
        with self._read() as db:
            me, project = self._access(db, session, None, "view")
            rows = db.execute("SELECT peer, status FROM contacts WHERE project_id=? AND name=? ORDER BY peer",
                              (project, me["display_name"])).fetchall()
            return {"name": me["display_name"], "policy": self._policy(db, project, me["display_name"]),
                    "contacts": [{"peer": r["peer"], "status": r["status"]} for r in rows]}

    # --- posting ------------------------------------------------------------------
    def _group(self, db, project: str, group: str, exclude: str) -> list:
        """Live sessions of `project` whose profile declares `group` as a capability or category."""
        rows = db.execute("SELECT s.*, p.category, p.capabilities_json FROM sessions s LEFT JOIN agent_profiles p"
                          " ON p.session_id=s.session_id WHERE s.project_id=?", (project,)).fetchall()
        return [r for r in rows if self._live(r) and r["session_id"] != exclude
                and (r["category"] == group or group in json.loads(r["capabilities_json"] or "[]"))]

    def _link_ids(self, db, project: str, task, document, discussion) -> tuple:
        out = []
        for kind, table, col, value in (("task", "tasks", "task_id", task), ("document", "documents", "id", document),
                                        ("discussion", "discussions", "id", discussion)):
            if not value:
                out.append(None)
                continue
            i = parse_id(kind, value)
            row = db.execute(f"SELECT project_id FROM {table} WHERE {col}=?", (i,)).fetchone()
            if row is None:
                raise CoordError("missing", f"no {kind} {value}")
            if row["project_id"] != project:
                raise CoordError("bad_args", f"{value} is in another project")
            out.append(i)
        return tuple(out)

    def post(self, session: str, body: str, kind: str = "info", to: str | None = None,
             reply_to: int | None = None, claim: str | None = None, priority: str = "normal",
             to_group: str | None = None, task: str | None = None, document: str | None = None,
             discussion: str | None = None, client_id: str | None = None) -> dict:
        """Post to the project, to one or several sessions (`to`: "a,b"), or to a group (`to_group`: the
        live sessions declaring that capability or category). Directed messages are seen by their
        recipients only and carry a receipt each; high/urgent ones sent to the whole project also
        ask every live session for an acknowledgement. Priority sets attention, not authority."""
        body = (body or "").strip()
        if not body:
            raise CoordError("empty", "empty message")
        if len(body) > MSG_MAX:
            raise CoordError("too_long", f"message is {len(body)} chars; the limit is {MSG_MAX} "
                             "- put long content in a document (`coord doc create`)")
        if kind not in MESSAGE_KINDS:
            raise CoordError("bad_kind", f"kind must be one of {', '.join(MESSAGE_KINDS)}")
        if priority not in MESSAGE_PRIORITIES:
            raise CoordError("bad_priority", f"priority must be one of {', '.join(MESSAGE_PRIORITIES)}")

        def fn(db):
            me, project = self._access(db, session, None, "participate")
            thread, parent = None, None
            if reply_to is not None:
                parent = db.execute("SELECT * FROM messages WHERE id=?", (int(reply_to),)).fetchone()
                if parent is None:
                    raise CoordError("missing", f"no message #{reply_to}")
                thread = parent["thread_id"] or parent["id"]
            names = [n.strip() for n in (to or "").split(",") if n.strip()]
            targets = [self._resolve_name(db, n, project) for n in dict.fromkeys(names)]
            if to_group:
                grp = self._group(db, project, to_group, session)
                if not grp:
                    raise CoordError("unknown_recipient", f"no live session in group {to_group!r} "
                                     "(a capability or category from `coord profile`)")
                targets += [r for r in grp if r["session_id"] not in {t["session_id"] for t in targets}]
            held = []
            for t in list(targets):
                why = self._may_contact(db, project, me, t, replying=parent is not None and
                                        parent["from_session_id"] == t["session_id"])
                if why:
                    if len(targets) == 1 and not to_group:     # refused: the request survives the rollback below
                        raise CoordError("contact_required", why, {"to": t["display_name"],
                                                                   "handshake": "contact request" in why})
                    held.append({"to": t["display_name"], "why": why})
                    targets.remove(t)
            if (names or to_group) and not targets:
                raise CoordError("contact_required", "; ".join(h["why"] for h in held), {"held": held})
            single = targets[0] if len(targets) == 1 and not to_group and len(names) == 1 else None
            listed = bool(targets) and single is None
            claim_id = parse_id("claim", claim) if claim else None
            tid, did, dsid = self._link_ids(db, project, task, document, discussion)
            now = self.clock()
            cur = db.execute(
                "INSERT INTO messages(project_id, from_session_id, from_name, to_session_id, to_name,"
                " thread_id, reply_to, kind, body, claim_id, created_at, priority, listed, task_id, document_id,"
                " discussion_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (project, session, me["display_name"], single["session_id"] if single else None,
                 single["display_name"] if single else (", ".join(t["display_name"] for t in targets) or None)
                 if not to_group else f"@{to_group}", thread,
                 int(reply_to) if reply_to is not None else None, kind, body, claim_id, now,
                 priority, int(listed), tid, did, dsid))
            mid = cur.lastrowid
            if thread is None:
                db.execute("UPDATE messages SET thread_id=? WHERE id=?", (mid, mid))
            receipts = targets
            if not targets and priority in ("high", "urgent"):     # the whole project is asked to look
                receipts = [r for r in db.execute("SELECT * FROM sessions WHERE project_id=?", (project,)).fetchall()
                            if self._live(r) and r["session_id"] != session]
            for t in receipts:
                db.execute("INSERT OR IGNORE INTO message_recipients(message_id, session_id, name, state)"
                           " VALUES(?,?,?, 'sent')", (mid, t["session_id"], t["display_name"]))
            if parent is not None:                   # a reply answers what was asked of me in the parent
                db.execute("UPDATE message_recipients SET answered_at=COALESCE(answered_at, ?),"
                           " state=CASE WHEN state IN ('done','declined') THEN state ELSE 'answered' END"
                           " WHERE message_id=? AND (session_id=? OR name=?)",
                           (now, parent["id"], session, me["display_name"]))
            self._event(db, project, "message.posted", session, "message", mid, kind=kind,
                        **({"priority": priority} if priority != "normal" else {}),
                        **({"to": [t["display_name"] for t in targets]} if targets else {}))
            out = {"id": mid, "thread": thread or mid,
                   "to": single["display_name"] if single else ([t["display_name"] for t in targets] or None)}
            if receipts:
                out["receipts"] = len(receipts)
            if held:
                out["held"] = held
            if len(body) > MSG_RECOMMENDED:
                out["warning"] = (f"{len(body)} chars (> {MSG_RECOMMENDED} recommended); "
                                  "consider a document for long analyses")
            return out
        try:
            return self._mutate("post", client_id, fn)
        except CoordError as e:
            if e.code == "contact_required" and e.data.get("handshake"):
                with self._tx() as db:
                    me, project = self._access(db, session, None, "participate")
                    to_row = self._resolve_name(db, e.data["to"], project)
                    self._contact_request(db, project, me, to_row)
            raise

    # --- receipts -------------------------------------------------------------------
    def ack(self, session: str, message: int, state: str = "taken", note: str = "") -> dict:
        """Say what you did with a message addressed to you: read, taken (you are on it), done, or
        declined (with the reason - it goes back to the sender). A reply marks it answered by itself."""
        if state not in ACK_STATES:
            raise CoordError("bad_state", f"state must be one of {', '.join(ACK_STATES)}")
        if state == "declined" and not note.strip():
            raise CoordError("comment_required", "declining needs its reason: coord ack <id> declined \"why\"")
        with self._tx() as db:
            m = db.execute("SELECT * FROM messages WHERE id=?", (int(message),)).fetchone()
            if m is None:
                raise CoordError("missing", f"no message #{message}")
            me, project = self._access(db, session, m["project_id"], "view")
            r = db.execute("SELECT * FROM message_recipients WHERE message_id=? AND (session_id=? OR name=?)"
                           " ORDER BY session_id=? DESC LIMIT 1",
                           (m["id"], session, me["display_name"], session)).fetchone()
            if r is None:
                raise CoordError("forbidden", f"#{m['id']} is not addressed to you")
            now = self.clock()
            col = {"read": "read_at", "taken": "taken_at", "done": "done_at", "declined": "done_at"}[state]
            db.execute(f"UPDATE message_recipients SET state=?, {col}=COALESCE({col}, ?),"
                       " read_at=COALESCE(read_at, ?), delivered_at=COALESCE(delivered_at, ?),"
                       " note=CASE WHEN ?!='' THEN ? ELSE note END WHERE message_id=? AND session_id=?",
                       (state, now, now, now, note, note, m["id"], r["session_id"]))
            if state in ("declined", "done"):
                self._notify(db, me, m["from_session_id"],
                             f"[#{m['id']}] {me['display_name']} {state}" + (f": {note}" if note else ""),
                             kind="warning" if state == "declined" else "done")
            self._event(db, project, f"message.{state}", session, "message", m["id"])
            return {"id": m["id"], "state": state, "by": me["display_name"]}

    def receipts(self, message: int, session: str | None = None) -> dict:
        """Who a message was addressed to and what each recipient did with it."""
        with self._read() as db:
            m = db.execute("SELECT * FROM messages WHERE id=?", (int(message),)).fetchone()
            if m is None:
                raise CoordError("missing", f"no message #{message}")
            self._view(db, m["project_id"], session)
            vis, vargs = self._visible(session)
            if not db.execute(f"SELECT 1 FROM messages WHERE id=? AND {vis}", (m["id"], *vargs)).fetchone():
                raise CoordError("forbidden", f"#{m['id']} is not addressed to you")
            rows = db.execute("SELECT * FROM message_recipients WHERE message_id=? ORDER BY name", (m["id"],)).fetchall()
        return {"id": m["id"], "from": m["from_name"], "priority": m["priority"], "kind": m["kind"],
                "recipients": [{"name": r["name"], "state": r["state"], "delivered_at": iso(r["delivered_at"]),
                                "read_at": iso(r["read_at"]), "taken_at": iso(r["taken_at"]),
                                "answered_at": iso(r["answered_at"]), "done_at": iso(r["done_at"]),
                                "note": r["note"]} for r in rows]}

    def _awaiting(self, db, session: str, name: str) -> list[dict]:
        """Messages addressed to this session (or its name) that it has not taken, answered or closed."""
        rows = db.execute("SELECT m.*, r.state AS rstate FROM message_recipients r JOIN messages m ON m.id=r.message_id"
                          f" WHERE (r.session_id=? OR r.name=?) AND r.state IN ({','.join('?' * len(PENDING))})"
                          " AND m.resolved_at IS NULL ORDER BY m.id", (session, name, *PENDING)).fetchall()
        return [self._msg_dict(r) | {"state": r["rstate"]} for r in rows]

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
            return ("((to_session_id IS NULL AND listed=0) OR to_session_id=? OR from_session_id=? OR (listed=1 AND"
                    " EXISTS(SELECT 1 FROM message_recipients r WHERE r.message_id=messages.id AND r.session_id=?)))",
                    (session, session, session))
        return ("(to_session_id IS NULL AND listed=0)", ())

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
                self._view(db, project, session)
                where.append("project_id=?"); args.append(project)
            else:                                   # across projects: only the ones this caller may view
                f, fargs = self._view_filter(db, session)
                where.append(f); args += fargs
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
            m = db.execute("SELECT thread_id, project_id FROM messages WHERE id=?", (int(message),)).fetchone()
            if m is None:
                raise CoordError("missing", f"no message #{message}")
            self._view(db, m["project_id"], session)
            vis, vargs = self._visible(session)
            rows = db.execute(f"SELECT * FROM messages WHERE thread_id=? AND {vis} ORDER BY id",
                              (m["thread_id"], *vargs)).fetchall()
        return [self._msg_dict(r) for r in rows]

    def resolve(self, session: str, message: int, resolution: str = "",
                client_id: str | None = None) -> dict:
        def fn(db):
            m = db.execute("SELECT * FROM messages WHERE id=?", (int(message),)).fetchone()
            if m is None:
                raise CoordError("missing", f"no message #{message}")
            me, project = self._access(db, session, m["project_id"], "participate")
            if m["resolved_at"]:
                return {"id": m["id"], "outcome": "already", "resolved_by": m["resolved_by"]}
            db.execute("UPDATE messages SET resolved_at=?, resolved_by=?, resolution=? WHERE id=?",
                       (self.clock(), me["display_name"], resolution, m["id"]))
            self._event(db, project, "message.resolved", session, "message", m["id"])
            return {"id": m["id"], "outcome": "ok", "resolved_by": me["display_name"]}
        return self._mutate("resolve", client_id, fn)

    def poll(self, session: str) -> dict:
        """Everything new since this session's cursor (by message id, never time)."""
        with self._tx() as db:
            me, _ = self._access(db, session, None, "view")
            vis, vargs = self._visible(session)
            rows = db.execute(f"SELECT * FROM messages WHERE id>? AND project_id=? AND {vis}"
                              " ORDER BY id", (me["cursor"], me["project_id"], *vargs)).fetchall()
            if rows:
                db.execute("UPDATE sessions SET cursor=? WHERE session_id=?", (rows[-1]["id"], session))
                db.execute("UPDATE message_recipients SET delivered_at=?, state=CASE WHEN state='sent' THEN"
                           " 'delivered' ELSE state END WHERE session_id=? AND delivered_at IS NULL AND message_id<=?",
                           (self.clock(), session, rows[-1]["id"]))
            db.execute("UPDATE sessions SET heartbeat_at=? WHERE session_id=?", (self.clock(), session))
            self._awake(db, me)
            project = me["project_id"]
            awaiting = self._awaiting(db, session, me["display_name"])
        return {"messages": [self._msg_dict(r) for r in rows],
                "my_claims": self.locks(project=project, owner_session=session, session=session),
                "tasks": [t for t in self.tasks(project=project, status="open", session=session)]
                + self.tasks(project=project, status="offered", assigned_session=session, session=session)
                + self.tasks(project=project, status="accepted", assigned_session=session, session=session),
                "discussions": self.discussions(project=project, session=session),
                "routines": self.routines(project=project, due=True, session=session),
                "awaiting": awaiting, "wake_requests": self.wake_requests(project=project, target=me["display_name"],
                                                                            open_only=True, session=session),
                "wake": self.wake(session)}
