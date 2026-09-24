"""Write schema/ops.json - the coord wire contract - from coordination/service.py.

Every op is POST /call {"op": <name>, "args": {...}} -> {"ok": true, "result": ...}
or {"ok": false, "error": <code>, "message": ..., "data": ...}. Clients in any
language check themselves against this file (see test_coord.py and
clients/ts/test/). Regenerate after changing an op signature:

    uv run tools/gen_schema.py
"""

import inspect
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coordination import service  # noqa: E402
from coordination.service import READ_OPS, WRITE_OPS, Coord  # noqa: E402

SERVER_FILLED = {"principal"}   # set by the server from the authenticated identity
OUT = Path(__file__).resolve().parents[1] / "schema" / "ops.json"


_JSON = {"str": "string", "int": "integer", "bool": "boolean", "float": "number", "list[str]": "string[]",
         "dict": "object"}


def json_type(annotation) -> tuple[str, bool]:
    """Python annotation -> (JSON type, nullable)."""
    text = annotation.__name__ if isinstance(annotation, type) else str(annotation)
    parts = [t.strip() for t in str(text).split("|")]
    nullable = "None" in parts
    parts = [t for t in parts if t != "None"]
    if len(parts) != 1 or parts[0] not in _JSON:
        raise SystemExit(f"unmapped annotation {annotation!r}")
    return _JSON[parts[0]], nullable


def schema() -> dict:
    ops = {}
    for op in sorted(READ_OPS | WRITE_OPS):
        params = []
        for n, p in inspect.signature(getattr(Coord, op)).parameters.items():
            if n == "self" or n in SERVER_FILLED:
                continue
            t, nullable = json_type(p.annotation)
            params.append({"name": n, "type": t, "nullable": nullable,
                           "required": p.default is inspect.Parameter.empty})
        ops[op] = {"kind": "write" if op in WRITE_OPS else "read", "params": params}
    enums = {name.lower(): list(getattr(service, name))
             for name in ("MESSAGE_KINDS", "ROLES", "PROJECT_ROLES", "STANCES", "DOC_KINDS", "NOTE_CONTEXTS",
                          "MEMORY_KINDS", "CONSENSUS_RULES", "ROUTINE_STATUSES", "ROUTINE_OUTCOMES")}
    return {"version": 1, "transport": "POST /call {op, args}", "enums": enums, "ops": ops}


def render() -> str:
    return json.dumps(schema(), indent=1) + "\n"


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(0 if OUT.exists() and OUT.read_text() == render() else "schema/ops.json is stale: uv run tools/gen_schema.py")
    OUT.write_text(render())
    print(f"wrote {OUT} ({len(schema()['ops'])} ops)")
