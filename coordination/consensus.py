"""Discussions and consensus: propose, react, computed decisions."""

import json

from .core import CONSENSUS_RULES, DEFAULT_QUORUM, STANCES, SUPPORTING, CoordError, iso, parse_id, parse_when


class ConsensusMixin:
    def discuss(self, session: str, topic: str, claim: str | None = None, participants: list[str] | None = None,
                rule: str = "unanimous", quorum: int | None = None, deadline: str | None = None,
                client_id: str | None = None) -> dict:
        """Open a discussion. `participants` (session names, live now) are the agents whose agreement
        counts, the opener included; without them it is open: the opener plus whoever reacts.
        `rule` decides what consensus means, `quorum` how many participants must take a stance;
        after `deadline` (90m, 48h, 3d or an ISO date) a participant's silence counts as agreement.
        Invited participants get a direct message."""
        if rule not in CONSENSUS_RULES:
            raise CoordError("bad_rule", f"rule must be one of {', '.join(CONSENSUS_RULES)}")
        q = DEFAULT_QUORUM if quorum is None else int(quorum)
        due = parse_when(deadline, self.clock()) if deadline else None
        if due is not None and due <= self.clock():
            raise CoordError("bad_deadline", "the deadline is already past")
        if q < 1:
            raise CoordError("bad_quorum", "quorum must be at least 1")

        def fn(db):
            me = self._session(db, session)
            cid = parse_id("claim", claim) if claim else None
            invited = []
            for name in dict.fromkeys(n.strip() for n in (participants or []) if n and n.strip()):
                row = self._resolve_name(db, name, me["project_id"])
                if row["session_id"] != session:
                    invited.append(row)
            if invited and q > len(invited) + 1:
                raise CoordError("bad_quorum", f"quorum {q} is more than the {len(invited) + 1} participants")
            cur = db.execute("INSERT INTO discussions(project_id, created_by, created_by_name, topic,"
                             " claim_id, created_at, rule, quorum, deadline) VALUES(?,?,?,?,?,?,?,?,?)",
                             (me["project_id"], session, me["display_name"], topic, cid, self.clock(), rule, q, due))
            did = cur.lastrowid
            if invited:
                for row in [me, *invited]:
                    db.execute("INSERT OR IGNORE INTO discussion_participants VALUES(?,?,?)",
                               (did, row["session_id"], row["display_name"]))
                when = f", deadline {iso(due)}" if due else ""
                for row in invited:
                    self._notify(db, me, row["session_id"],
                                 f"[D{did}] {me['display_name']} asks for your agreement: {topic} "
                                 f"(rule {rule}, quorum {q}{when}) - coord discussion D{did}", kind="question")
            m = db.execute("INSERT INTO messages(project_id, from_session_id, from_name, kind, body,"
                           " claim_id, created_at) VALUES(?,?,?,?,?,?,?)",
                           (me["project_id"], session, me["display_name"], "question",
                            f"[D{did}] {topic}", cid, self.clock())).lastrowid
            db.execute("UPDATE messages SET thread_id=? WHERE id=?", (m, m))
            db.execute("UPDATE discussions SET message_id=? WHERE id=?", (m, did))
            self._event(db, me["project_id"], "discussion.opened", session, "discussion", did)
            return {"discussion": f"D{did}", "id": did, "message": m, "rule": rule, "quorum": q, "deadline": iso(due),
                    "participants": [me["display_name"], *[r["display_name"] for r in invited]] if invited else None}
        return self._mutate("discuss", client_id, fn)

    def _consensus(self, db, d, proposal_id: int | None, deciding: bool) -> dict:
        """Evaluate the discussion's rule on one proposal. Stances: a participant's own reaction; else
        the proposal's author supports it and, when `deciding`, so does the decider (both `implied`)."""
        rule, quorum = d["rule"] or "unanimous", d["quorum"] or DEFAULT_QUORUM
        fixed = db.execute("SELECT session_id, name FROM discussion_participants WHERE discussion_id=?"
                           " ORDER BY rowid", (d["id"],)).fetchall()
        if proposal_id is None:
            return {"rule": rule, "quorum": quorum, "met": False, "why": ["no proposal was chosen"], "deadline": iso(d["deadline"]),
                    "participants": [{"name": r["name"], "stance": None, "implied": False} for r in fixed]}
        p = db.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        reacted = {r["author_session_id"]: r for r in db.execute(
            "SELECT * FROM reactions WHERE proposal_id=? ORDER BY created_at", (proposal_id,))}
        if fixed:
            people = [(r["session_id"], r["name"]) for r in fixed]
        else:   # open discussion: the opener and whoever took a stance (author included)
            people = [(d["created_by"], d["created_by_name"])]
            for sid, name in [(p["author_session_id"], p["author_name"]),
                              *[(sid, r["author_name"]) for sid, r in reacted.items()]]:
                if sid not in dict(people):
                    people.append((sid, name))
        out = []
        past = d["deadline"] is not None and self.clock() >= d["deadline"]
        for sid, name in people:
            if sid in reacted:
                out.append({"name": name, "stance": reacted[sid]["stance"], "implied": False})
            elif sid == p["author_session_id"] or (deciding and sid == d["created_by"]):
                out.append({"name": name, "stance": "support", "implied": True})
            elif past:   # silence after the deadline counts as agreement
                out.append({"name": name, "stance": "support", "implied": True, "silent_past_deadline": True})
            else:
                out.append({"name": name, "stance": None, "implied": False})
        count = lambda st: sum(1 for x in out if x["stance"] == st)
        supporting = sum(1 for x in out if x["stance"] in SUPPORTING)
        who = lambda *st: ", ".join(x["name"] for x in out if x["stance"] in st)
        why, n = [], len(out)
        responded = sum(1 for x in out if x["stance"] is not None)
        if responded < quorum:
            why.append(f"quorum not reached: {responded} of {quorum} participants took a stance")
        if rule == "unanimous":
            if count("object") or count("need-more-info"):
                why.append(f"not unanimous: {who('object', 'need-more-info')} did not agree")
            if count(None):
                why.append(f"not unanimous: no stance from {who(None)}")
            if not supporting:
                why.append("nobody supports it")
        elif rule == "majority":
            if supporting * 2 <= n:
                why.append(f"no majority: {supporting} of {n} participants support it")
        else:   # no-objection: silence is consent
            if count("object") or count("need-more-info"):
                why.append(f"objection from {who('object', 'need-more-info')}")
        reservations = [{"name": x["name"], "comment": reacted[sid]["comment"]}
                        for (sid, _), x in zip(people, out) if x["stance"] == "support-with-reservation"]
        return {"rule": rule, "quorum": quorum, "met": not why, "why": why, "participants": out,
                "reservations": reservations, "open": not fixed, "deadline": iso(d["deadline"])}

    def propose(self, session: str, discussion: str, body: str, supersedes: str | None = None,
                client_id: str | None = None) -> dict:
        """A proposal; `supersedes` replaces one of yours (or, as the opener, anyone's) in the same
        discussion - the old one can no longer be reacted to or decided, its stances do not carry over."""
        def fn(db):
            me = self._session(db, session)
            did = parse_id("discussion", discussion)
            d = db.execute("SELECT * FROM discussions WHERE id=?", (did,)).fetchone()
            if d is None:
                raise CoordError("missing", f"no discussion D{did}")
            if d["status"] != "open":
                raise CoordError("closed", f"D{did} is {d['status']}")
            old = None
            if supersedes:
                oid = parse_id("proposal", supersedes)
                old = db.execute("SELECT * FROM proposals WHERE id=? AND discussion_id=?", (oid, did)).fetchone()
                if old is None:
                    raise CoordError("missing", f"P{oid} is not part of D{did}")
                if old["status"] != "open":
                    raise CoordError("superseded", f"P{oid} is already {old['status']}")
                if old["author_session_id"] != session and d["created_by"] != session:
                    raise CoordError("forbidden", f"only {old['author_name']} (its author) or "
                                     f"{d['created_by_name']} (who opened D{did}) can supersede P{oid}")
            pid = db.execute("INSERT INTO proposals(discussion_id, author_session_id, author_name,"
                             " body, created_at, supersedes_id) VALUES(?,?,?,?,?,?)",
                             (did, session, me["display_name"], body, self.clock(),
                              old["id"] if old else None)).lastrowid
            if old is not None:
                db.execute("UPDATE proposals SET status='superseded' WHERE id=?", (old["id"],))
            m = db.execute("INSERT INTO messages(project_id, from_session_id, from_name, kind, body,"
                           " thread_id, reply_to, created_at) VALUES(?,?,?,?,?,?,?,?)",
                           (d["project_id"], session, me["display_name"], "proposal",
                            f"[P{pid} on D{did}" + (f", supersedes P{old['id']}" if old else "") + f"] {body}",
                            d["message_id"], d["message_id"],
                            self.clock())).lastrowid
            self._event(db, d["project_id"], "proposal.created", session, "proposal", pid)
            return {"proposal": f"P{pid}", "id": pid, "message": m,
                    **({"supersedes": f"P{old['id']}"} if old else {})}
        return self._mutate("propose", client_id, fn)

    def react(self, session: str, proposal: str, stance: str, comment: str = "") -> dict:
        if stance not in STANCES:
            raise CoordError("bad_stance", f"stance must be one of {', '.join(STANCES)}")
        if stance == "object" and not comment.strip():
            raise CoordError("comment_required", "an objection needs its reason: react P.. object \"why\"")
        with self._tx() as db:
            me = self._session(db, session)
            pid = parse_id("proposal", proposal)
            p = db.execute("SELECT p.*, d.status AS dstatus, d.project_id FROM proposals p JOIN"
                           " discussions d ON d.id=p.discussion_id WHERE p.id=?", (pid,)).fetchone()
            if p is None:
                raise CoordError("missing", f"no proposal P{pid}")
            if p["dstatus"] != "open":
                raise CoordError("closed", "discussion is closed")
            if p["status"] == "superseded":
                raise CoordError("superseded", f"P{pid} was superseded - react to its replacement")
            db.execute("INSERT INTO reactions VALUES(?,?,?,?,?,?) ON CONFLICT(proposal_id,"
                       " author_session_id) DO UPDATE SET stance=excluded.stance,"
                       " comment=excluded.comment, created_at=excluded.created_at",
                       (pid, session, me["display_name"], stance, comment, self.clock()))
            self._event(db, p["project_id"], "proposal.reacted", session, "proposal", pid, stance=stance)
            return {"proposal": f"P{pid}", "stance": stance}

    def discussion(self, discussion: str) -> dict:
        did = parse_id("discussion", discussion)
        with self._read() as db:
            d = db.execute("SELECT * FROM discussions WHERE id=?", (did,)).fetchone()
            if d is None:
                raise CoordError("missing", f"no discussion D{did}")
            props = []
            for p in db.execute("SELECT * FROM proposals WHERE discussion_id=? ORDER BY id", (did,)):
                rs = db.execute("SELECT * FROM reactions WHERE proposal_id=? ORDER BY created_at",
                                (p["id"],)).fetchall()
                tally = {s: 0 for s in STANCES}
                for r in rs:
                    tally[r["stance"]] += 1
                props.append({"proposal": f"P{p['id']}", "author": p["author_name"], "body": p["body"],
                              "status": p["status"], "tally": tally,
                              "supersedes": f"P{p['supersedes_id']}" if p["supersedes_id"] else None,
                              "reactions": [{"by": r["author_name"], "stance": r["stance"],
                                             "comment": r["comment"]} for r in rs],
                              "consensus": self._consensus(db, d, p["id"], deciding=True)})
            fixed = [r["name"] for r in db.execute("SELECT name FROM discussion_participants WHERE"
                                                   " discussion_id=? ORDER BY rowid", (did,))]
        return {"discussion": f"D{did}", "topic": d["topic"], "status": d["status"],
                "created_by": d["created_by_name"], "claim": f"C{d['claim_id']}" if d["claim_id"] else None,
                "thread": d["message_id"], "proposals": props, "decision": d["decision"],
                "consensus": None if d["consensus"] is None else bool(d["consensus"]),
                "rule": d["rule"], "quorum": d["quorum"], "participants": fixed or None,
                "consensus_detail": json.loads(d["consensus_detail"]) if d["consensus_detail"] else None,
                "decision_reason": d["decision_reason"],
                "decided_by": d["decided_by"], "decided_at": iso(d["decided_at"]),
                "decision_document": f"DOC{d['decision_document_id']}" if d["decision_document_id"] else None}

    def discussions(self, project: str | None = None, status: str = "open") -> list[dict]:
        with self._read() as db:
            q, a = "SELECT * FROM discussions WHERE status=?", [status]
            if project:
                q += " AND project_id=?"; a.append(project)
            rows = db.execute(q + " ORDER BY id", a).fetchall()
        return [{"discussion": f"D{r['id']}", "topic": r["topic"], "by": r["created_by_name"]} for r in rows]

    def decide(self, session: str, discussion: str, decision: str, proposal: str | None = None,
               consensus: bool = True, reason: str = "") -> dict:
        """Close a discussion. Consensus is computed from the participants' stances and the rule.

        - consensus reached: the opener or any participant may decide;
        - not reached: refused (`no_consensus`, with who objected or stayed silent) - only the
          opener may still decide, explicitly: consensus=False (--no-consensus) with a `reason`;
        - consensus=False always needs a reason and can only lower the record.
        Every participant gets a direct message with the outcome."""
        with self._tx() as db:
            me = self._session(db, session)
            did = parse_id("discussion", discussion)
            d = db.execute("SELECT * FROM discussions WHERE id=?", (did,)).fetchone()
            if d is None:
                raise CoordError("missing", f"no discussion D{did}")
            if d["status"] != "open":
                raise CoordError("closed", f"D{did} is already {d['status']}")
            pid = parse_id("proposal", proposal) if proposal else None
            if pid is not None:
                row = db.execute("SELECT status FROM proposals WHERE id=? AND discussion_id=?", (pid, did)).fetchone()
                if row is None:
                    raise CoordError("missing", f"P{pid} is not part of D{did}")
                if row["status"] == "superseded":
                    raise CoordError("superseded", f"P{pid} was superseded - decide on its replacement")
            detail = self._consensus(db, d, pid, deciding=d["created_by"] == session)
            opener = d["created_by"] == session
            participant = any(x["name"] == me["display_name"] for x in detail["participants"])
            if not consensus and not reason.strip():
                raise CoordError("reason_required", "deciding without consensus needs a reason (--no-consensus \"why\")")
            if not opener:
                if not participant:
                    raise CoordError("forbidden", f"only {d['created_by_name']} or a participant of D{did} can decide")
                if not (detail["met"] and consensus):
                    raise CoordError("forbidden", f"without consensus only {d['created_by_name']} (who opened D{did}) "
                                     "can decide", {"why": detail["why"]})
            if consensus and not detail["met"]:
                raise CoordError("no_consensus", f"D{did} has no consensus: {'; '.join(detail['why'])} - wait for the "
                                 "stances, or decide explicitly with --no-consensus \"reason\"", detail)
            if pid is not None:
                db.execute("UPDATE proposals SET status=CASE WHEN id=? THEN 'accepted' ELSE 'rejected' END"
                           " WHERE discussion_id=? AND status!='superseded'", (pid, did))
            now = self.clock()
            reached = detail["met"] and bool(consensus)
            if detail["met"] and not consensus:
                detail["why"] = ["the decider recorded no consensus"]
            stance = lambda x: (x["stance"] or "no stance") + (" (implied)" if x["implied"] else "")
            content = (f"# Decision: {d['topic']}\n\n{decision}\n\n"
                       f"- discussion: D{did}\n- accepted proposal: {f'P{pid}' if pid else 'none'}\n"
                       f"- consensus: {'yes' if reached else 'no'} (rule: {detail['rule']}, quorum: {detail['quorum']}"
                       f"{', open discussion' if detail.get('open') else ''})\n"
                       + "".join(f"  - {x['name']}: {stance(x)}\n" for x in detail["participants"])
                       + "".join(f"  - why not: {w}\n" for w in ([] if reached else detail["why"]))
                       + (f"- reason: {reason}\n" if reason else "")
                       + f"- decided by: {me['display_name']}\n")
            doc = self._doc_insert(db, me, f"Decision D{did}: {d['topic']}", "decision", content)
            db.execute("UPDATE documents SET status='final' WHERE id=?", (doc,))
            db.execute("UPDATE discussions SET status='decided', decision=?, consensus=?, decided_by=?,"
                       " decided_at=?, decision_document_id=?, decision_reason=?, consensus_detail=? WHERE id=?",
                       (decision, int(reached), me["display_name"], now, doc, reason or None,
                        json.dumps(detail), did))
            db.execute("INSERT INTO messages(project_id, from_session_id, from_name, kind, body, thread_id,"
                       " reply_to, created_at) VALUES(?,?,?,?,?,?,?,?)",
                       (d["project_id"], session, me["display_name"], "decision",
                        f"[D{did} decided -> DOC{doc}] {decision}", d["message_id"], d["message_id"], now))
            fixed = [r["session_id"] for r in db.execute("SELECT session_id FROM discussion_participants"
                                                          " WHERE discussion_id=?", (did,))]
            others = fixed or [r["session_id"] for r in db.execute(
                "SELECT DISTINCT r.author_session_id AS session_id FROM reactions r JOIN proposals p ON"
                " p.id=r.proposal_id WHERE p.discussion_id=?", (did,))] + [d["created_by"]]
            for sid in dict.fromkeys(others):
                self._notify(db, me, sid, f"[D{did} decided -> DOC{doc}] {decision} "
                             f"(consensus: {'yes' if reached else 'no'})", kind="decision")
            self._event(db, d["project_id"], "discussion.decided", session, "discussion", did, document=doc)
            return {"discussion": f"D{did}", "document": f"DOC{doc}", "consensus": reached,
                    **({} if reached else {"why": detail["why"]})}
