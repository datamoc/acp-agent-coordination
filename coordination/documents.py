"""Collaborative documents with revisions and optimistic concurrency."""

from . import textpatch
from .core import DOC_KINDS, CoordError, iso, parse_id


class DocumentsMixin:
    def _doc_insert(self, db, me, title, kind, content, message="created") -> int:
        now = self.clock()
        did = db.execute("INSERT INTO documents(project_id, title, kind, created_by, revision, content,"
                         " created_at, updated_at) VALUES(?,?,?,?,1,?,?,?)",
                         (me["project_id"], title, kind, me["display_name"], content, now, now)).lastrowid
        db.execute("INSERT INTO document_revisions VALUES(?,?,?,?,?,?,?)",
                   (did, 1, me["session_id"], me["display_name"], content, message, now))
        self._event(db, me["project_id"], "document.created", me["session_id"], "document", did)
        return did

    def doc_create(self, session: str, title: str, kind: str = "note", content: str = "",
                   client_id: str | None = None) -> dict:
        if kind not in DOC_KINDS:
            raise CoordError("bad_kind", f"document kind must be one of {', '.join(DOC_KINDS)}")

        def fn(db):
            me = self._session(db, session)
            did = self._doc_insert(db, me, title, kind, content)
            return {"document": f"DOC{did}", "id": did, "revision": 1}
        return self._mutate("doc_create", client_id, fn)

    def doc_show(self, document: str, revision: int | None = None) -> dict:
        did = parse_id("document", document)
        with self._read() as db:
            d = db.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
            if d is None:
                raise CoordError("missing", f"no document DOC{did}")
            content, rev = d["content"], d["revision"]
            if revision is not None:
                r = db.execute("SELECT * FROM document_revisions WHERE document_id=? AND revision=?",
                               (did, int(revision))).fetchone()
                if r is None:
                    raise CoordError("missing", f"DOC{did} has no revision {revision}")
                content, rev = r["content"], r["revision"]
        return {"document": f"DOC{did}", "title": d["title"], "kind": d["kind"], "status": d["status"],
                "revision": rev, "latest_revision": d["revision"], "created_by": d["created_by"],
                "updated_at": iso(d["updated_at"]), "content": content}

    def doc_edit(self, session: str, document: str, base_revision: int, content: str,
                 message: str = "", client_id: str | None = None) -> dict:
        """Optimistic concurrency: succeeds only if nobody edited since base_revision."""
        def fn(db):
            me = self._session(db, session)
            d = self._doc_editable(db, document)
            did = d["id"]
            if d["revision"] != int(base_revision):
                raise CoordError("revision_conflict",
                                 f"DOC{did} is at revision {d['revision']}, you edited {base_revision}; "
                                 "merge with the current content and retry with --base-revision "
                                 f"{d['revision']}",
                                 {"current_revision": d["revision"], "current_content": d["content"]})
            return {"document": f"DOC{did}", "revision": self._doc_write(db, me, d, content, message)}
        return self._mutate("doc_edit", client_id, fn)

    def _doc_editable(self, db, document: str):
        did = parse_id("document", document)
        d = db.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
        if d is None:
            raise CoordError("missing", f"no document DOC{did}")
        if d["status"] == "final":
            raise CoordError("final", f"DOC{did} is final; create a new document")
        return d

    def _doc_write(self, db, me, d, content: str, message: str) -> int:
        rev = d["revision"] + 1
        now = self.clock()
        db.execute("UPDATE documents SET revision=?, content=?, updated_at=? WHERE id=? AND revision=?",
                   (rev, content, now, d["id"], d["revision"]))
        db.execute("INSERT INTO document_revisions VALUES(?,?,?,?,?,?,?)",
                   (d["id"], rev, me["session_id"], me["display_name"], content, message, now))
        self._event(db, d["project_id"], "document.edited", me["session_id"], "document", d["id"], revision=rev)
        return rev

    def doc_patch(self, session: str, document: str, base_revision: int, patch: str,
                  message: str = "", client_id: str | None = None) -> dict:
        """Edit with a unified diff made against `base_revision` - only the change travels.

        The diff must apply to that revision exactly (else `patch_invalid`). If the document moved
        on since, the same diff is re-applied to the latest revision, each hunk located by its
        context: edits that don't overlap merge (`merged`: true); overlapping ones are refused with
        `revision_conflict`, naming the hunks and carrying the current content."""
        def fn(db):
            me = self._session(db, session)
            d = self._doc_editable(db, document)
            did, base = d["id"], int(base_revision)
            b = db.execute("SELECT content FROM document_revisions WHERE document_id=? AND revision=?",
                           (did, base)).fetchone()
            if b is None:
                raise CoordError("missing", f"DOC{did} has no revision {base}")
            try:
                hunks = textpatch.parse(patch)
                intended, _ = textpatch.apply(b["content"], hunks)
            except textpatch.PatchError as e:
                raise CoordError("patch_invalid", f"the patch does not apply to DOC{did} revision {base}: {e}",
                                 {"failed_hunks": e.failed})
            merged = d["revision"] != base
            if not merged:
                content, offsets = intended, [0] * len(hunks)
            else:
                try:
                    content, offsets = textpatch.apply(d["content"], hunks)
                except textpatch.PatchError as e:
                    raise CoordError(
                        "revision_conflict",
                        f"DOC{did} is at revision {d['revision']}, your patch was made on {base} and overlaps "
                        f"the changes since ({e}); rebase on revision {d['revision']} and retry",
                        {"current_revision": d["revision"], "current_content": d["content"],
                         "failed_hunks": [hunks[i].header for i in e.failed]})
            if content == d["content"]:
                raise CoordError("no_change", f"the patch leaves DOC{did} unchanged")
            note = message or f"patch ({len(hunks)} hunk{'s' if len(hunks) != 1 else ''})"
            if merged:
                note += f" [made on r{base}, merged onto r{d['revision']}]"
            rev = self._doc_write(db, me, d, content, note)
            return {"document": f"DOC{did}", "revision": rev, "base_revision": base, "merged": merged,
                    "hunks": len(hunks), "offsets": offsets}
        return self._mutate("doc_patch", client_id, fn)

    def doc_history(self, document: str) -> list[dict]:
        did = parse_id("document", document)
        with self._read() as db:
            rows = db.execute("SELECT * FROM document_revisions WHERE document_id=? ORDER BY revision",
                              (did,)).fetchall()
        if not rows:
            raise CoordError("missing", f"no document DOC{did}")
        return [{"revision": r["revision"], "author": r["author_name"], "message": r["message"],
                 "at": iso(r["created_at"]), "chars": len(r["content"])} for r in rows]

    def docs(self, project: str | None = None, kind: str | None = None) -> list[dict]:
        with self._read() as db:
            q, a = "SELECT * FROM documents WHERE 1=1", []
            if project:
                q += " AND project_id=?"; a.append(project)
            if kind:
                q += " AND kind=?"; a.append(kind)
            rows = db.execute(q + " ORDER BY id", a).fetchall()
        return [{"document": f"DOC{r['id']}", "title": r["title"], "kind": r["kind"],
                 "revision": r["revision"], "status": r["status"]} for r in rows]
