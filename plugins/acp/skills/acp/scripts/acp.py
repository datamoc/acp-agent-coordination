"""Run ACP_client.py from any working directory.

Installed plugins live in a cache copy (~/.claude/plugins/cache,
~/.codex/plugins/cache), not in the checkout, so this wrapper has to find
the acp-agent-coordination repo before it can delegate to its client.
Lookup order:

  1. $ACP_HOME            - path to the repo checkout
  2. parent directories   - when run straight from the checkout's plugins/
  3. ~/dev/acp-agent-coordination

Every argument is passed through unchanged, so the usage is exactly
`ACP_client.py`'s (`acp.py status`, `acp.py post "<session>: hi"`, ...).
The client runs under `uv run --project <repo>` so its httpx dependency
comes from the repo's own environment; without uv it falls back to this
interpreter, which then needs httpx installed.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

CLIENT = "ACP_client.py"
DEFAULT_HOME = Path.home() / "dev" / "acp-agent-coordination"


def find_home() -> Path | None:
    env = os.environ.get("ACP_HOME")
    if env:
        home = Path(env).expanduser()
        return home if (home / CLIENT).is_file() else None
    for parent in Path(__file__).resolve().parents:
        if (parent / CLIENT).is_file():
            return parent
    return DEFAULT_HOME if (DEFAULT_HOME / CLIENT).is_file() else None


def main() -> int:
    home = find_home()
    if home is None:
        tried = os.environ.get("ACP_HOME") or f"parent dirs, {DEFAULT_HOME}"
        print(
            f"ACP FAILED: cannot find {CLIENT} - set ACP_HOME to the "
            f"acp-agent-coordination checkout (tried {tried}).",
            file=sys.stderr,
        )
        return 2
    client = str(home / CLIENT)
    uv = shutil.which("uv")
    if uv:
        cmd = [uv, "run", "--quiet", "--project", str(home), "python", client]
    else:
        cmd = [sys.executable, client]
    return subprocess.call(cmd + sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
