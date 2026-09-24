"""Database upkeep for the administrator - never an agent op. CLI: `coord-db`.

    coord-db export [--project P] [--out file.json]   every table (a project's rows only with --project)
    coord-db prune --older-than 30d [--apply]         dry run unless --apply
    coord-db vacuum                                   checkpoint the WAL and compact the file
    coord-db merge-project OLD NEW [--apply]          move everything of project OLD into NEW

prune only removes what nobody needs to read again: events, idempotency keys, ended sessions,
released claims (and their roles), finished routine runs, and messages - except unresolved
questions and warnings. Documents, memory, discussions, decisions, tasks and routines are kept.
Safe while coord-server runs (one BEGIN IMMEDIATE transaction, like every other write).
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from . import state_home
from .core import SERVER_NAME, CoordBase, parse_when


def _db(path: str) -> sqlite3.Connection:
    CoordBase(path)                      # creates/migrates the schema like the server would
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    return db


def export(path: str, project: str | None = None) -> dict:
    db = _db(path)
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND"
                                       " name NOT LIKE 'sqlite_%' ORDER BY name")]
    out = {"exported_at": time.time(), "project": project, "tables": {}}
    for t in tables:
        cols = {r[1] for r in db.execute(f"PRAGMA table_info({t})")}
        if project and "project_id" not in cols:
            continue
        q, a = (f"SELECT * FROM {t} WHERE project_id=?", (project,)) if project else (f"SELECT * FROM {t}", ())
        out["tables"][t] = [dict(r) for r in db.execute(q, a)]
    db.close()
    return out


# (label, SQL selecting the rowids to delete, parameters from the cutoff)
_PRUNE = [
    ("events", "SELECT rowid FROM events WHERE created_at<?"),
    ("idempotency keys", "SELECT rowid FROM idempotency WHERE created_at<?"),
    ("messages", "SELECT rowid FROM messages WHERE created_at<? AND NOT (resolved_at IS NULL"
                 " AND kind IN ('question','warning'))"),
    ("released claims", "SELECT rowid FROM claims WHERE released_at IS NOT NULL AND released_at<?"),
    ("routine runs", "SELECT rowid FROM routine_runs WHERE finished_at IS NOT NULL AND finished_at<?"),
    ("ended sessions", "SELECT rowid FROM sessions WHERE ended_at IS NOT NULL AND ended_at<?"),
]
_TABLE = {"events": "events", "idempotency keys": "idempotency", "messages": "messages",
          "released claims": "claims", "routine runs": "routine_runs", "ended sessions": "sessions"}


def prune(path: str, older_than: str, apply: bool = False) -> dict:
    now = time.time()
    cutoff = 2 * now - parse_when(older_than, now)       # "30d" -> 30 days ago
    db = _db(path)
    counts = {}
    try:
        db.execute("BEGIN IMMEDIATE")
        for label, sql in _PRUNE:
            ids = [r[0] for r in db.execute(sql, (cutoff,))]
            counts[label] = len(ids)
            if apply and ids:
                if label == "released claims":
                    db.executemany("DELETE FROM claim_roles WHERE claim_id=(SELECT claim_id FROM claims"
                                   " WHERE rowid=?)", [(i,) for i in ids])
                db.executemany(f"DELETE FROM {_TABLE[label]} WHERE rowid=?", [(i,) for i in ids])
        db.execute("COMMIT" if apply else "ROLLBACK")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    finally:
        db.close()
    return {"older_than": older_than, "applied": apply, "removed" if apply else "would_remove": counts}


def merge_project(path: str, old: str, new: str, apply: bool = False) -> dict:
    """Move every row of project `old` into `new` (a renamed repository, or sessions that joined under
    a wrong project id). A dry run unless `apply`; one transaction."""
    if old == new:
        raise SystemExit("coord-db: OLD and NEW are the same project")
    db = _db(path)
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    counts = {}
    try:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM repos WHERE project_id=? UNION SELECT 1 FROM sessions WHERE project_id=?",
                          (old, old)).fetchone():
            raise SystemExit(f"coord-db: no project {old!r}")
        moved_ids = {r[0] for r in db.execute("SELECT id FROM messages WHERE project_id=?", (old,))}
        for t in tables:
            if t == "repos" or "project_id" not in {r[1] for r in db.execute(f"PRAGMA table_info({t})")}:
                continue
            n = db.execute(f"SELECT COUNT(*) FROM {t} WHERE project_id=?", (old,)).fetchone()[0]
            if n:
                counts[t] = n
                if apply:
                    db.execute(f"UPDATE {t} SET project_id=? WHERE project_id=?", (new, old))
        open_ids = [r[0] for r in db.execute("SELECT id FROM messages WHERE project_id=? AND resolved_at IS NULL AND"
                                             " kind IN ('question','warning') ORDER BY id", (new if apply else old,))
                    if not apply or r[0] in moved_ids]
        if apply:
            db.execute("INSERT OR IGNORE INTO repos(project_id, provider, created_at) SELECT ?, provider, created_at"
                       " FROM repos WHERE project_id=?", (new, old))
            db.execute("DELETE FROM repos WHERE project_id=?", (old,))
            if counts.get("messages"):     # their ids are older than the live cursors: poll would never show them
                note = (f"{counts['messages']} messages from project {old} were merged into this one"
                        + (f"; still open: {', '.join(f'#{i}' for i in open_ids)} (coord thread N)" if open_ids else ""))
                db.execute("INSERT INTO messages(project_id, from_session_id, from_name, kind, body, created_at)"
                           " VALUES(?,?,?,?,?,?)", (new, SERVER_NAME, SERVER_NAME, "info", note, time.time()))
        db.execute("COMMIT" if apply else "ROLLBACK")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    finally:
        db.close()
    return {"from": old, "into": new, "applied": apply, "moved" if apply else "would_move": counts,
            "open_messages": open_ids}


def vacuum(path: str) -> dict:
    before = os.path.getsize(path)
    db = _db(path)
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.execute("VACUUM")
    db.close()
    return {"db": path, "bytes_before": before, "bytes_after": os.path.getsize(path)}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="coord-db", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=os.environ.get("COORD_DB") or str(state_home() / "coord2.db"),
                   help="default $COORD_DB, else coord2.db in the checkout or ~/.local/share/coord")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="dump the database (or one project) as JSON")
    e.add_argument("--project"); e.add_argument("--out")
    r = sub.add_parser("prune", help="drop what nobody reads again; a dry run without --apply")
    r.add_argument("--older-than", required=True, help="30d, 12h, ...")
    r.add_argument("--apply", action="store_true")
    sub.add_parser("vacuum", help="checkpoint the WAL and compact the file")
    m = sub.add_parser("merge-project", help="move project OLD into NEW (a rename, a wrong project id); dry run "
                       "without --apply")
    m.add_argument("old"); m.add_argument("new"); m.add_argument("--apply", action="store_true")
    a = p.parse_args(argv)
    if not Path(a.db).exists():
        print(f"coord-db: no database at {a.db}", file=sys.stderr)
        return 1
    if a.cmd == "export":
        data = json.dumps(export(a.db, a.project), indent=1, default=str)
        if a.out:
            Path(a.out).write_text(data, encoding="utf-8")
            print(json.dumps({"out": a.out, "bytes": len(data)}))
        else:
            print(data)
    elif a.cmd == "prune":
        print(json.dumps(prune(a.db, a.older_than, a.apply)))
    elif a.cmd == "merge-project":
        print(json.dumps(merge_project(a.db, a.old, a.new, a.apply)))
    else:
        print(json.dumps(vacuum(a.db)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
