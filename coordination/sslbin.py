"""Which openssl binary management and the certificate sources run.

$COORD_OPENSSL wins. On Windows, Git for Windows' copy comes next: the first
openssl.exe on PATH there is often one bundled by an unrelated application
(KDiff3, ...), built with a config path from its build machine - it cannot
find its openssl.cnf and some crash outright. Otherwise the one on PATH.
"""

import os
import shutil
import sys
from functools import lru_cache
from pathlib import Path


class OpensslMissing(RuntimeError):
    pass


def _git_openssl() -> Path | None:
    roots = []
    git = shutil.which("git")
    if git:                                   # <root>\cmd\git.exe or <root>\bin\git.exe
        roots.append(Path(git).resolve().parent.parent)
    for var in ("ProgramFiles", "ProgramW6432", "LOCALAPPDATA"):
        if os.environ.get(var):
            roots.append(Path(os.environ[var]) / ("Programs/Git" if var == "LOCALAPPDATA" else "Git"))
    for root in roots:
        for sub in ("mingw64/bin/openssl.exe", "usr/bin/openssl.exe"):
            if (root / sub).is_file():
                return root / sub
    return None


@lru_cache(maxsize=None)
def openssl() -> str:
    if os.environ.get("COORD_OPENSSL"):
        return os.environ["COORD_OPENSSL"]
    if sys.platform == "win32" and (git := _git_openssl()):
        return str(git)
    found = shutil.which("openssl")
    if not found:
        raise OpensslMissing("openssl not found: install it (on Windows it comes with Git for Windows) "
                             "or set COORD_OPENSSL to its path")
    return found


def crashed(returncode: int) -> bool:
    """A signal on POSIX, an NTSTATUS exception code (0xC0000005 access violation, ...) on Windows."""
    return returncode < 0 or returncode >= 0xC0000000
