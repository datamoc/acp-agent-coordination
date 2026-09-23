"""Local mode for non-Python clients: one op against a SQLite db, over stdio. CLI: `coord-local`.

stdin:  {"db": "<path to coord2.db>", "op": "<name>", "args": {...}}
stdout: the same envelope as the HTTP server's /call:
        {"ok": true, "result": ...} or {"ok": false, "error": code, "message": ..., "data": ...}

No server, no port, no auth - exactly the Python client's former local mode.
"""

import json
import sys

from .service import READ_OPS, WRITE_OPS, Coord, CoordError


def run(req: dict) -> dict:
    op, args = req.get("op"), dict(req.get("args") or {})
    if op not in READ_OPS | WRITE_OPS:
        return {"ok": False, "error": "bad_op", "message": f"unknown op {op!r}", "data": None}
    args.pop("principal", None)   # never client-supplied
    try:
        return {"ok": True, "result": getattr(Coord(req["db"]), op)(**args)}
    except CoordError as e:
        return {"ok": False, "error": e.code, "message": str(e), "data": e.data}
    except TypeError as e:
        return {"ok": False, "error": "bad_args", "message": str(e), "data": None}


def main() -> int:
    try:
        req = json.loads(sys.stdin.read() or "{}")
    except ValueError as e:
        req, out = None, {"ok": False, "error": "bad_request", "message": str(e), "data": None}
    if req is not None:
        out = run(req) if req.get("db") else {"ok": False, "error": "bad_request", "message": "missing db", "data": None}
    sys.stdout.write(json.dumps(out))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
