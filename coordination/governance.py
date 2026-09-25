"""Governance: contextual voting weights, and the crisis authority.

Weights are per project and domain (architecture, graphics...), for humans and agents alike, set by
a project admin and revocable. A weighted discussion copies them when it opens: changing a weight
later never changes a vote already under way.

Crisis authority (`crisis_authority`, after the Roman dictator who handed his powers back once the
crisis was over): project admins may give a human a temporary mandate - holder, grantors, reason,
scope, precise powers, start and a mandatory expiry - to break a grave deadlock. The holder can
neither grant it to themself nor prolong it; admins (or the holder, handing it back) revoke it; at
expiry the ordinary rights come back by themselves. Every act under it is marked as such, keeps
what the normal rules would have given and the objections, and when the mandate ends a post-crisis
review opens to confirm, amend or annul what was decided. Stopping agents in an emergency is a
different permission, not a power of this mandate."""

import json

from .core import MANDATE_MAX, MANDATE_POWERS, SERVER_NAME, CoordBase, CoordError, iso, parse_id, parse_when


class GovernanceMixin(CoordBase):
    # --- weights -----------------------------------------------------------------------
    def weight_set(self, session: str, name: str, weight: float | None = None, domain: str = "") -> dict:
        """Give `name` a voting weight in this project, for a domain ("" = every domain) - admins only.
        No weight removes it (back to 1). Weights are copied into a weighted vote when it opens."""
        if weight is not None and float(weight) < 0:
            raise CoordError("bad_args", "a weight is 0 or more")
        with self._tx() as db:
            me, project = self._access(db, session, None, "admin")
            if weight is None:
                db.execute("DELETE FROM project_weights WHERE project_id=? AND name=? AND domain=?", (project, name, domain))
            else:
                db.execute("INSERT INTO project_weights VALUES(?,?,?,?,?,?) ON CONFLICT(project_id, name, domain) DO UPDATE"
                           " SET weight=excluded.weight, set_by=excluded.set_by, set_at=excluded.set_at",
                           (project, name, domain, float(weight), me["display_name"], self.clock()))
            self._event(db, project, "weight.set", session, "member", name, domain=domain, weight=weight)
            return {"project": project, "name": name, "domain": domain, "weight": weight}

    def weights(self, project: str | None = None, session: str | None = None) -> list[dict]:
        with self._read() as db:
            if project is None and session:
                project = self._session(db, session)["project_id"]
            if not project:
                raise CoordError("bad_args", "pass a project or a session")
            self._view(db, project, session)
            return [{"name": r["name"], "domain": r["domain"], "weight": r["weight"], "set_by": r["set_by"],
                     "set_at": iso(r["set_at"])}
                    for r in db.execute("SELECT * FROM project_weights WHERE project_id=? ORDER BY domain, name", (project,))]

    # --- crisis authority -------------------------------------------------------------------
    def _mandate_dict(self, db, m) -> dict:
        now = self.clock()
        status = ("revoked" if m["revoked_at"] is not None else "expired" if m["expires_at"] <= now
                  else "active" if m["starts_at"] <= now else "scheduled")
        acts = [{"discussion": f"D{r['id']}", "topic": r["topic"], "decision": r["decision"],
                 "document": f"DOC{r['decision_document_id']}" if r["decision_document_id"] else None}
                for r in db.execute("SELECT * FROM discussions WHERE mandate_id=? ORDER BY id", (m["id"],))]
        acts += [{"event": e["kind"], "entity": f"{e['entity_type']} {e['entity_id']}", **json.loads(e["payload_json"])}
                 for e in db.execute("SELECT * FROM events WHERE kind IN ('crisis.reassigned','crisis.released')"
                                     " AND project_id=? ORDER BY event_id", (m["project_id"],))
                 if json.loads(e["payload_json"]).get("mandate") == f"A{m['id']}"]
        return {"mandate": f"A{m['id']}", "project": m["project_id"], "holder": m["holder"],
                "granted_by": json.loads(m["granted_by"]), "reason": m["reason"], "scope": m["scope"],
                "powers": json.loads(m["powers"]), "starts_at": iso(m["starts_at"]), "expires_at": iso(m["expires_at"]),
                "status": status, "revoked_by": m["revoked_by"], "revoke_reason": m["revoke_reason"],
                "revoked_at": iso(m["revoked_at"]), "acts": acts,
                "review": f"D{m['review_discussion_id']}" if m["review_discussion_id"] else None}

    def mandate_grant(self, session: str, holder: str, reason: str, powers: list[str], duration: str,
                      scope: str = "", client_id: str | None = None) -> dict:
        """Give `holder` (a human) a crisis mandate in this project - admins only, never to themselves.
        `powers`: decide (arbitrate a discussion without consensus), reassign (offer a task to someone
        else), release_claims (free a claim that blocks). `duration` (48h, 3d) is mandatory and capped
        by the project's mandate_max_days (default 7) and by 30 days."""
        if not reason.strip():
            raise CoordError("reason_required", "a crisis mandate needs its reason")
        bad = [x for x in powers if x not in MANDATE_POWERS]
        if not powers or bad:
            raise CoordError("bad_args", f"powers are some of {', '.join(MANDATE_POWERS)}")

        def fn(db):
            me, project = self._access(db, session, None, "admin")
            if holder == me["display_name"]:
                raise CoordError("forbidden", "nobody grants a crisis mandate to themself")
            if not self._is_human(db, holder):
                raise CoordError("bad_args", f"{holder} is not known as a human (a crisis mandate goes to a person)")
            now = self.clock()
            ends = parse_when(duration, now)
            cap = min(MANDATE_MAX, float(self._setting(db, project, "mandate_max_days", 7)) * 86400)
            if ends <= now or ends - now > cap:
                raise CoordError("bad_args", f"a mandate lasts more than 0 and at most {cap / 86400:g} days")
            live = db.execute("SELECT id FROM mandates WHERE project_id=? AND holder=? AND revoked_at IS NULL AND"
                              " expires_at>?", (project, holder, now)).fetchone()
            if live is not None:
                raise CoordError("forbidden", f"{holder} already holds A{live['id']}: a mandate is not prolonged - "
                                 "let it end (its review opens) or revoke it first")
            mid = db.execute("INSERT INTO mandates(project_id, holder, granted_by, reason, scope, powers, starts_at,"
                             " expires_at, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                             (project, holder, json.dumps([me["display_name"]]), reason, scope or "the whole project",
                              json.dumps(sorted(set(powers))), now, ends, now)).lastrowid
            m = db.execute("INSERT INTO messages(project_id, from_session_id, from_name, kind, body, created_at, priority)"
                           " VALUES(?,?,?,?,?,?,?)",
                           (project, session, me["display_name"], "warning",
                            f"[A{mid}] crisis authority to {holder} until {iso(ends)} - powers: {', '.join(sorted(set(powers)))}"
                            f"; scope: {scope or 'the whole project'}; reason: {reason}. Acts under it are marked, "
                            "reviewed when it ends.", now, "high")).lastrowid
            db.execute("UPDATE messages SET thread_id=? WHERE id=?", (m, m))
            self._event(db, project, "mandate.granted", session, "mandate", mid, holder=holder, until=iso(ends),
                        powers=sorted(set(powers)))
            return self._mandate_dict(db, db.execute("SELECT * FROM mandates WHERE id=?", (mid,)).fetchone())
        return self._mutate("mandate_grant", client_id, fn)

    def mandate_revoke(self, session: str, mandate: str, reason: str) -> dict:
        """End a mandate now: a project admin, or its holder handing the powers back. The review opens."""
        if not reason.strip():
            raise CoordError("reason_required", "revoking a mandate needs its reason")
        with self._tx() as db:
            m = db.execute("SELECT * FROM mandates WHERE id=?", (parse_id("mandate", mandate),)).fetchone()
            if m is None:
                raise CoordError("missing", f"no mandate {mandate}")
            me, project = self._access(db, session, m["project_id"], "view")
            if me["display_name"] != m["holder"]:
                self._access(db, session, m["project_id"], "admin")
            if m["revoked_at"] is not None or m["expires_at"] <= self.clock():
                raise CoordError("closed", f"A{m['id']} has already ended")
            db.execute("UPDATE mandates SET revoked_at=?, revoked_by=?, revoke_reason=? WHERE id=?",
                       (self.clock(), me["display_name"], reason, m["id"]))
            self._event(db, project, "mandate.revoked", session, "mandate", m["id"], reason=reason)
            self._sweep_mandates(db)
            return self._mandate_dict(db, db.execute("SELECT * FROM mandates WHERE id=?", (m["id"],)).fetchone())

    def mandates(self, project: str | None = None, session: str | None = None) -> list[dict]:
        """Crisis mandates - active, scheduled and ended - with every act taken under each, and its review."""
        with self._tx() as db:
            self._sweep_mandates(db)
            q, a = "SELECT * FROM mandates WHERE 1=1", []
            if project:
                self._view(db, project, session)
                q += " AND project_id=?"; a.append(project)
            else:
                f, fargs = self._view_filter(db, session)
                q += f" AND {f}"; a += list(fargs)
            return [self._mandate_dict(db, m) for m in db.execute(q + " ORDER BY id DESC", a).fetchall()]

    def _sweep_mandates(self, db) -> None:
        """Mandates that ended (expiry or revocation) get their post-crisis review: a discussion listing
        each act, one proposal per decision to confirm - amending or annulling stays open to all."""
        now = self.clock()
        for m in db.execute("SELECT * FROM mandates WHERE review_discussion_id IS NULL AND (revoked_at IS NOT NULL"
                            " OR expires_at<=?)", (now,)).fetchall():
            server = {"session_id": SERVER_NAME, "display_name": SERVER_NAME, "project_id": m["project_id"]}
            d = self._mandate_dict(db, m)
            ended = f"revoked by {m['revoked_by']} ({m['revoke_reason']})" if m["revoked_at"] else "expired"
            topic = (f"Post-crisis review of A{m['id']} ({m['holder']}, {ended}): confirm, amend or annul what was "
                     f"decided under it")
            opened = self._discussion_open(db, server, topic)
            did = opened["id"]
            for act in d["acts"]:
                body = (f"Confirm {act['discussion']} ({act['topic']}): {act['decision']}" if "discussion" in act
                        else f"Confirm {act['event']} on {act['entity']}: {act.get('reason', '')}")
                db.execute("INSERT INTO proposals(discussion_id, author_session_id, author_name, body, created_at)"
                           " VALUES(?,?,?,?,?)", (did, SERVER_NAME, SERVER_NAME, body, now))
            db.execute("UPDATE mandates SET review_discussion_id=? WHERE id=?", (did, m["id"]))
            self._event(db, m["project_id"], "mandate.ended", None, "mandate", m["id"], review=f"D{did}", how=ended)

    # --- the powers besides deciding ------------------------------------------------------------
    def crisis_reassign(self, session: str, task: str, to: str, reason: str) -> dict:
        """Under a mandate with the power `reassign`: offer a task to someone else (it returns offered)."""
        if not reason.strip():
            raise CoordError("reason_required", "a crisis act needs its reason")
        with self._tx() as db:
            t = db.execute("SELECT * FROM tasks WHERE task_id=?", (parse_id("task", task),)).fetchone()
            if t is None:
                raise CoordError("missing", f"no task {task}")
            me, project = self._access(db, session, t["project_id"], "view")
            m = self._mandate(db, project, me["display_name"], "reassign")
            if m is None:
                raise CoordError("forbidden", f"{me['display_name']} holds no active mandate with the power reassign")
            if t["status"] in ("done", "cancelled"):
                raise CoordError("closed", f"T{t['task_id']} is {t['status']}")
            target = self._resolve_name(db, to, project)
            db.execute("UPDATE tasks SET status='offered', assigned_to=?, assigned_name=?, note=?, updated_at=? WHERE task_id=?",
                       (target["session_id"], target["display_name"],
                        f"reassigned under crisis mandate A{m['id']} by {me['display_name']}: {reason}", self.clock(),
                        t["task_id"]))
            for sid in dict.fromkeys(x for x in (t["assigned_to"], target["session_id"]) if x):
                self._notify(db, me, sid, f"[T{t['task_id']}] reassigned to {target['display_name']} under crisis "
                             f"mandate A{m['id']}: {reason}", kind="warning")
            self._event(db, project, "crisis.reassigned", session, "task", t["task_id"], mandate=f"A{m['id']}",
                        to=target["display_name"], previous=t["assigned_name"], reason=reason)
            return {"task": f"T{t['task_id']}", "status": "offered", "to": target["display_name"], "mandate": f"A{m['id']}"}

    def crisis_release(self, session: str, claim: str, reason: str) -> dict:
        """Under a mandate with the power `release_claims`: free a claim held by someone else."""
        if not reason.strip():
            raise CoordError("reason_required", "a crisis act needs its reason")
        with self._tx() as db:
            c = db.execute("SELECT * FROM claims WHERE claim_id=?", (parse_id("claim", claim),)).fetchone()
            if c is None:
                raise CoordError("missing", f"no claim {claim}")
            me, project = self._access(db, session, c["project_id"], "view")
            m = self._mandate(db, project, me["display_name"], "release_claims")
            if m is None:
                raise CoordError("forbidden", f"{me['display_name']} holds no active mandate with the power release_claims")
            if c["released_at"] is not None:
                raise CoordError("released", f"C{c['claim_id']} was already released")
            db.execute("UPDATE claims SET released_at=?, released_by=? WHERE claim_id=?",
                       (self.clock(), f"crisis:A{m['id']}", c["claim_id"]))
            self._notify(db, me, c["owner_session_id"], f"[C{c['claim_id']}] released under crisis mandate A{m['id']} "
                         f"by {me['display_name']}: {reason}", kind="warning")
            self._event(db, project, "crisis.released", session, "claim", c["claim_id"], mandate=f"A{m['id']}",
                        owner=c["owner_name"], reason=reason)
            return {"claim": f"C{c['claim_id']}", "released": True, "mandate": f"A{m['id']}"}
