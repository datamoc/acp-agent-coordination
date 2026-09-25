"""Discussions and consensus: propose, react, computed decisions."""

import json
from typing import NotRequired, TypedDict

from .core import (
    CONSENSUS_RULES,
    DEFAULT_QUORUM,
    SERVER_NAME,
    STANCES,
    SUPPORTING,
    CoordBase,
    CoordError,
    iso,
    parse_id,
    parse_when,
)


class ConsensusDetail(TypedDict):
    rule: str
    quorum: int
    met: bool
    why: list[str]
    deadline: str | None
    participants: list[dict]
    reservations: NotRequired[list[dict]]
    open: NotRequired[bool]
    tally: NotRequired[dict]
    threshold: NotRequired[float]
    quorum_weight: NotRequired[float]
    closed: NotRequired[bool]
    objections: NotRequired[list[dict]]
    crisis: NotRequired[dict]


class ConsensusMixin(CoordBase):
    def discuss(self, session: str, topic: str, claim: str | None = None, participants: list[str] | None = None,
                rule: str = "unanimous", quorum: int | None = None, deadline: str | None = None,
                weights: list[str] | None = None, threshold: float | None = None,
                quorum_weight: float | None = None, owner: str | None = None, domain: str | None = None,
                client_id: str | None = None) -> dict:
        """Open a discussion. `participants` (session names, live now) are the agents whose agreement
        counts, the opener included; without them it is open: the opener plus whoever reacts.
        `rule` decides what consensus means, `quorum` how many participants must take a stance;
        after `deadline` (90m, 48h, 3d or an ISO date) a participant's silence counts as agreement -
        except under `weighted`, where silence and abstention never count as agreement.
        weighted: every rule is fixed now and visible - the electorate (participants), their weights
        (`weights` name=3.0, else the project's weights for `domain`, else 1), `threshold` (share of the
        expressed weight that must support, default 0.5), `quorum_weight` (share of the electorate's weight
        that must take a stance, default 0.5) and the closing date (`deadline`, required).
        advisory: opinions only, the outcome binds nobody. owner: `owner` decides, the stances are advice.
        Invited participants get a direct message."""
        if rule not in CONSENSUS_RULES:
            raise CoordError("bad_rule", f"rule must be one of {', '.join(CONSENSUS_RULES)}")
        q = DEFAULT_QUORUM if quorum is None else int(quorum)
        due = parse_when(deadline, self.clock()) if deadline else None
        if due is not None and due <= self.clock():
            raise CoordError("bad_deadline", "the deadline is already past")
        if q < 1:
            raise CoordError("bad_quorum", "quorum must be at least 1")
        if rule == "weighted":
            if not participants:
                raise CoordError("bad_args", "a weighted vote needs its electorate fixed now (--with a,b,c)")
            if due is None:
                raise CoordError("bad_deadline", "a weighted vote needs its closing date fixed now (--deadline)")
            if threshold is not None and not 0 <= float(threshold) < 1:
                raise CoordError("bad_args", "threshold is a share: 0 <= threshold < 1 (0.5 = more than half)")
            if quorum_weight is not None and not 0 < float(quorum_weight) <= 1:
                raise CoordError("bad_quorum", "quorum_weight is a share of the electorate's weight: 0 < q <= 1")
        if rule == "owner" and not owner:
            raise CoordError("bad_args", "rule owner needs the designated decider (--owner name)")
        explicit: dict[str, float] = {}
        for w in weights or []:
            name, _, val = str(w).partition("=")
            try:
                explicit[name.strip()] = float(val)
            except ValueError as e:
                raise CoordError("bad_args", f"weight {w!r}: use name=1.5") from e
            if explicit[name.strip()] < 0:
                raise CoordError("bad_args", f"weight {w!r} is negative")

        def fn(db):
            me, _ = self._access(db, session, None, "participate")
            return self._discussion_open(db, me, topic, claim, participants, rule, q, due, explicit,
                                         threshold, quorum_weight, owner, domain)
        return self._mutate("discuss", client_id, fn)

    def _discussion_open(self, db, me, topic: str, claim: str | None = None, participants: list[str] | None = None,
                         rule: str = "unanimous", q: int = DEFAULT_QUORUM, due: float | None = None,
                         explicit: dict | None = None, threshold: float | None = None,
                         quorum_weight: float | None = None, owner: str | None = None,
                         domain: str | None = None) -> dict:
        session = me["session_id"]
        cid = parse_id("claim", claim) if claim else None
        invited = []
        for name in dict.fromkeys(n.strip() for n in (participants or []) if n and n.strip()):
            row = self._resolve_name(db, name, me["project_id"])
            if row["session_id"] != session:
                invited.append(row)
        if invited and q > len(invited) + 1 and rule != "weighted":
            raise CoordError("bad_quorum", f"quorum {q} is more than the {len(invited) + 1} participants")
        explicit = explicit or {}
        unknown = set(explicit) - {r["display_name"] for r in [me, *invited]}
        if unknown:
            raise CoordError("bad_args", f"weights for people outside the electorate: {', '.join(sorted(unknown))}")
        if rule == "weighted":
            threshold = 0.5 if threshold is None else float(threshold)
            quorum_weight = 0.5 if quorum_weight is None else float(quorum_weight)
        cur = db.execute("INSERT INTO discussions(project_id, created_by, created_by_name, topic,"
                         " claim_id, created_at, rule, quorum, deadline, threshold, quorum_weight, owner_name, domain)"
                         " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (me["project_id"], session, me["display_name"], topic, cid, self.clock(), rule, q, due,
                          threshold, quorum_weight, owner, domain))
        did = cur.lastrowid
        frozen = {}
        if invited:
            for row in [me, *invited]:
                w = explicit.get(row["display_name"])
                if w is None:
                    pw = db.execute("SELECT weight FROM project_weights WHERE project_id=? AND name=? AND domain IN (?, '')"
                                    " ORDER BY domain='' LIMIT 1", (me["project_id"], row["display_name"], domain or "")).fetchone()
                    w = pw["weight"] if pw else 1.0
                frozen[row["display_name"]] = w
                db.execute("INSERT OR IGNORE INTO discussion_participants(discussion_id, session_id, name, weight)"
                           " VALUES(?,?,?,?)", (did, row["session_id"], row["display_name"], w))
            when = f", deadline {iso(due)}" if due else ""
            how = (f"weighted: threshold {threshold}, quorum {quorum_weight} of the weight, your weight "
                   if rule == "weighted" else f"rule {rule}, quorum {q}")
            for row in invited:
                extra = f"{frozen[row['display_name']]}" if rule == "weighted" else ""
                self._notify(db, me, row["session_id"],
                             f"[D{did}] {me['display_name']} asks for your {'vote' if rule == 'weighted' else 'agreement'}: "
                             f"{topic} ({how}{extra}{when}) - coord discussion D{did}", kind="question")
        m = db.execute("INSERT INTO messages(project_id, from_session_id, from_name, kind, body,"
                       " claim_id, created_at, discussion_id) VALUES(?,?,?,?,?,?,?,?)",
                       (me["project_id"], session, me["display_name"], "question",
                        f"[D{did}] {topic}", cid, self.clock(), did)).lastrowid
        db.execute("UPDATE messages SET thread_id=? WHERE id=?", (m, m))
        db.execute("UPDATE discussions SET message_id=? WHERE id=?", (m, did))
        self._event(db, me["project_id"], "discussion.opened", session, "discussion", did, rule=rule)
        return {"discussion": f"D{did}", "id": did, "message": m, "rule": rule, "quorum": q, "deadline": iso(due),
                "participants": [me["display_name"], *[r["display_name"] for r in invited]] if invited else None,
                **({"weights": frozen, "threshold": threshold, "quorum_weight": quorum_weight} if rule == "weighted" else {}),
                **({"owner": owner} if owner else {})}

    def _identity(self, db, session_id: str) -> str | None:
        """Who a session is, across its restarts: its authenticated principal, else its family."""
        r = db.execute("SELECT principal, family FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        return None if r is None else (r["principal"] or f"family:{r['family']}")

    def _successors(self, db, participants: list[tuple[str, str]], reacted: dict) -> dict:
        """participant session -> a reaction by a later session of the same identity, when the participant
        has not reacted itself (sessions die after 30 min; the agent comes back under a new name). Each
        reaction speaks for one participant at most."""
        used, out = set(reacted) & {sid for sid, _ in participants}, {}
        for sid, _ in participants:
            if sid in reacted:
                continue
            me = self._identity(db, sid)
            for rsid, r in reacted.items():
                if rsid not in used and me is not None and self._identity(db, rsid) == me:
                    out[sid], used = r, used | {rsid}
                    break
        return out

    def _consensus(self, db, d, proposal_id: int | None, deciding: bool) -> ConsensusDetail:
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
            people = [x for x in people if x[0] != SERVER_NAME]    # the server opens reviews, it has no voice
        out = []
        past = d["deadline"] is not None and self.clock() >= d["deadline"]
        later = self._successors(db, people, reacted) if fixed else {}
        weighted = rule == "weighted"
        for sid, name in people:
            if sid in reacted:
                out.append({"name": name, "stance": reacted[sid]["stance"], "implied": False})
            elif sid in later:
                out.append({"name": name, "stance": later[sid]["stance"], "implied": False,
                            "by": later[sid]["author_name"]})
            elif sid == p["author_session_id"] or (deciding and sid == d["created_by"] and not weighted):
                out.append({"name": name, "stance": "support", "implied": True})
            elif past and not weighted:   # silence after the deadline counts as agreement (not in a vote)
                out.append({"name": name, "stance": "support", "implied": True, "silent_past_deadline": True})
            else:
                out.append({"name": name, "stance": None, "implied": False})
        if weighted:
            return self._weighted(db, d, out, reacted, later, people, past)
        def count(st):
            return sum(1 for x in out if x["stance"] == st)
        supporting = sum(1 for x in out if x["stance"] in SUPPORTING)

        def who(*st):
            return ", ".join(x["name"] for x in out if x["stance"] in st)
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
        elif rule == "no-objection":   # silence is consent
            if count("object") or count("need-more-info"):
                why.append(f"objection from {who('object', 'need-more-info')}")
        elif rule == "advisory":
            why = ["advisory: opinions only, the outcome binds nobody"]
        else:   # owner: the designated person decides, the stances are advice
            why = [f"decided by its designated owner {d['owner_name']}; the stances are advice"]
        reservations = []
        for (sid, _), x in zip(people, out, strict=True):
            if x["stance"] != "support-with-reservation":
                continue
            entry = reacted.get(sid) or later.get(sid)
            assert entry is not None   # `out`'s loop above only sets this stance from reacted/later
            reservations.append({"name": x["name"], "comment": entry["comment"]})
        return {"rule": rule, "quorum": quorum, "met": not why, "why": why, "participants": out,
                "reservations": reservations, "open": not fixed, "deadline": iso(d["deadline"])}

    def _weighted(self, db, d, out, reacted, later, people, past) -> ConsensusDetail:
        """A weighted vote: the weights, electorate, threshold, quorum and closing date were fixed when the
        discussion opened. Abstention and silence never count as support; the outcome is final once the
        vote closes (or everyone has voted)."""
        weights = {r["name"]: r["weight"] for r in db.execute(
            "SELECT name, weight FROM discussion_participants WHERE discussion_id=?", (d["id"],))}
        tally = {"for": 0.0, "against": 0.0, "abstain": 0.0, "silent": 0.0}
        for x in out:
            w = weights.get(x["name"], 1.0)
            x["weight"] = w
            st = x["stance"]
            key = "silent" if st is None else "for" if st in SUPPORTING else "abstain" if st == "abstain" else "against"
            tally[key] += w
        total = sum(tally.values())
        expressed = tally["for"] + tally["against"]
        took = expressed + tally["abstain"]
        threshold, qw = d["threshold"] if d["threshold"] is not None else 0.5, d["quorum_weight"] or 0.5
        why = []
        if total and took / total < qw:
            why.append(f"quorum not reached: {took:g} of {total:g} weight took a stance (needs {qw:g} of it)")
        if not expressed or tally["for"] / expressed <= threshold:
            why.append(f"threshold not reached: {tally['for']:g} for, {tally['against']:g} against "
                       f"(needs more than {threshold:g} of the expressed weight)")
        closed = past or tally["silent"] == 0
        if not closed:
            why.append(f"the vote is open until {iso(d['deadline'])}")
        reservations = []
        for (sid, _), x in zip(people, out, strict=True):
            if x["stance"] == "support-with-reservation":
                entry = reacted.get(sid) or later.get(sid)
                reservations.append({"name": x["name"], "comment": entry["comment"] if entry else ""})
        objections = [{"name": x["name"], "comment": (reacted.get(sid) or later.get(sid) or {"comment": ""})["comment"]}
                      for (sid, _), x in zip(people, out, strict=True) if x["stance"] in ("object", "need-more-info")]
        return {"rule": "weighted", "quorum": d["quorum"], "met": not why, "why": why, "participants": out,
                "reservations": reservations, "open": False, "deadline": iso(d["deadline"]),
                "tally": tally, "threshold": threshold, "quorum_weight": qw, "closed": closed,
                "objections": objections}

    def propose(self, session: str, discussion: str, body: str, supersedes: str | None = None,
                client_id: str | None = None) -> dict:
        """A proposal; `supersedes` replaces one of yours (or, as the opener, anyone's) in the same
        discussion - the old one can no longer be reacted to or decided, its stances do not carry over."""
        def fn(db):
            did = parse_id("discussion", discussion)
            d = db.execute("SELECT * FROM discussions WHERE id=?", (did,)).fetchone()
            if d is None:
                raise CoordError("missing", f"no discussion D{did}")
            me, _ = self._access(db, session, d["project_id"], "participate")
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
            pid = parse_id("proposal", proposal)
            p = db.execute("SELECT p.*, d.status AS dstatus, d.project_id FROM proposals p JOIN"
                           " discussions d ON d.id=p.discussion_id WHERE p.id=?", (pid,)).fetchone()
            if p is None:
                raise CoordError("missing", f"no proposal P{pid}")
            me, _ = self._access(db, session, p["project_id"], "participate")
            if p["dstatus"] != "open":
                raise CoordError("closed", "discussion is closed")
            if p["status"] == "superseded":
                raise CoordError("superseded", f"P{pid} was superseded - react to its replacement")
            d = db.execute("SELECT * FROM discussions WHERE id=?", (p["discussion_id"],)).fetchone()
            before = self._consensus(db, d, pid, deciding=True)["met"]      # as the opener would decide
            db.execute("INSERT INTO reactions VALUES(?,?,?,?,?,?) ON CONFLICT(proposal_id,"
                       " author_session_id) DO UPDATE SET stance=excluded.stance,"
                       " comment=excluded.comment, created_at=excluded.created_at",
                       (pid, session, me["display_name"], stance, comment, self.clock()))
            self._event(db, p["project_id"], "proposal.reacted", session, "proposal", pid, stance=stance)
            if not before and self._consensus(db, d, pid, deciding=True)["met"]:
                note = (f"[P{pid} on D{d['id']}] has consensus: {p['body'][:120]} - record what it says (a strategy "
                        f"entry, a routine, a task...), then coord decide D{d['id']} \"...\" --proposal P{pid}")
                for target in dict.fromkeys((p["author_session_id"], d["created_by"])):
                    self._notify(db, me, target, note, kind="decision")
            return {"proposal": f"P{pid}", "stance": stance}

    def discussion(self, discussion: str, session: str | None = None) -> dict:
        did = parse_id("discussion", discussion)
        with self._read() as db:
            d = db.execute("SELECT * FROM discussions WHERE id=?", (did,)).fetchone()
            if d is None:
                raise CoordError("missing", f"no discussion D{did}")
            self._view(db, d["project_id"], session)
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
            weights = {r["name"]: r["weight"] for r in db.execute(
                "SELECT name, weight FROM discussion_participants WHERE discussion_id=?", (did,))} \
                if d["rule"] == "weighted" else {}
        return {"discussion": f"D{did}", "topic": d["topic"], "status": d["status"],
                "created_by": d["created_by_name"], "claim": f"C{d['claim_id']}" if d["claim_id"] else None,
                "thread": d["message_id"], "proposals": props, "decision": d["decision"],
                "consensus": None if d["consensus"] is None else bool(d["consensus"]),
                "rule": d["rule"], "quorum": d["quorum"], "participants": fixed or None,
                "deadline": iso(d["deadline"]), "owner": d["owner_name"], "domain": d["domain"],
                "weights": weights or None, "threshold": d["threshold"], "quorum_weight": d["quorum_weight"],
                "crisis": f"A{d['mandate_id']}" if d["mandate_id"] else None,
                "consensus_detail": json.loads(d["consensus_detail"]) if d["consensus_detail"] else None,
                "decision_reason": d["decision_reason"],
                "decided_by": d["decided_by"], "decided_at": iso(d["decided_at"]),
                "decision_document": f"DOC{d['decision_document_id']}" if d["decision_document_id"] else None}

    def discussions(self, project: str | None = None, status: str = "open",
                    session: str | None = None) -> list[dict]:
        with self._read() as db:
            q, a = "SELECT * FROM discussions WHERE status=?", [status]
            if project:
                self._view(db, project, session)
                q += " AND project_id=?"; a.append(project)
            else:
                f, fargs = self._view_filter(db, session)
                q += f" AND {f}"; a += list(fargs)
            rows = db.execute(q + " ORDER BY id", a).fetchall()
        return [{"discussion": f"D{r['id']}", "topic": r["topic"], "by": r["created_by_name"]} for r in rows]

    def decide(self, session: str, discussion: str, decision: str, proposal: str | None = None,
               consensus: bool = True, reason: str = "", crisis: bool = False) -> dict:
        """Close a discussion. Consensus is computed from the participants' stances and the rule.

        - consensus reached: the opener or any participant may decide;
        - not reached: refused (`no_consensus`, with who objected or stayed silent) - only the
          opener may still decide, explicitly: consensus=False (--no-consensus) with a `reason`;
        - consensus=False always needs a reason and can only lower the record;
        - rule advisory: the opener records the outcome, never as consensus; rule owner: only the
          designated owner decides, the stances stay on record as advice;
        - crisis=True: the holder of an active crisis mandate (power `decide`) arbitrates, with a reason.
          The record keeps what the normal rule gives and every objection, and says it is a crisis
          arbitration - never a consensus.
        Every participant gets a direct message with the outcome."""
        with self._tx() as db:
            did = parse_id("discussion", discussion)
            d = db.execute("SELECT * FROM discussions WHERE id=?", (did,)).fetchone()
            if d is None:
                raise CoordError("missing", f"no discussion D{did}")
            me, _ = self._access(db, session, d["project_id"], "decide")
            if d["status"] != "open":
                raise CoordError("closed", f"D{did} is already {d['status']}")
            pids = [parse_id("proposal", x) for x in str(proposal).split(",") if x.strip()] if proposal else []
            for q in pids:
                row = db.execute("SELECT status FROM proposals WHERE id=? AND discussion_id=?", (q, did)).fetchone()
                if row is None:
                    raise CoordError("missing", f"P{q} is not part of D{did}")
                if row["status"] == "superseded":
                    raise CoordError("superseded", f"P{q} was superseded - decide on its replacement")
            details = {q: self._consensus(db, d, q, deciding=d["created_by"] == session) for q in pids}
            detail = self._consensus(db, d, None, deciding=d["created_by"] == session) if not pids else details[pids[0]]
            if len(pids) > 1:   # every accepted proposal needs its own consensus
                lacking = [f"P{q}: {'; '.join(x['why'])}" for q, x in details.items() if not x["met"]]
                detail = {**detail, "met": not lacking, "why": lacking}
            opener = d["created_by"] == session or self._identity(db, session) == self._identity(db, d["created_by"])
            participant = any(x["name"] == me["display_name"] or x.get("by") == me["display_name"]
                              for x in detail["participants"])
            mandate, crisis_note = None, {}
            if crisis:
                mandate = self._mandate(db, d["project_id"], me["display_name"], "decide")
                if mandate is None:
                    raise CoordError("forbidden", f"{me['display_name']} holds no active crisis mandate with the "
                                     f"power to decide in {d['project_id']}")
                if not reason.strip():
                    raise CoordError("reason_required", "a crisis arbitration needs its reason (--reason)")
                consensus = False
                crisis_note = {"mandate": f"A{mandate['id']}", "holder": mandate["holder"],
                               "normal_outcome": "met" if detail["met"] else "not met", "normal_why": list(detail["why"])}
                detail = {**detail, "crisis": crisis_note}
            elif d["rule"] == "owner":
                if me["display_name"] != d["owner_name"]:
                    raise CoordError("forbidden", f"D{did} is decided by its designated owner {d['owner_name']}")
                consensus = False
            elif d["rule"] == "advisory":
                if not opener:
                    raise CoordError("forbidden", f"only {d['created_by_name']} (who opened the advisory D{did}) "
                                     "records its outcome")
                consensus = False
            if not consensus and not reason.strip() and d["rule"] not in ("owner", "advisory"):
                raise CoordError("reason_required", "deciding without consensus needs a reason (--no-consensus \"why\")")
            if mandate is None and d["rule"] not in ("owner", "advisory") and not opener:
                if not participant:
                    raise CoordError("forbidden", f"only {d['created_by_name']} or a participant of D{did} can decide")
                if not (detail["met"] and consensus):
                    raise CoordError("forbidden", f"without consensus only {d['created_by_name']} (who opened D{did}) "
                                     "can decide", {"why": detail["why"]})
            if consensus and not detail["met"] and mandate is None:
                raise CoordError("no_consensus", f"D{did} has no consensus: {'; '.join(detail['why'])} - wait for the "
                                 "stances, or decide explicitly with --no-consensus \"reason\"", dict(detail))
            if pids:
                marks = ",".join("?" * len(pids))
                db.execute(f"UPDATE proposals SET status=CASE WHEN id IN ({marks}) THEN 'accepted' ELSE 'rejected' END"
                           " WHERE discussion_id=? AND status!='superseded'", (*pids, did))
            now = self.clock()
            reached = detail["met"] and bool(consensus)
            if mandate is not None:
                detail["why"] = [f"crisis arbitration by {me['display_name']} under mandate A{mandate['id']} - not a consensus"]
            elif d["rule"] == "owner":
                detail["why"] = [f"decided by its designated owner {me['display_name']}; the stances are advice"]
            elif detail["met"] and not consensus:
                detail["why"] = ["the decider recorded no consensus"]
            def stance(x):
                return (x["stance"] or "no stance") + (" (implied)" if x["implied"] else "")
            content = (f"# Decision: {d['topic']}\n\n{decision}\n\n"
                       f"- discussion: D{did}\n- accepted proposal{'s' if len(pids) > 1 else ''}: "
                       f"{', '.join(f'P{q}' for q in pids) or 'none'}\n"
                       f"- consensus: {'yes' if reached else 'no'} (rule: {detail['rule']}, quorum: {detail['quorum']}"
                       f"{', open discussion' if detail.get('open') else ''})\n"
                       + "".join(f"  - {x['name']}: {stance(x)}" + (f" (weight {x['weight']:g})" if "weight" in x else "")
                                 + "\n" for x in detail["participants"])
                       + "".join(f"  - objection from {o['name']}: {o['comment'] or '-'}\n"
                                 for o in self._objections(db, pids))
                       + (f"- CRISIS ARBITRATION under mandate A{mandate['id']} (holder {mandate['holder']}, until "
                          f"{iso(mandate['expires_at'])}) - not a consensus; the normal rule gives: "
                          f"{crisis_note['normal_outcome']}"
                          + (f" ({'; '.join(crisis_note['normal_why'])})" if crisis_note['normal_why'] else "")
                          + "\n" if mandate is not None else "")
                       + "".join(f"  - why not: {w}\n" for w in ([] if reached else detail["why"]))
                       + (f"- reason: {reason}\n" if reason else "")
                       + f"- decided by: {me['display_name']}\n")
            doc = self._doc_insert(db, me, f"Decision D{did}: {d['topic']}", "decision", content)
            db.execute("UPDATE documents SET status='final' WHERE id=?", (doc,))
            db.execute("UPDATE discussions SET status='decided', decision=?, consensus=?, decided_by=?,"
                       " decided_at=?, decision_document_id=?, decision_reason=?, consensus_detail=?, mandate_id=?"
                       " WHERE id=?",
                       (decision, int(reached), me["display_name"], now, doc, reason or None,
                        json.dumps(detail), mandate["id"] if mandate is not None else None, did))
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
            self._event(db, d["project_id"], "discussion.decided", session, "discussion", did, document=doc,
                        **({"crisis": f"A{mandate['id']}"} if mandate is not None else {}))
            return {"discussion": f"D{did}", "document": f"DOC{doc}", "consensus": reached,
                    **({"crisis": f"A{mandate['id']}"} if mandate is not None else {}),
                    **({} if reached else {"why": detail["why"]})}

    @staticmethod
    def _objections(db, pids: list[int]) -> list[dict]:
        if not pids:
            return []
        return [{"name": r["author_name"], "comment": r["comment"]} for r in db.execute(
            f"SELECT author_name, comment FROM reactions WHERE stance IN ('object','need-more-info') AND proposal_id IN"
            f" ({','.join('?' * len(pids))}) ORDER BY created_at", tuple(pids))]
