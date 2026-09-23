"""Where the server's own TLS certificate comes from, and how it is renewed.

Independent of how clients authenticate (none, mTLS, OIDC/Keycloak):

  local    our management (pki.py) re-certifies it          - the local mTLS setup
  watch    something else rewrites the files; we reload     - cert-manager (mounted Secret),
                                                              certmonger (AD CS autoenrollment),
                                                              `step ca renew --daemon`
  command  we run a renewal command when due, then reload   - `step ca renew --force {cert} {key}`,
                                                              a certreq/PowerShell script for AD CS

`refresh()` returns True when new files are on disk *and* they load as a
valid pair; the server then swaps them into its live SSL context. A bad or
half-written pair is never loaded: the server keeps serving the old one.
Only `local` imports the PKI code.
"""

import hashlib
import shlex
import ssl
import subprocess
import time
from pathlib import Path

RENEW_AFTER_DAYS = 15   # or 2/3 of the lifetime, whichever comes first (24 h step-ca certs...)


def cert_dates(path: str | Path) -> tuple[float, float]:
    """(notBefore, notAfter) as epoch seconds, stdlib only (openssl CLI as a fallback)."""
    try:
        d = ssl._ssl._test_decode_cert(str(path))
        return ssl.cert_time_to_seconds(d["notBefore"]), ssl.cert_time_to_seconds(d["notAfter"])
    except (AttributeError, ssl.SSLError):
        out = subprocess.run(["openssl", "x509", "-in", str(path), "-noout", "-startdate", "-enddate"],
                             capture_output=True, text=True, check=True).stdout
        nb, na = (line.partition("=")[2] for line in out.strip().splitlines())
        return ssl.cert_time_to_seconds(nb), ssl.cert_time_to_seconds(na)


def renewal_due(not_before: float, not_after: float, now: float, renew_after_days: float) -> bool:
    return now - not_before >= min(renew_after_days * 86400, (not_after - not_before) * 2 / 3)


def loads(cert: str | Path, key: str | Path) -> bool:
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(str(cert), str(key))
        return True
    except (OSError, ssl.SSLError):
        return False


class WatchSource:
    """An external renewer owns the files; reload when their content changes."""

    name = "watch"

    def __init__(self, cert, key):
        self.cert, self.key = Path(cert), Path(key)
        self._seen = self._digest()

    def _digest(self) -> str:
        try:   # content, not mtime: kubelet swaps a ..data symlink, others rewrite in place
            return hashlib.sha256(self.cert.read_bytes() + b"\0" + self.key.read_bytes()).hexdigest()
        except OSError:
            return ""

    def refresh(self) -> bool:
        now = self._digest()
        if not now or now == self._seen:
            return False
        if not loads(self.cert, self.key):   # half-written or mismatched: retry next tick
            return False
        self._seen = now
        return True


class CommandSource:
    """Run a renewal command when the cert is due; it must rewrite {cert}/{key} in place."""

    name = "command"

    def __init__(self, cert, key, command: str, renew_after_days: float = RENEW_AFTER_DAYS,
                 clock=time.time, timeout: float = 300):
        self.cert, self.key, self.clock, self.timeout = Path(cert), Path(key), clock, timeout
        self.renew_after_days = renew_after_days
        self.argv = [part.format(cert=self.cert, key=self.key) for part in shlex.split(command)]

    def refresh(self) -> bool:
        if not renewal_due(*cert_dates(self.cert), self.clock(), self.renew_after_days):
            return False
        before = self.cert.read_bytes()
        p = subprocess.run(self.argv, capture_output=True, text=True, timeout=self.timeout)
        if p.returncode != 0:
            raise RuntimeError(f"renewal command exited {p.returncode}: {(p.stderr or p.stdout).strip()[:500]}")
        if self.cert.read_bytes() == before:
            raise RuntimeError("renewal command succeeded but the certificate did not change")
        if not loads(self.cert, self.key):
            raise RuntimeError("renewed certificate does not load with its key")
        return True


class LocalSource:
    """Our own management (pki.py) re-certifies the server key."""

    name = "local"

    def __init__(self, cert, key, authority):
        self.cert, self.key, self.authority = cert, key, authority

    def refresh(self) -> bool:
        return self.authority.renew_server(self.cert) is not None


def make(kind: str, cert, key, *, command: str | None = None, authority=None,
         renew_after_days: float = RENEW_AFTER_DAYS):
    if kind == "watch":
        return WatchSource(cert, key)
    if kind == "command":
        if not command:
            raise SystemExit("--cert-source command needs --renew-command")
        return CommandSource(cert, key, command, renew_after_days)
    if kind == "local":
        if authority is None:
            raise SystemExit("--cert-source local needs --pki")
        return LocalSource(cert, key, authority)
    raise SystemExit(f"unknown --cert-source {kind!r}")
