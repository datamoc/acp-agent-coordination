"""Reviewing a source: comments on a document, and candidates drawn from a note or a conversation.

An imported note (or a chat message) is a source, not an order. Anyone taking part may comment on
it (citing a passage) and propose candidates from it - a task, a decision to debate, a memory
entry, a question, a summary - each citing the exact passage it comes from and saying what that
passage is: a fact reported, a hypothesis, an opinion, or a decision already taken elsewhere.
Only an explicit review by someone with the decide right turns a candidate into official state;
rejections and corrections stay on record, and every promoted object links back to its passage.
Nothing is ever executed from the text itself."""

import re

from .core import MEMORY_KINDS, SUGGESTION_NATURES, SUGGESTION_TARGETS, CoordBase, CoordError, iso, parse_id


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


class ReviewMixin(CoordBase):
    # --- comments ------------------------------------------------------------------------
    def doc_comment(self, session: str, document: str, body: str, quote: str | None = None) -> dict:
        """Comment on a document, optionally on a passage (`quote`, found verbatim in it)."""
        if not (body or "").strip():
            raise CoordError("empty", "empty comment")
        with self._tx() as db:
            d = db.execute("SELECT * FROM documents WHERE id=?", (parse_id("document", document),)).fetchone()
            if d is None:
                raise CoordError("missing", f"no document {document}")
            me, project = self._access(db, session, d["project_id"], "participate")
            self._doc_gate(db, d, session)
            if quote and _squash(quote) not in _squash(d["content"]):
                raise CoordError("quote_not_found", f"the passage is not in DOC{d['id']} r{d['revision']}")
            cid = db.execute("INSERT INTO document_comments(document_id, revision, author_session_id, author_name,"
                             " quote, body, created_at) VALUES(?,?,?,?,?,?,?)",
                             (d["id"], d["revision"], session, me["display_name"], quote or None, body.strip(),
                              self.clock())).lastrowid
            self._event(db, project, "document.commented", session, "document", d["id"], comment=f"K{cid}")
            return {"comment": f"K{cid}", "document": f"DOC{d['id']}", "revision": d["revision"]}

    def doc_comments(self, document: str, session: str | None = None) -> list[dict]:
        with self._read() as db:
            d = db.execute("SELECT * FROM documents WHERE id=?", (parse_id("document", document),)).fetchone()
            if d is None:
                raise CoordError("missing", f"no document {document}")
            self._doc_gate(db, d, session)
            return [{"comment": f"K{c['id']}", "by": c["author_name"], "revision": c["revision"], "quote": c["quote"],
                     "body": c["body"], "at": iso(c["created_at"])}
                    for c in db.execute("SELECT * FROM document_comments WHERE document_id=? ORDER BY id", (d["id"],))]

    def doc_reviewed(self, session: str, document: str, note: str = "") -> dict:
        """Close the review of an imported note (the decide right): its status becomes `reviewed`; what
        was promoted from it and what was rejected stay linked to it."""
        with self._tx() as db:
            d = db.execute("SELECT * FROM documents WHERE id=?", (parse_id("document", document),)).fetchone()
            if d is None:
                raise CoordError("missing", f"no document {document}")
            me, project = self._access(db, session, d["project_id"], "decide")
            self._doc_gate(db, d, session)
            if d["status"] != "imported":
                raise CoordError("bad_args", f"DOC{d['id']} is {d['status']}, not an imported note pending review")
            pending = [f"S{r[0]}" for r in db.execute("SELECT id FROM suggestions WHERE source_type='document' AND"
                                                       " source_id=? AND status='proposed'", (d["id"],))]
            if pending:
                raise CoordError("pending", f"candidates still wait for review: {', '.join(pending)}", {"pending": pending})
            db.execute("UPDATE documents SET status='reviewed', updated_at=? WHERE id=?", (self.clock(), d["id"]))
            self._event(db, project, "document.reviewed", session, "document", d["id"], note=note or None)
            return {"document": f"DOC{d['id']}", "status": "reviewed"}

    # --- candidates -----------------------------------------------------------------------
    def _source(self, db, source: str, session: str):
        """(type, row, text, revision) for `DOC3` or `#12` / `12`, with the caller's read rights checked."""
        s = str(source).strip()
        if s.upper().startswith("DOC"):
            d = db.execute("SELECT * FROM documents WHERE id=?", (parse_id("document", s),)).fetchone()
            if d is None:
                raise CoordError("missing", f"no document {s}")
            self._doc_gate(db, d, session)
            return "document", d, d["content"], d["revision"]
        m = db.execute("SELECT * FROM messages WHERE id=?", (int(s.lstrip("#")),)).fetchone()
        if m is None:
            raise CoordError("missing", f"no message {s}")
        vis, vargs = self._visible(session)
        if not db.execute(f"SELECT 1 FROM messages WHERE id=? AND {vis}", (m["id"], *vargs)).fetchone():
            raise CoordError("forbidden", f"#{m['id']} is not visible to you")
        return "message", m, m["body"], None

    @staticmethod
    def _suggestion_source(sg) -> str:
        where = f"DOC{sg['source_id']}" + (f" r{sg['source_revision']}" if sg["source_revision"] else "") \
            if sg["source_type"] == "document" else f"#{sg['source_id']}"
        return f"S{sg['id']} from {where} ({sg['nature']}): «{sg['quote']}»"

    def _suggestion_dict(self, sg) -> dict:
        return {"suggestion": f"S{sg['id']}", "project": sg["project_id"],
                "source": f"DOC{sg['source_id']}" if sg["source_type"] == "document" else f"#{sg['source_id']}",
                "revision": sg["source_revision"], "quote": sg["quote"], "nature": sg["nature"], "target": sg["target"],
                "title": sg["title"], "body": sg["body"], "memory_kind": sg["memory_kind"], "status": sg["status"],
                "proposed_by": sg["proposed_by"], "reviewed_by": sg["reviewed_by"], "review_note": sg["review_note"],
                "result": sg["result"], "at": iso(sg["created_at"]), "reviewed_at": iso(sg["reviewed_at"])}

    def suggestion_add(self, session: str, source: str, target: str, title: str, quote: str | None = None,
                       body: str = "", nature: str = "fact", memory_kind: str | None = None,
                       client_id: str | None = None) -> dict:
        """Propose a candidate drawn from a source (DOC3, or a message #12 to turn a conversation into a
        structured object). `quote` is the passage it comes from and must be found verbatim in the source
        (a message may omit it: the whole message is cited). `nature` says what the passage is: fact
        (reported), hypothesis, opinion, or decision (already taken elsewhere). Nothing happens until a
        reviewer with the decide right accepts it."""
        if target not in SUGGESTION_TARGETS:
            raise CoordError("bad_args", f"target must be one of {', '.join(SUGGESTION_TARGETS)}")
        if nature not in SUGGESTION_NATURES:
            raise CoordError("bad_args", f"nature must be one of {', '.join(SUGGESTION_NATURES)}")
        if target == "memory" and memory_kind not in MEMORY_KINDS:
            raise CoordError("bad_kind", f"a memory candidate needs --memory-kind, one of {', '.join(MEMORY_KINDS)}")
        if not (title or "").strip():
            raise CoordError("empty", "a candidate needs a title")

        def fn(db):
            stype, row, text, rev = self._source(db, source, session)
            me, project = self._access(db, session, row["project_id"], "participate")
            q = (quote or "").strip() or (text if stype == "message" else "")
            if not q:
                raise CoordError("quote_required", "cite the passage the candidate comes from (--quote)")
            if _squash(q) not in _squash(text):
                raise CoordError("quote_not_found", f"the passage is not in {source} as it stands"
                                 + (f" (r{rev})" if rev else ""))
            sid = db.execute("INSERT INTO suggestions(project_id, source_type, source_id, source_revision, quote, nature,"
                             " target, title, body, memory_kind, proposed_by, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                             (project, stype, row["id"], rev, q, nature, target, title.strip(), body or "",
                              memory_kind if target == "memory" else None, me["display_name"], self.clock())).lastrowid
            self._event(db, project, "suggestion.proposed", session, "suggestion", sid, source=str(source), target=target)
            return {"suggestion": f"S{sid}", "id": sid, "target": target, "status": "proposed"}
        return self._mutate("suggestion_add", client_id, fn)

    def suggestions(self, project: str | None = None, source: str | None = None, status: str | None = None,
                    session: str | None = None) -> list[dict]:
        with self._read() as db:
            q, a = "SELECT * FROM suggestions WHERE 1=1", []
            if project:
                self._view(db, project, session)
                q += " AND project_id=?"; a.append(project)
            else:
                f, fargs = self._view_filter(db, session)
                q += f" AND {f}"; a += list(fargs)
            if source:
                src = str(source).strip()
                if src.upper().startswith("DOC"):
                    q += " AND source_type='document' AND source_id=?"; a.append(parse_id("document", src))
                else:
                    q += " AND source_type='message' AND source_id=?"; a.append(int(src.lstrip("#")))
            if status:
                q += " AND status=?"; a.append(status)
            rows = db.execute(q + " ORDER BY id", a).fetchall()
            out = []
            for sg in rows:
                if sg["source_type"] == "document":
                    d = db.execute("SELECT * FROM documents WHERE id=?", (sg["source_id"],)).fetchone()
                    if d is not None and not self._doc_readable(db, d, session):
                        continue
                out.append(self._suggestion_dict(sg))
        return out

    def suggestion_review(self, session: str, suggestion: str, accept: bool, note: str = "",
                          title: str | None = None, body: str | None = None) -> dict:
        """Accept or reject a candidate - the decide right, the explicit validation. Accepting creates the
        official object, linked back to its passage: a task (unassigned), a discussion with the decision
        as its first proposal (to debate, not decided), a memory entry, a question to the project, or a
        summary document. `title`/`body` correct it on the way (the correction is recorded). A rejection
        needs its reason."""
        if not accept and not note.strip():
            raise CoordError("reason_required", "a rejection needs its reason")
        with self._tx() as db:
            sg = db.execute("SELECT * FROM suggestions WHERE id=?", (parse_id("suggestion", suggestion),)).fetchone()
            if sg is None:
                raise CoordError("missing", f"no candidate {suggestion}")
            me, project = self._access(db, session, sg["project_id"], "decide")
            if sg["status"] != "proposed":
                raise CoordError("closed", f"S{sg['id']} was already {sg['status']} by {sg['reviewed_by']}")
            now = self.clock()
            final_title, final_body = (title or sg["title"]).strip(), sg["body"] if body is None else body
            corrected = (title is not None and title.strip() != sg["title"]) or (body is not None and body != sg["body"])
            review_note = (note or "") + (" [corrected on review]" if corrected else "")
            result = None
            if accept:
                cite = self._suggestion_source(sg)
                if sg["target"] == "task":
                    tid = db.execute("INSERT INTO tasks(project_id, created_by, title, description, status, created_at,"
                                     " updated_at, suggestion_id) VALUES(?,?,?,?,'open',?,?,?)",
                                     (project, me["display_name"], final_title,
                                      (final_body + "\n\n" if final_body else "") + cite, now, now, sg["id"])).lastrowid
                    self._event(db, project, "task.created", session, "task", tid, suggestion=f"S{sg['id']}")
                    result = f"T{tid}"
                elif sg["target"] == "decision":
                    opened = self._discussion_open(db, me, final_title)
                    pid = db.execute("INSERT INTO proposals(discussion_id, author_session_id, author_name, body, created_at)"
                                     " VALUES(?,?,?,?,?)", (opened["id"], session, me["display_name"],
                                                            (final_body or final_title) + f"\n({cite})", now)).lastrowid
                    self._event(db, project, "proposal.created", session, "proposal", pid, suggestion=f"S{sg['id']}")
                    result = f"D{opened['id']}"
                elif sg["target"] == "memory":
                    if sg["memory_kind"] == "policy":
                        self._access(db, session, project, "admin")
                    result = f"M{self._memory_insert(db, me, sg['memory_kind'], final_title, final_body or sg['quote'], cite)}"
                elif sg["target"] == "question":
                    mid = db.execute("INSERT INTO messages(project_id, from_session_id, from_name, kind, body, created_at,"
                                     " document_id) VALUES(?,?,?,?,?,?,?)",
                                     (project, session, me["display_name"], "question",
                                      f"[S{sg['id']}] {final_title}" + (f" - {final_body}" if final_body else "") + f" ({cite})",
                                      now, sg["source_id"] if sg["source_type"] == "document" else None)).lastrowid
                    db.execute("UPDATE messages SET thread_id=? WHERE id=?", (mid, mid))
                    self._event(db, project, "message.posted", session, "message", mid, kind="question")
                    result = f"#{mid}"
                else:   # summary: a document of its own, pointing at its source
                    result = f"DOC{self._doc_insert(db, me, final_title, 'note', (final_body or '') + chr(10) * 2 + cite)}"
                if sg["source_type"] == "message":        # the conversation learns what it became
                    src = db.execute("SELECT * FROM messages WHERE id=?", (sg["source_id"],)).fetchone()
                    mid = db.execute("INSERT INTO messages(project_id, from_session_id, from_name, kind, body, thread_id,"
                                     " reply_to, created_at) VALUES(?,?,?,?,?,?,?,?)",
                                     (project, session, me["display_name"], "info",
                                      f"[S{sg['id']} accepted -> {result}] {final_title}",
                                      src["thread_id"] or src["id"], src["id"], now)).lastrowid
            db.execute("UPDATE suggestions SET status=?, reviewed_by=?, review_note=?, result=?, reviewed_at=?, title=?,"
                       " body=? WHERE id=?",
                       ("accepted" if accept else "rejected", me["display_name"], review_note or None, result, now,
                        final_title, final_body, sg["id"]))
            if corrected:          # the proposer's own wording stays on record
                self._event(db, project, "suggestion.corrected", session, "suggestion", sg["id"],
                            original_title=sg["title"], original_body=sg["body"])
            self._event(db, project, "suggestion.accepted" if accept else "suggestion.rejected", session,
                        "suggestion", sg["id"], result=result, note=note or None,
                        self_reviewed=sg["proposed_by"] == me["display_name"])
            proposer = self._live_by_name(db, sg["proposed_by"])
            self._notify(db, me, proposer["session_id"] if proposer else None,
                         f"[S{sg['id']}] {f'accepted -> {result}' if accept else 'rejected'}: {final_title}"
                         + (f" - {note}" if note else ""), kind="info" if accept else "warning")
            return {"suggestion": f"S{sg['id']}", "status": "accepted" if accept else "rejected", "result": result,
                    "corrected": corrected, "self_reviewed": sg["proposed_by"] == me["display_name"]}
