"""Routines: standing work that comes back by itself - a security review every day, the docs after
every commit touching src/. The server keeps the schedule and a run lease; agents see what is due
in `poll`/`context`, one of them takes the run (`routine_start`), reports it (`routine_done`).
Nothing runs server-side: like everything else here, it waits for an agent to poll."""

import json
import re

from .core import ROUTINE_OUTCOMES, ROUTINE_STATUSES, CoordError, iso, parse_id

MIN_EVERY = 300          # 5 minutes: a routine is not a busy loop
RUN_LEASE = 3600         # a run left unfinished frees itself after this


def parse_every(value: str) -> float:
    """"90m", "6h", "1d", "2w" -> seconds."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([mhdw])", str(value).strip())
    if not m:
        raise CoordError("bad_every", f"every {value!r}: use 30m, 6h, 1d or 2w")
    seconds = float(m.group(1)) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
    if seconds < MIN_EVERY:
        raise CoordError("bad_every", f"every {value!r}: at least 5m")
    return seconds


class RoutinesMixin:
    def routine_create(self, session: str, title: str, instructions: str = "", every: str | None = None,
                       on_commit: bool = False, paths: list[str] | None = None,
                       client_id: str | None = None) -> dict:
        interval = parse_every(every) if every else None
        if interval is None and not on_commit:
            raise CoordError("bad_args", "a routine needs --every and/or --on-commit")
        watched = sorted({self._norm(p, None)[1] for p in paths or []})

        def fn(db):
            me = self._session(db, session)
            now = self.clock()
            rid = db.execute("INSERT INTO routines(project_id, title, instructions, every, on_commit, paths,"
                             " created_by, pending, pending_reason, created_at, updated_at)"
                             " VALUES(?,?,?,?,?,?,?,1,'first run',?,?)",
                             (me["project_id"], title, instructions, interval, int(bool(on_commit)),
                              json.dumps(watched), me["display_name"], now, now)).lastrowid
            self._event(db, me["project_id"], "routine.created", session, "routine", rid)
            return {"routine": f"R{rid}", "id": rid}
        return self._mutate("routine_create", client_id, fn)

    def _routine_row(self, db, routine: str):
        rid = parse_id("routine", routine)
        r = db.execute("SELECT * FROM routines WHERE routine_id=?", (rid,)).fetchone()
        if r is None:
            raise CoordError("missing", f"no routine R{rid}")
        return r

    def _running(self, db, r) -> bool:
        if not r["runner_session_id"] or (r["run_expires"] or 0) <= self.clock():
            return False
        s = db.execute("SELECT * FROM sessions WHERE session_id=?", (r["runner_session_id"],)).fetchone()
        return s is not None and self._live(s)

    def _routine_dict(self, db, r) -> dict:
        now = self.clock()
        next_due = (r["last_run_at"] + r["every"]) if r["every"] and r["last_run_at"] is not None else None
        running = self._running(db, r)
        due = r["status"] == "active" and not running and bool(
            r["pending"] or (next_due is not None and next_due <= now))
        return {"routine": f"R{r['routine_id']}", "title": r["title"], "status": r["status"],
                "every": r["every"], "on_commit": bool(r["on_commit"]), "paths": json.loads(r["paths"]),
                "due": due, "why": (r["pending_reason"] if r["pending"] else "interval") if due else None,
                "next_due": iso(next_due), "running": r["runner_name"] if running else None,
                "last_run_at": iso(r["last_run_at"]), "last_run_by": r["last_run_by"],
                "last_outcome": r["last_outcome"], "last_result": r["last_result"], "created_by": r["created_by"]}

    def routines(self, project: str | None = None, due: bool = False, include_retired: bool = False) -> list[dict]:
        with self._read() as db:
            q, a = "SELECT * FROM routines WHERE 1=1", []
            if project:
                q += " AND project_id=?"; a.append(project)
            if not include_retired:
                q += " AND status!='retired'"
            out = [self._routine_dict(db, r) for r in db.execute(q + " ORDER BY routine_id", a).fetchall()]
        return [r for r in out if r["due"]] if due else out

    def routine_get(self, routine: str) -> dict:
        """One routine in full: its instructions and its last runs."""
        with self._read() as db:
            r = self._routine_row(db, routine)
            out = self._routine_dict(db, r) | {"instructions": r["instructions"], "project": r["project_id"]}
            out["runs"] = [{"run": x["run"], "by": x["name"], "trigger": x["trigger"], "outcome": x["outcome"],
                            "result": x["result"], "started_at": iso(x["started_at"]),
                            "finished_at": iso(x["finished_at"])}
                           for x in db.execute("SELECT * FROM routine_runs WHERE routine_id=? ORDER BY run DESC"
                                               " LIMIT 10", (r["routine_id"],)).fetchall()]
        return out

    def routine_start(self, session: str, routine: str) -> dict:
        """Take this run (due or not - a manual run is fine); refused while another live session runs it."""
        with self._tx() as db:
            me = self._session(db, session)
            r = self._routine_row(db, routine)
            if r["status"] != "active":
                raise CoordError("not_active", f"R{r['routine_id']} is {r['status']}")
            if self._running(db, r) and r["runner_session_id"] != session:
                raise CoordError("running", f"R{r['routine_id']} is being run by {r['runner_name']}",
                                 {"runner": r["runner_name"]})
            d = self._routine_dict(db, r)
            trigger = d["why"] or "manual"
            now = self.clock()
            run = db.execute("SELECT COALESCE(MAX(run), 0) + 1 FROM routine_runs WHERE routine_id=?",
                             (r["routine_id"],)).fetchone()[0]
            db.execute("INSERT INTO routine_runs(routine_id, run, session_id, name, trigger, started_at)"
                       " VALUES(?,?,?,?,?,?)", (r["routine_id"], run, session, me["display_name"], trigger, now))
            db.execute("UPDATE routines SET runner_session_id=?, runner_name=?, run_expires=?, updated_at=?"
                       " WHERE routine_id=?", (session, me["display_name"], now + RUN_LEASE, now, r["routine_id"]))
            self._event(db, r["project_id"], "routine.started", session, "routine", r["routine_id"], run=run)
            return {"routine": f"R{r['routine_id']}", "run": run, "trigger": trigger, "title": r["title"],
                    "instructions": r["instructions"], "until": iso(now + RUN_LEASE),
                    "last_result": r["last_result"]}

    def routine_done(self, session: str, routine: str, result: str = "", outcome: str = "ok") -> dict:
        """Report this session's run. `issues`/`failed` also post a warning everyone sees."""
        if outcome not in ROUTINE_OUTCOMES:
            raise CoordError("bad_outcome", f"outcome must be one of {', '.join(ROUTINE_OUTCOMES)}")
        with self._tx() as db:
            me = self._session(db, session)
            r = self._routine_row(db, routine)
            if r["runner_session_id"] != session:
                raise CoordError("forbidden", f"R{r['routine_id']} is not being run by you - routine start first")
            now = self.clock()
            run = db.execute("SELECT MAX(run) FROM routine_runs WHERE routine_id=? AND session_id=?",
                             (r["routine_id"], session)).fetchone()[0]
            db.execute("UPDATE routine_runs SET finished_at=?, outcome=?, result=? WHERE routine_id=? AND run=?",
                       (now, outcome, result, r["routine_id"], run))
            db.execute("UPDATE routines SET runner_session_id=NULL, runner_name=NULL, run_expires=NULL,"
                       " pending=0, pending_reason=NULL, last_run_at=?, last_run_by=?, last_outcome=?,"
                       " last_result=?, updated_at=? WHERE routine_id=?",
                       (now, me["display_name"], outcome, result, now, r["routine_id"]))
            if outcome != "ok":
                db.execute("INSERT INTO messages(project_id, from_session_id, from_name, kind, body, created_at)"
                           " VALUES(?,?,?,?,?,?)",
                           (r["project_id"], session, me["display_name"], "warning",
                            f"[R{r['routine_id']}] {r['title']}: {outcome} - {result}"[:2000], now))
            self._event(db, r["project_id"], "routine.done", session, "routine", r["routine_id"],
                        run=run, outcome=outcome)
            return {"routine": f"R{r['routine_id']}", "run": run, "outcome": outcome}

    def routine_update(self, session: str, routine: str, status: str | None = None, every: str | None = None,
                       instructions: str | None = None, on_commit: bool | None = None,
                       paths: list[str] | None = None, title: str | None = None) -> dict:
        """Pause, resume, retire or reshape a routine (anyone in the project: routines are shared)."""
        if status is not None and status not in ROUTINE_STATUSES:
            raise CoordError("bad_status", f"status must be one of {', '.join(ROUTINE_STATUSES)}")
        interval = parse_every(every) if every else None
        with self._tx() as db:
            self._session(db, session)
            r = self._routine_row(db, routine)
            watched = json.dumps(sorted({self._norm(p, None)[1] for p in paths})) if paths is not None else None
            if (on_commit is False or (on_commit is None and not r["on_commit"])) and not (interval or r["every"]):
                raise CoordError("bad_args", "a routine needs --every and/or --on-commit")
            db.execute("UPDATE routines SET status=COALESCE(?, status), every=COALESCE(?, every),"
                       " instructions=COALESCE(?, instructions), on_commit=COALESCE(?, on_commit),"
                       " paths=COALESCE(?, paths), title=COALESCE(?, title), updated_at=? WHERE routine_id=?",
                       (status, interval, instructions, None if on_commit is None else int(on_commit), watched,
                        title, self.clock(), r["routine_id"]))
            self._event(db, r["project_id"], "routine.updated", session, "routine", r["routine_id"])
            return self._routine_dict(db, self._routine_row(db, routine))

    def _routines_on_commit(self, db, project: str, sha: str, paths: set[str]) -> list[str]:
        """Called by post_commit: mark due the commit routines whose watched paths the commit touches."""
        marked = []
        for r in db.execute("SELECT * FROM routines WHERE project_id=? AND status='active' AND on_commit=1",
                            (project,)).fetchall():
            watched = json.loads(r["paths"])
            if not watched or any(p == w or p.startswith(w.rstrip("/") + "/") for w in watched for p in paths):
                db.execute("UPDATE routines SET pending=1, pending_reason=? WHERE routine_id=?",
                           (f"commit {sha[:10]}", r["routine_id"]))
                marked.append(f"R{r['routine_id']}")
        return marked
