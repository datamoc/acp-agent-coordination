"""Claims: scopes, fences, roles (ask/grant), git checks."""

import contextlib
import sqlite3

from . import scopes
from .core import CLAIM_TTL, ROLES, ROLES_BY_CONSENT, SESSION_TTL, CoordBase, CoordError, iso, parse_id


class ClaimsMixin(CoordBase):
    def _norm(self, raw: str, tree: bool | None) -> tuple[str, str]:
        try:
            path, is_dir = scopes.normalize(raw, self.case_insensitive)
        except scopes.ScopeError as e:
            raise CoordError("bad_scope", str(e)) from e
        return ("tree" if (tree or is_dir) else "exact"), path

    def _active_claims(self, db, project):
        now = self.clock()
        return db.execute(
            "SELECT * FROM claims WHERE project_id=? AND released_at IS NULL AND expires_at>?" + self._LIVE_OWNER,
            (project, now, now - SESSION_TTL)).fetchall()

    def _delegated(self, db, c, session_id: str, stype: str, path: str) -> bool:
        """An accepted delegate role on claim `c` covers (stype, path): the whole claim, or its sub-scope."""
        r = db.execute("SELECT scope_type, scope FROM claim_roles WHERE claim_id=? AND session_id=? AND"
                       " role='delegate' AND accepted=1", (c["claim_id"], session_id)).fetchone()
        if r is None:
            return False
        outer = (r["scope_type"], r["scope"]) if r["scope"] is not None else (c["scope_type"], c["scope"])
        return scopes.contains(outer[0], outer[1], stype, path)

    def _roles(self, db, claim_id: int, session_id: str) -> set[str]:
        return {r[0] for r in db.execute(              # only roles the grantee accepted count
            "SELECT role FROM claim_roles WHERE claim_id=? AND session_id=? AND accepted=1",
            (claim_id, session_id))}

    @staticmethod
    def _claim_dict(r) -> dict:
        return {"claim": f"C{r['claim_id']}", "claim_id": r["claim_id"], "owner": r["owner_name"],
                "owner_session_id": r["owner_session_id"], "scope_type": r["scope_type"],
                "scope": scopes.display(r["scope_type"], r["scope"]), "note": r["note"],
                "fence": r["fence"], "expires_at": iso(r["expires_at"]),
                "release_on_commit": bool(r["release_on_commit"]), "project": r["project_id"],
                "released": r["released_at"] is not None}

    def claim(self, session: str, scope: str, tree: bool | None = None, note: str = "",
              ttl: int = CLAIM_TTL, release_on_commit: bool = False,
              client_id: str | None = None) -> dict:
        stype, path = self._norm(scope, tree)

        def fn(db):
            me, project = self._access(db, session, None, "participate")
            self._reap(db)
            for c in self._active_claims(db, project):
                if not scopes.overlaps(stype, path, c["scope_type"], c["scope"]):
                    continue
                if c["owner_session_id"] == session:
                    if c["scope_type"] == stype and c["scope"] == path:
                        raise CoordError("already_held", f"you already hold C{c['claim_id']}; "
                                         f"use `coord renew C{c['claim_id']}`", self._claim_dict(c))
                    continue
                if self._delegated(db, c, session, stype, path):
                    continue
                raise CoordError("conflict", f"{scopes.display(stype, path)} overlaps C{c['claim_id']} "
                                 f"({scopes.display(c['scope_type'], c['scope'])}) held by "
                                 f"{c['owner_name']}", {"held_by": self._claim_dict(c)})
            now = self.clock()
            fence = self._next_fence(db)
            cur = db.execute(
                "INSERT INTO claims(project_id, owner_session_id, owner_name, scope_type, scope, note,"
                " claimed_at, expires_at, fence, release_on_commit) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (project, session, me["display_name"], stype, path, note or "", now, now + ttl,
                 fence, int(bool(release_on_commit))))
            row = db.execute("SELECT * FROM claims WHERE claim_id=?", (cur.lastrowid,)).fetchone()
            self._event(db, project, "claim.acquired", session, "claim", cur.lastrowid,
                        scope=scopes.display(stype, path), fence=fence)
            return self._claim_dict(row)
        return self._mutate("claim", client_id, fn)

    def _owned(self, db, session, claim) -> sqlite3.Row:
        cid = parse_id("claim", claim)
        row = db.execute("SELECT * FROM claims WHERE claim_id=?", (cid,)).fetchone()
        if row is None:
            raise CoordError("missing", f"no claim C{cid}")
        if row["owner_session_id"] != session:
            raise CoordError("not_owner", f"C{cid} is owned by {row['owner_name']} "
                             f"(session {row['owner_session_id'][:8]}), not you")
        if row["released_at"] is not None:
            raise CoordError("released", f"C{cid} was already released")
        return row

    def renew(self, session: str, claim: str, ttl: int = CLAIM_TTL) -> dict:
        with self._tx() as db:
            me, _ = self._access(db, session, None, "participate")
            row = self._owned(db, session, claim)
            if row["expires_at"] <= self.clock():
                raise CoordError("expired", f"C{row['claim_id']} expired; claim the scope again "
                                 "(you will get a new fence)")
            db.execute("UPDATE claims SET expires_at=? WHERE claim_id=?",
                       (self.clock() + ttl, row["claim_id"]))
            self._event(db, me["project_id"], "claim.renewed", session, "claim", row["claim_id"])
            return self._claim_dict(db.execute("SELECT * FROM claims WHERE claim_id=?",
                                               (row["claim_id"],)).fetchone())

    def release(self, session: str, claim: str | None = None, all: bool = False) -> dict:
        with self._tx() as db:
            me, _ = self._access(db, session, None, "participate")
            now = self.clock()
            if all:
                ids = [r[0] for r in db.execute(
                    "SELECT claim_id FROM claims WHERE owner_session_id=? AND released_at IS NULL",
                    (session,))]
            else:
                if not claim:
                    raise CoordError("usage", "give a claim id (C12) or --all")
                ids = [self._owned(db, session, claim)["claim_id"]]
            for cid in ids:
                db.execute("UPDATE claims SET released_at=?, released_by=? WHERE claim_id=?",
                           (now, session, cid))
                self._event(db, me["project_id"], "claim.released", session, "claim", cid)
            return {"released": [f"C{i}" for i in ids]}

    def locks(self, project: str | None = None, all: bool = False,
              owner_session: str | None = None, session: str | None = None) -> list[dict]:
        with self._read() as db:
            q, args = "SELECT * FROM claims WHERE 1=1", []
            if project:
                self._view(db, project, session)
                q += " AND project_id=?"; args.append(project)
            else:
                f, fargs = self._view_filter(db, session)
                q += f" AND {f}"; args += list(fargs)
            if not all:
                q += " AND released_at IS NULL AND expires_at>?" + self._LIVE_OWNER
                args += [self.clock(), self.clock() - SESSION_TTL]
            if owner_session:
                q += " AND owner_session_id=?"; args.append(owner_session)
            rows = db.execute(q + " ORDER BY claim_id", args).fetchall()
        return [self._claim_dict(r) for r in rows]

    def fence_check(self, claim: str, fence: int, session: str | None = None) -> dict:
        """Guard a write with the fence you got at claim time: stale leases fail."""
        cid = parse_id("claim", claim)
        with self._read() as db:
            row = db.execute("SELECT * FROM claims WHERE claim_id=?", (cid,)).fetchone()
            if row is None or row["released_at"] is not None or row["expires_at"] <= self.clock():
                raise CoordError("stale_fence", f"C{cid} is no longer active")
            self._view(db, row["project_id"], session)
        if int(fence) != row["fence"]:
            raise CoordError("stale_fence", f"fence {fence} is stale for C{cid} (current {row['fence']})")
        return {"ok": True, "claim": f"C{cid}", "fence": row["fence"]}

    def grant(self, session: str, claim: str, to: str, role: str, scope: str | None = None) -> dict:
        """Give someone a role on your claim. `scope` (delegate only) narrows the delegation to a part
        of the claim: the delegate may then claim and commit inside it, with its own lease and fence."""
        if role not in ROLES:
            raise CoordError("bad_role", f"role must be one of {', '.join(ROLES)}")
        if scope is not None and role != "delegate":
            raise CoordError("bad_args", "--scope only narrows a delegate role")
        with self._tx() as db:
            me, _ = self._access(db, session, None, "participate")
            row = self._owned(db, session, claim)
            target = self._resolve_name(db, to, me["project_id"])
            sub: tuple[str, str] | None = self._norm(scope, None) if scope is not None else None
            if sub is not None and not scopes.contains(row["scope_type"], row["scope"], *sub):
                raise CoordError("bad_scope", f"{scopes.display(*sub)} is not inside C{row['claim_id']} "
                                 f"({scopes.display(row['scope_type'], row['scope'])})")
            consent = role in ROLES_BY_CONSENT        # write duties: an offer the grantee accepts or declines
            cid = row["claim_id"]
            existing = db.execute("SELECT accepted FROM claim_roles WHERE claim_id=? AND session_id=? AND role=?",
                                  (cid, target["session_id"], role)).fetchone()
            where = scopes.display(*sub) if sub is not None else row["scope"]
            if existing is None:
                db.execute("INSERT INTO claim_roles(claim_id, session_id, role, granted_by, granted_at, accepted,"
                           " scope_type, scope) VALUES(?,?,?,?,?,?,?,?)",
                           (cid, target["session_id"], role, session, self.clock(), 0 if consent else 1,
                            *(sub or (None, None))))
                if consent:
                    self._notify(db, me, target["session_id"],
                                 f"[C{cid} {where}] {me['display_name']} offers you the {role} role - "
                                 f"coord role accept C{cid} {role} / coord role decline C{cid} {role} \"why\"",
                                 kind="question", claim_id=cid)
            elif sub is not None:   # re-delegating to the same session: the new sub-scope replaces the old
                db.execute("UPDATE claim_roles SET scope_type=?, scope=? WHERE claim_id=? AND session_id=? AND role=?",
                           (*sub, cid, target["session_id"], role))
            status = "granted" if existing is not None and existing["accepted"] or not consent else "offered"
            self._event(db, me["project_id"], "claim.role_granted" if status == "granted" else "claim.role_offered",
                        session, "claim", cid, to=target["display_name"], role=role)
            return {"claim": f"C{cid}", "to": target["display_name"], "role": role, "status": status,
                    **({"scope": where} if scope is not None else {})}

    def _role_answer(self, session: str, claim: str, role: str, accept: bool, reason: str = "") -> dict:
        with self._tx() as db:
            me, _ = self._access(db, session, None, "participate")
            cid = parse_id("claim", claim)
            r = db.execute("SELECT * FROM claim_roles WHERE claim_id=? AND session_id=? AND role=?",
                           (cid, session, role)).fetchone()
            if r is None:
                raise CoordError("missing", f"nobody offered you the {role} role on C{cid}")
            if accept and r["accepted"]:
                return {"claim": f"C{cid}", "role": role, "status": "granted"}
            if accept:
                db.execute("UPDATE claim_roles SET accepted=1 WHERE claim_id=? AND session_id=? AND role=?",
                           (cid, session, role))
            else:
                db.execute("DELETE FROM claim_roles WHERE claim_id=? AND session_id=? AND role=?", (cid, session, role))
            self._notify(db, me, r["granted_by"],
                         f"[C{cid}] {me['display_name']} {'accepted' if accept else 'declined'} the {role} role"
                         + (f": {reason}" if reason else ""), kind="info" if accept else "warning", claim_id=cid)
            self._event(db, me["project_id"], f"claim.role_{'accepted' if accept else 'declined'}", session,
                        "claim", cid, role=role)
            return {"claim": f"C{cid}", "role": role, "status": "granted" if accept else "declined"}

    def role_accept(self, session: str, claim: str, role: str) -> dict:
        """Accept a coeditor/delegate role offered on someone's claim; it takes effect now."""
        return self._role_answer(session, claim, role, True)

    def role_decline(self, session: str, claim: str, role: str, reason: str = "") -> dict:
        return self._role_answer(session, claim, role, False, reason)

    def revoke(self, session: str, claim: str, to: str, role: str) -> dict:
        with self._tx() as db:
            me, _ = self._access(db, session, None, "participate")
            row = self._owned(db, session, claim)
            target = self._resolve_name(db, to, me["project_id"])
            db.execute("DELETE FROM claim_roles WHERE claim_id=? AND session_id=? AND role=?",
                       (row["claim_id"], target["session_id"], role))
            return {"claim": f"C{row['claim_id']}", "to": target["display_name"], "revoked": role}

    def roles(self, claim: str, session: str | None = None) -> list[dict]:
        cid = parse_id("claim", claim)
        with self._read() as db:
            c = db.execute("SELECT project_id FROM claims WHERE claim_id=?", (cid,)).fetchone()
            if c is not None:
                self._view(db, c["project_id"], session)
            rows = db.execute("SELECT r.role, r.accepted, r.scope_type, r.scope, s.display_name FROM claim_roles r"
                              " JOIN sessions s ON s.session_id=r.session_id WHERE claim_id=?", (cid,)).fetchall()
        return [{"session": r["display_name"], "role": r["role"], "status": "granted" if r["accepted"] else "offered",
                 **({"scope": scopes.display(r["scope_type"], r["scope"])} if r["scope"] is not None else {})}
                for r in rows]

    def ask(self, session: str, claim: str, to: str, body: str, role: str = "advisor",
            kind: str = "question", client_id: str | None = None) -> dict:
        """Ask for help on a claim you keep: grants a non-owner role, never releases."""
        if role not in ROLES:
            raise CoordError("bad_role", f"role must be one of {', '.join(ROLES)}")
        with self._read() as db:
            row = self._owned(db, session, claim)
            if row["expires_at"] <= self.clock():
                raise CoordError("expired", f"C{row['claim_id']} expired")
        out = self.post(session, body, kind=kind, to=to, claim=claim, client_id=client_id)
        if not out.get("replayed"):
            self.grant(session, claim, to, role)
        out.update(claim=f"C{row['claim_id']}", role=role, kept=True)
        return out

    def check(self, session: str, files: list[str]) -> dict:
        """Pre-commit: files claimed by someone else (owner/delegate only may write)."""
        with self._read() as db:
            me, _ = self._access(db, session, None, "view")
            active = self._active_claims(db, me["project_id"])
            conflicts = []
            for f in files:
                try:
                    stype, path = self._norm(f, False)
                except CoordError:
                    continue
                for c in active:
                    if c["owner_session_id"] == session:
                        continue
                    if not scopes.overlaps("exact", path, c["scope_type"], c["scope"]):
                        continue
                    if self._delegated(db, c, session, "exact", path):
                        continue
                    conflicts.append({"file": path, "claim": f"C{c['claim_id']}", "owner": c["owner_name"],
                                      "scope": scopes.display(c["scope_type"], c["scope"])})
        return {"ok": not conflicts, "conflicts": conflicts}

    def post_commit(self, session: str, sha: str, files: list[str]) -> dict:
        with self._tx() as db:
            me, _ = self._access(db, session, None, "participate")
            paths = set()
            for f in files:
                with contextlib.suppress(CoordError):
                    paths.add(self._norm(f, False)[1])
            released = []
            # Exact-scope claims are how you protect a file while you edit it; the commit is that
            # file's natural release point, so every exact claim on a committed file goes - not only
            # ones opted in with --release-on-commit (still honoured, and still the only way to
            # auto-release a tree-scope claim, which this loop does not touch).
            for c in db.execute("SELECT * FROM claims WHERE owner_session_id=? AND released_at IS NULL"
                                " AND scope_type='exact'", (session,)).fetchall():
                if c["scope"] in paths:
                    db.execute("UPDATE claims SET released_at=?, released_by=? WHERE claim_id=?",
                               (self.clock(), session, c["claim_id"]))
                    released.append(f"C{c['claim_id']}")
            routines = self._routines_on_commit(db, me["project_id"], sha, paths)
            self._event(db, me["project_id"], "commit.created", session, "commit", sha,
                        files=sorted(paths), released=released, routines=routines)
            return {"sha": sha, "released": released, "routines_due": routines}
