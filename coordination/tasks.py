"""Tasks and their A2A push-notification configs."""

import uuid

from .core import CoordError, iso, parse_id


RESOLVED = ("done", "cancelled")      # a prerequisite in one of these no longer blocks


class TasksMixin:
    # --- the task graph: T3 after T1, T2 ---------------------------------------
    def _deps(self, db, tid: int) -> list[int]:
        return [r[0] for r in db.execute("SELECT after_id FROM task_deps WHERE task_id=? ORDER BY after_id", (tid,))]

    def _blocked_by(self, db, tid: int) -> list[int]:
        return [r[0] for r in db.execute("SELECT d.after_id FROM task_deps d JOIN tasks t ON t.task_id=d.after_id"
                                         " WHERE d.task_id=? AND t.status NOT IN ('done','cancelled')"
                                         " ORDER BY d.after_id", (tid,))]

    def _link(self, db, project: str, tid: int, after: list[str]) -> list[int]:
        """Add prerequisites to a task: same project, no self-loop, no cycle."""
        added = []
        for a in after:
            aid = parse_id("task", a)
            row = db.execute("SELECT project_id FROM tasks WHERE task_id=?", (aid,)).fetchone()
            if row is None:
                raise CoordError("missing", f"no task T{aid}")
            if row["project_id"] != project:
                raise CoordError("bad_args", f"T{aid} is in another project")
            if aid == tid or tid in self._ancestors(db, aid):
                raise CoordError("cycle", f"T{tid} after T{aid} would make a cycle")
            db.execute("INSERT OR IGNORE INTO task_deps VALUES(?,?)", (tid, aid))
            added.append(aid)
        return added

    def _ancestors(self, db, tid: int) -> set[int]:
        seen, todo = set(), [tid]
        while todo:
            for dep in self._deps(db, todo.pop()):
                if dep not in seen:
                    seen.add(dep); todo.append(dep)
        return seen

    def _unblock_dependents(self, db, me, tid: int) -> None:
        """T{tid} just finished: tell whoever waits on a task that now has nothing left before it."""
        for (dep,) in db.execute("SELECT task_id FROM task_deps WHERE after_id=?", (tid,)).fetchall():
            t = db.execute("SELECT * FROM tasks WHERE task_id=?", (dep,)).fetchone()
            if t is None or t["status"] in RESOLVED or self._blocked_by(db, dep):
                continue
            target = t["assigned_to"] or (self._live_by_name(db, t["created_by"]) or {"session_id": None})["session_id"]
            self._notify(db, me, target, f"[T{dep}] unblocked - everything before it is finished: {t['title']}",
                         kind="info")
            self._event(db, t["project_id"], "task.unblocked", me["session_id"], "task", dep)

    def task_link(self, session: str, task: str, after: list[str], remove: bool = False) -> dict:
        """T3 after T1, T2: T3 cannot be accepted until they are done (or cancelled)."""
        with self._tx() as db:
            me = self._session(db, session)
            tid = parse_id("task", task)
            t = db.execute("SELECT * FROM tasks WHERE task_id=?", (tid,)).fetchone()
            if t is None:
                raise CoordError("missing", f"no task T{tid}")
            if remove:
                for a in after:
                    db.execute("DELETE FROM task_deps WHERE task_id=? AND after_id=?", (tid, parse_id("task", a)))
            else:
                self._link(db, t["project_id"], tid, after)
            self._event(db, t["project_id"], "task.linked", session, "task", tid)
            return {"task": f"T{tid}", "after": [f"T{x}" for x in self._deps(db, tid)],
                    "blocked_by": [f"T{x}" for x in self._blocked_by(db, tid)]}

    def task_create(self, session: str, title: str, description: str = "", priority: int = 0,
                    claim: str | None = None, assign: str | None = None, category: str | None = None,
                    after: list[str] | None = None, client_id: str | None = None) -> dict:
        def fn(db):
            me = self._session(db, session)
            target = self._resolve_name(db, assign, me["project_id"]) if assign else None
            now = self.clock()
            tid = db.execute("INSERT INTO tasks(project_id, created_by, assigned_to, assigned_name, title,"
                             " description, status, priority, related_claim_id, category, created_at,"
                             " updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                             (me["project_id"], me["display_name"], target["session_id"] if target else None,
                              target["display_name"] if target else None, title, description,
                              "offered" if target else "open", int(priority),
                              parse_id("claim", claim) if claim else None, category, now, now)).lastrowid
            self._link(db, me["project_id"], tid, after or [])
            if target:   # an offer, not an order: the assignee accepts or declines
                self._notify(db, me, target["session_id"],
                             f"[T{tid}] {me['display_name']} offers you a task: {title} - "
                             f"coord task accept T{tid} / coord task decline T{tid} \"why\"", kind="question")
            self._event(db, me["project_id"], "task.created", session, "task", tid)
            return {"task": f"T{tid}", "id": tid}
        return self._mutate("task_create", client_id, fn)

    def tasks(self, project: str | None = None, status: str | None = None,
              assigned_session: str | None = None) -> list[dict]:
        with self._read() as db:
            q, a = "SELECT * FROM tasks WHERE 1=1", []
            if project:
                q += " AND project_id=?"; a.append(project)
            if status:
                q += " AND status=?"; a.append(status)
            if assigned_session:
                q += " AND assigned_to=?"; a.append(assigned_session)
            rows = db.execute(q + " ORDER BY priority DESC, task_id", a).fetchall()
            graph = {r["task_id"]: (self._deps(db, r["task_id"]), self._blocked_by(db, r["task_id"])) for r in rows}
        return [{"task": f"T{r['task_id']}", "title": r["title"], "status": r["status"],
                 "priority": r["priority"], "assigned": r["assigned_name"], "created_by": r["created_by"],
                 "claim": f"C{r['related_claim_id']}" if r["related_claim_id"] else None,
                 "category": r["category"], "after": [f"T{x}" for x in graph[r["task_id"]][0]],
                 "blocked_by": [f"T{x}" for x in graph[r["task_id"]][1]]} for r in rows]

    def _task_update(self, session, task, status, note=None, require_assignee=False):
        with self._tx() as db:
            me = self._session(db, session)
            tid = parse_id("task", task)
            t = db.execute("SELECT * FROM tasks WHERE task_id=?", (tid,)).fetchone()
            if t is None:
                raise CoordError("missing", f"no task T{tid}")
            if status == "accepted":
                if t["status"] == "offered" and t["assigned_to"] != session:
                    raise CoordError("forbidden", f"T{tid} is offered to {t['assigned_name']}, not to you")
                if t["status"] not in ("open", "offered"):
                    raise CoordError("taken", f"T{tid} is {t['status']} ({t['assigned_name'] or '-'})")
                waiting = self._blocked_by(db, tid)
                if waiting:
                    raise CoordError("blocked", f"T{tid} waits for {', '.join(f'T{x}' for x in waiting)} - "
                                     "you are told when they are done", {"blocked_by": [f"T{x}" for x in waiting]})
            if require_assignee and t["assigned_to"] not in (None, session):
                raise CoordError("forbidden", f"T{tid} is assigned to {t['assigned_name']}")
            if status == "accepted" and t["status"] == "offered":
                creator = self._live_by_name(db, t["created_by"])
                self._notify(db, me, creator["session_id"] if creator else None,
                             f"[T{tid}] {me['display_name']} accepted: {t['title']}")
            db.execute("UPDATE tasks SET status=?, assigned_to=COALESCE(assigned_to, ?),"
                       " assigned_name=COALESCE(assigned_name, ?), note=COALESCE(?, note), updated_at=?"
                       " WHERE task_id=?", (status, session, me["display_name"], note, self.clock(), tid))
            self._event(db, t["project_id"], f"task.{status}", session, "task", tid)
            if status == "done":
                self._unblock_dependents(db, me, tid)
            return {"task": f"T{tid}", "status": status}

    def task_decline(self, session: str, task: str, reason: str = "") -> dict:
        """Turn down a task offered (or given back after accepting): it returns to open, unassigned."""
        with self._tx() as db:
            me = self._session(db, session)
            tid = parse_id("task", task)
            t = db.execute("SELECT * FROM tasks WHERE task_id=?", (tid,)).fetchone()
            if t is None:
                raise CoordError("missing", f"no task T{tid}")
            if t["assigned_to"] != session or t["status"] not in ("offered", "accepted"):
                raise CoordError("forbidden", f"T{tid} is not offered to or accepted by you ({t['status']})")
            db.execute("UPDATE tasks SET status='open', assigned_to=NULL, assigned_name=NULL,"
                       " note=?, updated_at=? WHERE task_id=?",
                       (f"declined by {me['display_name']}" + (f": {reason}" if reason else ""), self.clock(), tid))
            creator = self._live_by_name(db, t["created_by"])
            self._notify(db, me, creator["session_id"] if creator else None,
                         f"[T{tid}] {me['display_name']} declined: {t['title']}" + (f" - {reason}" if reason else ""),
                         kind="warning")
            self._event(db, t["project_id"], "task.declined", session, "task", tid)
            return {"task": f"T{tid}", "status": "open", "declined_by": me["display_name"]}

    def task_get(self, task: str) -> dict:
        """One task in full (the A2A GetTask view of it)."""
        tid = parse_id("task", task)
        with self._read() as db:
            r = db.execute("SELECT * FROM tasks WHERE task_id=?", (tid,)).fetchone()
            if r is None:
                raise CoordError("missing", f"no task T{tid}")
            after, blocked = self._deps(db, tid), self._blocked_by(db, tid)
            before = [x[0] for x in db.execute("SELECT task_id FROM task_deps WHERE after_id=? ORDER BY task_id", (tid,))]
        return {"task": f"T{tid}", "project": r["project_id"], "title": r["title"], "description": r["description"],
                "after": [f"T{x}" for x in after], "blocked_by": [f"T{x}" for x in blocked],
                "before": [f"T{x}" for x in before],
                "status": r["status"], "priority": r["priority"], "assigned": r["assigned_name"],
                "created_by": r["created_by"], "note": r["note"], "category": r["category"],
                "claim": f"C{r['related_claim_id']}" if r["related_claim_id"] else None,
                "created_at": iso(r["created_at"]), "updated_at": iso(r["updated_at"])}

    def task_cancel(self, session: str, task: str, note: str = "") -> dict:
        """The creator or the assignee withdraws a task that is not done yet."""
        with self._tx() as db:
            me = self._session(db, session)
            tid = parse_id("task", task)
            t = db.execute("SELECT * FROM tasks WHERE task_id=?", (tid,)).fetchone()
            if t is None:
                raise CoordError("missing", f"no task T{tid}")
            if t["status"] in ("done", "cancelled"):
                raise CoordError("not_cancelable", f"T{tid} is already {t['status']}")
            if me["display_name"] != t["created_by"] and t["assigned_to"] != session:
                raise CoordError("forbidden", f"only {t['created_by']} or the assignee can cancel T{tid}")
            db.execute("UPDATE tasks SET status='cancelled', note=COALESCE(NULLIF(?, ''), note), updated_at=?"
                       " WHERE task_id=?", (note, self.clock(), tid))
            self._event(db, t["project_id"], "task.cancelled", session, "task", tid)
            self._unblock_dependents(db, me, tid)
            return {"task": f"T{tid}", "status": "cancelled"}

    # --- push notifications for tasks (A2A TaskPushNotificationConfig) ---
    def push_create(self, task: str, url: str, token: str | None = None, auth_scheme: str | None = None,
                    auth_credentials: str | None = None, principal: str | None = None,
                    config_id: str | None = None) -> dict:
        tid = parse_id("task", task)
        self.task_get(f"T{tid}")
        cid = config_id or str(uuid.uuid4())
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO task_push VALUES(?,?,?,?,?,?,?,?)",
                       (cid, tid, url, token, auth_scheme, auth_credentials, principal, self.clock()))
        return self.push_get(f"T{tid}", cid)

    def push_list(self, task: str) -> list[dict]:
        with self._read() as db:
            rows = db.execute("SELECT * FROM task_push WHERE task_id=? ORDER BY created_at",
                              (parse_id("task", task),)).fetchall()
        return [{"id": r["config_id"], "task": f"T{r['task_id']}", "url": r["url"], "token": r["token"],
                 "auth_scheme": r["auth_scheme"], "auth_credentials": r["auth_credentials"]} for r in rows]

    def push_get(self, task: str, config_id: str) -> dict:
        for c in self.push_list(task):
            if c["id"] == config_id:
                return c
        raise CoordError("missing", f"no push config {config_id} on {task}")

    def push_delete(self, task: str, config_id: str) -> None:
        self.push_get(task, config_id)
        with self._tx() as db:
            db.execute("DELETE FROM task_push WHERE config_id=?", (config_id,))

    def task_accept(self, session: str, task: str) -> dict:
        return self._task_update(session, task, "accepted")

    def task_done(self, session: str, task: str, note: str = "") -> dict:
        return self._task_update(session, task, "done", note, require_assignee=True)
