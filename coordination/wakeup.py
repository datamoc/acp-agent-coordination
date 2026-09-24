"""Agents at rest and bringing them back: session states, pending work, wake-up requests.

A session's state (active, idle, paused, ended, unreachable) is kept apart from its work: a paused
agent can still hold an accepted task, and an open task in the project is not one assigned to it.

A wake-up request is abstract - "resume agent X because task Y is pending" - and its delivery is
concrete: a wake hook (a webhook the agent's console or supervisor registered, POSTed by
coord-server) or, without one, a manual relaunch, with a resume summary the human can paste. The
request is followed through requested -> delivered -> woken (the agent polled again, or a new
session of the same identity started) -> accepted / refused (the agent says why). A message in an
inbox is never counted as a wake-up.

Limits against loops: one request per agent, reason and reference every WAKE_MIN_INTERVAL, at most
WAKE_MAX_ATTEMPTS unanswered ones - then the request is refused with a diagnostic for a human."""

import json

from .core import (
    SETTINGS,
    WAKE_MAX_ATTEMPTS,
    WAKE_MIN_INTERVAL,
    WAKE_REASONS,
    CoordBase,
    CoordError,
    iso,
    parse_id,
)

OPEN = ("requested", "delivered", "woken")      # a request still in flight


class WakeupMixin(CoordBase):
    # --- pausing and coming back ---------------------------------------------------------
    def pause(self, session: str, reason: str = "") -> dict:
        """Say you are pausing (nothing left for you right now, a quota, a break). Your work stays
        assigned; the dashboard shows you paused, and a wake-up request can bring you back."""
        with self._tx() as db:
            me, project = self._access(db, session, None, "view")
            db.execute("UPDATE sessions SET paused_at=?, pause_reason=? WHERE session_id=?",
                       (self.clock(), (reason or "")[:400], session))
            self._event(db, project, "session.paused", session, "session", session, reason=reason or "")
            pending = self._pending_work(db, project, me["display_name"], session)
            return {"name": me["display_name"], "state": "paused", "pending": pending,
                    **({"warning": "you pause with work still assigned or waiting for you"}
                       if any(pending.values()) else {})}

    def _awake(self, db, me) -> None:
        """This session is back (a poll, a heartbeat, a new session of the same identity): it is no
        longer paused, and the wake-up requests aimed at it or at its identity are woken."""
        if me is None:
            return
        now = self.clock()
        if me["paused_at"] is not None:
            db.execute("UPDATE sessions SET paused_at=NULL, pause_reason=NULL WHERE session_id=?", (me["session_id"],))
            self._event(db, me["project_id"], "session.resumed", me["session_id"], "session", me["session_id"])
        same = [r[0] for r in db.execute(
            "SELECT session_id FROM sessions WHERE project_id=? AND family=? AND user IS ? AND model IS ?"
            " AND principal IS ?", (me["project_id"], me["family"], me["user"], me["model"], me["principal"]))]
        marks = ",".join("?" * len(same))
        rows = db.execute("SELECT id FROM wake_requests WHERE project_id=? AND status IN ('requested','delivered') AND"
                          f" (target_name=? OR target_session_id IN ({marks}))",
                          (me["project_id"], me["display_name"], *same)).fetchall()
        for (wid,) in rows:
            db.execute("UPDATE wake_requests SET status='woken', target_session_id=?, woken_at=?,"
                       " delivered_at=COALESCE(delivered_at, ?) WHERE id=?", (me["session_id"], now, now, wid))
            self._event(db, me["project_id"], "wake.woken", me["session_id"], "wake", wid)

    # --- who is there and what waits for them -------------------------------------------
    def _pending_work(self, db, project: str, name: str, session_id: str | None) -> dict:
        tasks = db.execute("SELECT task_id, title, status FROM tasks WHERE project_id=? AND assigned_name=? AND"
                           " status IN ('offered','accepted') ORDER BY priority DESC, task_id", (project, name)).fetchall()
        ready, blocked = [], []
        for t in tasks:
            (blocked if self._blocked_by(db, t["task_id"]) else ready).append(f"T{t['task_id']}")
        waiting = self._awaiting(db, session_id or "", name)
        discussions = [f"D{r[0]}" for r in db.execute(
            "SELECT d.id FROM discussions d JOIN discussion_participants p ON p.discussion_id=d.id WHERE"
            " d.project_id=? AND d.status='open' AND p.name=? AND NOT EXISTS(SELECT 1 FROM proposals x JOIN"
            " reactions r ON r.proposal_id=x.id WHERE x.discussion_id=d.id AND r.author_name=p.name)"
            " AND EXISTS(SELECT 1 FROM proposals x WHERE x.discussion_id=d.id AND x.status='open')",
            (project, name))]
        return {"ready_tasks": ready, "blocked_tasks": blocked,
                "messages": [f"#{m['id']}" for m in waiting], "discussions": discussions}

    def agents(self, project: str | None = None, session: str | None = None) -> list[dict]:
        """Everyone who took part in a project, one line per name: their session state (active, idle,
        paused, ended, unreachable), what is assigned to or waiting for them, and open wake-up requests."""
        with self._read() as db:
            if project is None and session:
                project = self._session(db, session)["project_id"]
            if not project:
                raise CoordError("bad_args", "pass a project or a session")
            self._view(db, project, session)
            rows = db.execute("SELECT * FROM sessions WHERE project_id=? ORDER BY heartbeat_at DESC", (project,)).fetchall()
            seen, out = set(), []
            for r in rows:
                if r["display_name"] in seen:
                    continue
                seen.add(r["display_name"])
                pending = self._pending_work(db, project, r["display_name"], r["session_id"])
                state = self._session_state(r)
                wakes = [self._wake_dict(w) for w in db.execute(
                    f"SELECT * FROM wake_requests WHERE project_id=? AND target_name=? AND status IN ({','.join('?' * len(OPEN))})"
                    " ORDER BY id", (project, r["display_name"], *OPEN))]
                out.append({"name": r["display_name"], "kind": r["kind"], "state": state, "status": r["status"],
                            "paused": r["pause_reason"] if state == "paused" else None,
                            "seen": iso(r["heartbeat_at"]), "family": r["family"], "model": r["model"],
                            "pending": pending, "wake_requests": wakes,
                            "asleep_with_work": state in ("paused", "idle", "unreachable", "ended")
                            and bool(pending["ready_tasks"] or pending["messages"] or pending["discussions"])})
        return out

    # --- wake-up requests -----------------------------------------------------------------
    @staticmethod
    def _wake_dict(w) -> dict:
        return {"wake": f"W{w['id']}", "agent": w["target_name"], "reason": w["reason"], "ref": w["ref"],
                "note": w["note"], "status": w["status"], "mechanism": w["mechanism"],
                "requested_by": w["requested_by"], "auto": bool(w["auto"]), "diagnostic": w["diagnostic"],
                "response": w["response"], "at": iso(w["created_at"]), "delivered_at": iso(w["delivered_at"]),
                "woken_at": iso(w["woken_at"]), "answered_at": iso(w["answered_at"])}

    def _resume_summary(self, db, project: str, name: str, reason: str, ref: str | None, note: str) -> str:
        """What a relaunched agent (or a new session taking over) needs: the precise work that motivates
        the request, then everything persistent that waits for it. Its volatile context is not restored."""
        why = {"task": "a task assigned to you", "message": "a message waiting for your answer",
               "question": "a question waiting for you", "unblocked": "a task of yours is ready (its prerequisites are done)",
               "review": "a review is asked of you", "other": "work waits for you"}[reason]
        lines = [f"coord: {name}, please resume in project {project} - {why}" + (f" ({ref})" if ref else "")
                 + (f": {note}" if note else "") + "."]
        if ref and ref.upper().startswith("T"):
            t = db.execute("SELECT * FROM tasks WHERE task_id=?", (parse_id("task", ref),)).fetchone()
            if t is not None:
                lines.append(f"{ref} [{t['status']}] {t['title']}" + (f" - {t['description']}" if t["description"] else ""))
        if ref and ref.startswith("#"):
            m = db.execute("SELECT * FROM messages WHERE id=?", (int(ref[1:]),)).fetchone()
            if m is not None:
                lines.append(f"{ref} from {m['from_name']}: {m['body'][:300]}")
        p = self._pending_work(db, project, name, None)
        for key, label in (("ready_tasks", "ready tasks"), ("blocked_tasks", "blocked tasks"),
                           ("messages", "messages waiting for you"), ("discussions", "discussions waiting for your stance")):
            if p[key]:
                lines.append(f"{label}: {', '.join(p[key])}")
        lines.append("Start with: coord whoami <family> (same user/model), coord context, coord poll; then "
                     "coord wake answer W<id> accept|refuse \"why\".")
        return "\n".join(lines)

    def _wake_insert(self, db, me, project: str, name: str, reason: str, ref: str | None, note: str,
                     auto: bool) -> dict:
        now = self.clock()
        recent = db.execute("SELECT * FROM wake_requests WHERE project_id=? AND target_name=? AND reason=? AND ref IS ?"
                            " ORDER BY id DESC", (project, name, reason, ref)).fetchall()
        if recent and recent[0]["created_at"] > now - WAKE_MIN_INTERVAL:
            raise CoordError("too_soon", f"{name} was asked to resume for this {int(now - recent[0]['created_at'])} s ago "
                             f"(W{recent[0]['id']}, {recent[0]['status']}); wait {WAKE_MIN_INTERVAL // 60} min between "
                             "requests", {"wake": f"W{recent[0]['id']}"})
        unanswered = [w for w in recent if w["answered_at"] is None and w["status"] != "refused"]
        if len(unanswered) >= WAKE_MAX_ATTEMPTS:
            diag = (f"{len(unanswered)} requests to {name} for {reason} {ref or ''} stayed unanswered "
                    f"({', '.join('W{}:{}'.format(w['id'], w['status']) for w in unanswered[:WAKE_MAX_ATTEMPTS])}): "
                    "it does not come back, or falls asleep again - relaunch it by hand or reassign the work")
            raise CoordError("max_attempts", diag, {"diagnostic": diag})
        target = self._live_by_name(db, name)
        last = target or db.execute("SELECT * FROM sessions WHERE project_id=? AND display_name=?"
                                    " ORDER BY heartbeat_at DESC LIMIT 1", (project, name)).fetchone()
        if last is None:
            raise CoordError("unknown_recipient", f"nobody named {name} ever joined {project}")
        hook = db.execute("SELECT * FROM wake_hooks WHERE project_id=? AND target IN (?, ?, '*')"
                          " ORDER BY target='*'", (project, name, last["family"])).fetchone()
        mechanism = "webhook" if hook else "session" if target is not None else "manual"
        wid = db.execute("INSERT INTO wake_requests(project_id, target_name, target_session_id, reason, ref, note,"
                         " requested_by, auto, status, mechanism, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (project, name, last["session_id"], reason, ref, note, me["display_name"], int(auto),
                          "requested", mechanism, now)).lastrowid
        summary = self._resume_summary(db, project, name, reason, ref, note)
        if target is not None:                    # a live session: it sees the request at its next poll
            db.execute("INSERT INTO messages(project_id, from_session_id, from_name, to_session_id, to_name, kind,"
                       " body, created_at, priority) VALUES(?,?,?,?,?,?,?,?,?)",
                       (project, me["session_id"], me["display_name"], target["session_id"], name, "question",
                        f"[W{wid}] " + summary, now, "high"))
            mid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.execute("UPDATE messages SET thread_id=? WHERE id=?", (mid, mid))
            db.execute("INSERT OR IGNORE INTO message_recipients(message_id, session_id, name, state) VALUES(?,?,?,'sent')",
                       (mid, target["session_id"], name))
        self._event(db, project, "wake.requested", me["session_id"], "wake", wid, agent=name, reason=reason,
                    ref=ref, mechanism=mechanism, auto=auto)
        return {"wake": f"W{wid}", "id": wid, "agent": name, "status": "requested", "mechanism": mechanism,
                "state": self._session_state(last), "resume": summary,
                **({"manual": f"{name} has no wake hook and no live session: relaunch it by hand with the resume "
                              "summary below"} if mechanism == "manual" else {})}

    def wake_request(self, session: str, agent: str, reason: str = "task", ref: str | None = None,
                     note: str = "", client_id: str | None = None) -> dict:
        """Ask `agent` to resume, saying why (reason + ref: T12, #340, D4). With a wake hook for it,
        coord-server calls the hook and records the outcome; with a live session, the request waits in
        its next poll; otherwise it must be relaunched by hand - the result carries the resume summary."""
        if reason not in WAKE_REASONS:
            raise CoordError("bad_reason", f"reason must be one of {', '.join(WAKE_REASONS)}")

        def fn(db):
            me, project = self._access(db, session, None, "participate")
            return self._wake_insert(db, me, project, agent, reason, ref, note, auto=False)
        return self._mutate("wake_request", client_id, fn)

    def _auto_wake(self, db, me, project: str, name: str | None, reason: str, ref: str, note: str) -> None:
        """The project's rule (setting wake_auto=request): a sleeping agent whose assigned work just
        became actionable is asked to resume - within the same limits as a human request."""
        if not name or self._setting(db, project, "wake_auto", "off") != "request":
            return
        last = db.execute("SELECT * FROM sessions WHERE project_id=? AND display_name=? ORDER BY heartbeat_at DESC"
                          " LIMIT 1", (project, name)).fetchone()
        if last is None or self._session_state(last) == "active":
            return
        try:
            self._wake_insert(db, me, project, name, reason, ref, note, auto=True)
        except CoordError as e:                    # limited: leave the diagnostic where the humans look
            self._event(db, project, "wake.skipped", me["session_id"], "task", ref, agent=name, why=str(e))

    def wake_answer(self, session: str, wake: str, accept: bool = True, note: str = "") -> dict:
        """The agent's answer to a wake-up request: it takes the work up, or refuses / postpones it and
        says why (the requester is told; the answer becomes part of the record)."""
        if not accept and not note.strip():
            raise CoordError("comment_required", "refusing needs its reason: coord wake answer W<id> refuse \"why\"")
        with self._tx() as db:
            w = db.execute("SELECT * FROM wake_requests WHERE id=?", (parse_id("wake", wake),)).fetchone()
            if w is None:
                raise CoordError("missing", f"no wake-up request {wake}")
            me, project = self._access(db, session, w["project_id"], "view")
            target = db.execute("SELECT * FROM sessions WHERE session_id=?", (w["target_session_id"],)).fetchone()
            def ident(r):
                return None if r is None else (r["family"], r["user"], r["model"], r["principal"])
            if me["display_name"] != w["target_name"] and ident(me) != ident(target):
                raise CoordError("forbidden", f"W{w['id']} is addressed to {w['target_name']}")
            if w["answered_at"] is not None:
                raise CoordError("closed", f"W{w['id']} was already {w['status']}")
            now, status = self.clock(), "accepted" if accept else "refused"
            db.execute("UPDATE wake_requests SET status=?, response=?, answered_at=?, woken_at=COALESCE(woken_at, ?),"
                       " delivered_at=COALESCE(delivered_at, ?), target_session_id=? WHERE id=?",
                       (status, note, now, now, now, session, w["id"]))
            asker = self._live_by_name(db, w["requested_by"])
            self._notify(db, me, asker["session_id"] if asker else None,
                         f"[W{w['id']}] {me['display_name']} {status} to resume" + (f": {note}" if note else ""),
                         kind="info" if accept else "warning")
            self._event(db, project, f"wake.{status}", session, "wake", w["id"], note=note)
            return {"wake": f"W{w['id']}", "status": status}

    def wake_requests(self, project: str | None = None, target: str | None = None, open_only: bool = False,
                      session: str | None = None) -> list[dict]:
        """Wake-up requests and how far each got (requested, delivered, woken, accepted, refused, failed)."""
        with self._read() as db:
            q, a = "SELECT * FROM wake_requests WHERE 1=1", []
            if project:
                self._view(db, project, session)
                q += " AND project_id=?"; a.append(project)
            else:
                f, fargs = self._view_filter(db, session)
                q += f" AND {f}"; a += list(fargs)
            if target:
                q += " AND target_name=?"; a.append(target)
            if open_only:
                q += f" AND status IN ({','.join('?' * len(OPEN))})"; a += list(OPEN)
            return [self._wake_dict(w) for w in db.execute(q + " ORDER BY id", a).fetchall()]

    # --- the concrete mechanism: wake hooks (delivered by coord-server) ------------------------
    def wake_hook_set(self, session: str, target: str, url: str | None = None, token: str | None = None) -> dict:
        """Register (or with no url, remove) the webhook that wakes `target` - an agent name, a family
        (claude) or * - in this project. coord-server POSTs {wake, agent, reason, ref, resume} to it
        (hosts: loopback or --push-allow) and records delivered or failed. Project admins only."""
        with self._tx() as db:
            me, project = self._access(db, session, None, "admin")
            if url:
                if not url.startswith(("http://", "https://")):
                    raise CoordError("bad_args", "the wake hook must be an http(s) URL")
                db.execute("INSERT INTO wake_hooks VALUES(?,?,?,?,?,?) ON CONFLICT(project_id, target) DO UPDATE SET"
                           " url=excluded.url, token=excluded.token, created_by=excluded.created_by,"
                           " created_at=excluded.created_at", (project, target, url, token, me["display_name"], self.clock()))
            else:
                db.execute("DELETE FROM wake_hooks WHERE project_id=? AND target=?", (project, target))
            self._event(db, project, "wake.hook", session, "member", target, removed=not url)
            return {"project": project, "target": target, "url": url, "removed": not url}

    def _wake_delivery_plan(self, wake_id: int) -> dict | None:
        """For the transport: where to POST a webhook-mechanism request, and what (None: nothing to do)."""
        with self._read() as db:
            w = db.execute("SELECT * FROM wake_requests WHERE id=?", (wake_id,)).fetchone()
            if w is None or w["mechanism"] != "webhook" or w["status"] != "requested":
                return None
            last = db.execute("SELECT family FROM sessions WHERE session_id=?", (w["target_session_id"],)).fetchone()
            hook = db.execute("SELECT * FROM wake_hooks WHERE project_id=? AND target IN (?, ?, '*') ORDER BY target='*'",
                              (w["project_id"], w["target_name"], last["family"] if last else "")).fetchone()
            if hook is None:
                return None
            body = {"wake": f"W{w['id']}", "project": w["project_id"], "agent": w["target_name"], "reason": w["reason"],
                    "ref": w["ref"], "note": w["note"],
                    "resume": self._resume_summary(db, w["project_id"], w["target_name"], w["reason"], w["ref"], w["note"])}
            return {"url": hook["url"], "token": hook["token"], "body": json.dumps(body).encode()}

    def _wake_delivered(self, wake_id: int, ok: bool, diagnostic: str = "") -> None:
        """The transport's report: the hook answered (delivered) or not (failed, with the reason)."""
        with self._tx() as db:
            w = db.execute("SELECT * FROM wake_requests WHERE id=?", (wake_id,)).fetchone()
            if w is None or w["status"] != "requested":
                return
            db.execute("UPDATE wake_requests SET status=?, delivered_at=?, diagnostic=? WHERE id=?",
                       ("delivered" if ok else "failed", self.clock() if ok else None, diagnostic or None, wake_id))
            self._event(db, w["project_id"], "wake.delivered" if ok else "wake.failed", None, "wake", wake_id,
                        diagnostic=diagnostic)

    # --- project settings -------------------------------------------------------------------
    def setting_set(self, session: str, key: str, value: str) -> dict:
        """Set a project rule (admins): wake_auto off|propose|request, mandate_max_days <n>."""
        if key not in SETTINGS:
            raise CoordError("bad_args", f"setting must be one of {', '.join(SETTINGS)}")
        allowed = SETTINGS[key]
        if allowed is not None and value not in allowed:
            raise CoordError("bad_args", f"{key} must be one of {', '.join(allowed)}")
        parsed: object = value
        if allowed is None:
            try:
                parsed = float(value)
            except ValueError as e:
                raise CoordError("bad_args", f"{key} must be a number") from e
        with self._tx() as db:
            me, project = self._access(db, session, None, "admin")
            db.execute("INSERT INTO project_settings VALUES(?,?,?,?,?) ON CONFLICT(project_id, key) DO UPDATE SET"
                       " value=excluded.value, set_by=excluded.set_by, set_at=excluded.set_at",
                       (project, key, json.dumps(parsed), me["display_name"], self.clock()))
            self._event(db, project, "setting.set", session, "setting", key, value=parsed)
            return {"project": project, "key": key, "value": parsed}

    def settings(self, project: str | None = None, session: str | None = None) -> dict:
        with self._read() as db:
            if project is None and session:
                project = self._session(db, session)["project_id"]
            if not project:
                raise CoordError("bad_args", "pass a project or a session")
            self._view(db, project, session)
            rows = {r["key"]: json.loads(r["value"]) for r in
                    db.execute("SELECT key, value FROM project_settings WHERE project_id=?", (project,))}
        return {"project": project, "settings": {"wake_auto": rows.get("wake_auto", "off"),
                                                 "mandate_max_days": rows.get("mandate_max_days", 7)}}
