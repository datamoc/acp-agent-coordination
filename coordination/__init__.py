"""coord v2: service (SQLite), server, client, management (PKI)."""

import os
from pathlib import Path


def state_home() -> Path:
    """Where the server's database and management's CA live by default: the source
    checkout when running from one (pki/, coord2.db next to pyproject.toml), else
    $XDG_DATA_HOME/coord (~/.local/share/coord) for an installed package."""
    checkout = Path(__file__).resolve().parents[1]
    if (checkout / "pyproject.toml").exists() and (checkout / "coord.py").exists():
        return checkout
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "coord"
