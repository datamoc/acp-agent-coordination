"""Management: the local CA for mTLS (wraps the openssl CLI). CLI: `coord-admin`.

Only this module touches the CA key. The administrator uses it to create
the CA and the server certificate, to enroll clients (a self-contained
identity bundle: ca.crt, agent.crt, agent.key, env) and to revoke them.
The server asks it, through `Authority`, whether a presented certificate
is still valid and for a renewed certificate once one is RENEW_AFTER_DAYS
old. A renewal re-certifies the public key the client already holds, so
no private key ever leaves the client and the client needs no openssl.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import state_home
from .sslbin import OpensslMissing, crashed, openssl

try:
    import fcntl
    msvcrt = None
except ImportError:  # Windows: byte-range lock on the lock file instead
    fcntl = None
    import msvcrt

MAX_DAYS = 47            # no leaf certificate (client or server) may live longer
CLIENT_DAYS = 30
SERVER_DAYS = 47
RENEW_AFTER_DAYS = 15    # renew once this old - or at once if it outlives MAX_DAYS
assert max(CLIENT_DAYS, SERVER_DAYS) <= MAX_DAYS
DEFAULT_DIR = state_home() / "pki"

_CNF = """[ca]
default_ca = coord
[coord]
dir = {d}
database = $dir/index.txt
new_certs_dir = $dir/newcerts
serial = $dir/serial
crlnumber = $dir/crlnumber
certificate = $dir/ca.crt
private_key = $dir/ca.key
default_md = sha256
default_days = 365
default_crl_days = 30
policy = anything
unique_subject = no
copy_extensions = copy
[anything]
commonName = supplied
[client_ext]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature
extendedKeyUsage = clientAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
[server_ext]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature
extendedKeyUsage = serverAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
subjectAltName = @alt
[alt]
DNS.1 = localhost
IP.1 = 127.0.0.1
"""

_thread_lock = threading.Lock()


def _run(*args, text=False) -> str | bytes:
    return subprocess.run([openssl(), *args], check=True, capture_output=True, text=text).stdout


@contextmanager
def _locked(d: Path):
    """Serialize CA database changes across threads and processes (coord-server renewing while
    coord-admin enrolls): `openssl ca` itself does not lock index.txt/serial."""
    with _thread_lock, open(d / ".lock", "a+") as f:
        if fcntl:
            fcntl.flock(f, fcntl.LOCK_EX)
        else:                                 # msvcrt.LK_LOCK gives up after ~10 s: keep waiting
            f.seek(0)
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    continue
        try:
            yield
        finally:
            if fcntl:
                fcntl.flock(f, fcntl.LOCK_UN)
            else:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def _ts(openssl_date: str) -> float:
    """'Sep 23 08:57:42 2026 GMT' -> epoch seconds."""
    return dt.datetime.strptime(openssl_date.strip(), "%b %d %H:%M:%S %Y %Z").replace(
        tzinfo=dt.timezone.utc).timestamp()


def _index(d: Path) -> list[list[str]]:
    return [line.split("\t") for line in (d / "index.txt").read_text().splitlines() if line]


_CA_EXT = ("-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
           "-addext", "subjectKeyIdentifier=hash")   # Python >= 3.13 (VERIFY_X509_STRICT) needs keyUsage


def write_cnf(d: Path) -> None:
    """(Re)write openssl.cnf: it holds the CA dir's absolute path, so after a moved or renamed
    checkout openssl would otherwise look for the CA key in the old place."""
    # forward slashes: openssl.cnf treats "\" as an escape (C:\Users\...\tmp -> "C:Users...<TAB>mp")
    text, f = _CNF.format(d=d.as_posix()), d / "openssl.cnf"
    if not f.exists() or f.read_text() != text:
        f.write_text(text)


def init(d: str | Path, cn: str = "coord-ca") -> Path:
    d = Path(d).resolve()
    (d / "newcerts").mkdir(parents=True, exist_ok=True)
    (d / "index.txt").touch()
    for f in ("serial", "crlnumber"):
        if not (d / f).exists():
            (d / f).write_text("1000\n")
    write_cnf(d)
    if not (d / "ca.crt").exists():
        _run("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
             "-keyout", str(d / "ca.key"), "-out", str(d / "ca.crt"), "-days", "3650", "-subj", f"/CN={cn}",
             *_CA_EXT)
    with _locked(d):   # the CRL number is shared state too
        upgrade_ca(d)
        gencrl(d)
    return d


def upgrade_ca(d: Path) -> bool:
    """A CA certificate made before keyUsage was added is refused by Python >= 3.13 clients.
    Re-sign it with the same key and subject plus the extensions - everything it issued stays
    valid - keep the old one as ca.crt.pre-keyusage, and refresh this machine's bundles."""
    old = (d / "ca.crt").read_text()
    if "Key Usage" in _run("x509", "-in", str(d / "ca.crt"), "-noout", "-text", text=True):
        return False
    subject = _run("x509", "-in", str(d / "ca.crt"), "-noout", "-subject", "-nameopt", "compat",
                   text=True).strip().partition("=")[2]
    (d / "ca.crt.pre-keyusage").write_text(old)
    _run("req", "-x509", "-new", "-key", str(d / "ca.key"), "-subj", subject if subject.startswith("/") else f"/{subject}",
         "-days", "3650", "-out", str(d / "ca.crt"), *_CA_EXT)
    new = (d / "ca.crt").read_bytes()
    home = config_home()
    for bundle_ca in home.glob("*/ca.crt") if home.is_dir() else []:
        if bundle_ca.read_text().strip() == old.strip():
            bundle_ca.write_bytes(new)
    return True


def issue(d: str | Path, name: str, server: bool = False, days: int | None = None) -> tuple[Path, Path]:
    d = Path(d).resolve()
    key, csr, crt = d / f"{name}.key", d / f"{name}.csr", d / f"{name}.crt"
    with _locked(d):
        _run("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
             "-keyout", str(key), "-out", str(csr), "-subj", f"/CN={name}")
        _run("ca", "-batch", "-config", str(d / "openssl.cnf"), "-extensions",
             "server_ext" if server else "client_ext", "-days", str(days or (SERVER_DAYS if server else CLIENT_DAYS)),
             "-in", str(csr), "-out", str(crt), "-notext")
    return crt, key


def revoke(d: str | Path, name: str) -> Path:
    """Revoke every valid certificate of <name>: the enrolled one and all its renewals."""
    d = Path(d).resolve()
    with _locked(d):
        serials = [f[3] for f in _index(d) if f[0] == "V" and f[-1] == f"/CN={name}"]
        if not serials:
            raise SystemExit(f"coord-admin: no valid certificate for {name!r}")
        for s in serials:
            _run("ca", "-config", str(d / "openssl.cnf"), "-revoke", str(d / "newcerts" / f"{s}.pem"))
        return gencrl(d)


def _pubkey(pem_file: Path) -> str:
    return _run("x509", "-in", str(pem_file), "-noout", "-pubkey", text=True)


def _same_key_valid(d: Path, cn: str, pub: str) -> list[str]:
    """Valid serials of <cn> whose certificate carries public key <pub>."""
    out = []
    for f in _index(d):
        pem = d / "newcerts" / f"{f[3]}.pem"
        if f[0] == "V" and f[-1] == f"/CN={cn}" and pem.exists() and _pubkey(pem) == pub:
            out.append(f[3])
    return out


def _revoke_serials(d: Path, serials: list[str]) -> None:
    """Caller holds the lock. Revokes each serial still valid, then refreshes the CRL."""
    valid = {f[3] for f in _index(d) if f[0] == "V"}
    for s in serials:
        if s in valid:
            _run("ca", "-config", str(d / "openssl.cnf"), "-revoke", str(d / "newcerts" / f"{s}.pem"),
                 "-crl_reason", "superseded")
    gencrl(d)


def tidy(d: str | Path, apply: bool = False) -> list[dict]:
    """Superseded certificates: for each (CN, key), every valid cert but the newest.
    Dry run unless `apply`; the newest of each key stays valid, so no client is locked out
    as long as it holds that newest cert (check with `list` first)."""
    d = Path(d).resolve()
    with _locked(d):
        groups: dict[tuple[str, str], list[str]] = {}
        for f in _index(d):
            pem = d / "newcerts" / f"{f[3]}.pem"
            if f[0] == "V" and pem.exists():
                groups.setdefault((f[-1], _pubkey(pem)), []).append(f[3])
        old = [(cn, s) for (cn, _), serials in groups.items()
               for s in sorted(serials, key=lambda x: int(x, 16))[:-1]]
        if apply and old:
            _revoke_serials(d, [s for _, s in old])
    return [{"serial": s, "cn": cn.removeprefix("/CN="), "revoked" if apply else "would_revoke": True}
            for cn, s in old]


def gencrl(d: str | Path) -> Path:
    d = Path(d).resolve()
    _run("ca", "-config", str(d / "openssl.cnf"), "-gencrl", "-out", str(d / "crl.pem"))
    return d / "crl.pem"


def is_valid(d: str | Path, name: str) -> bool:
    """True if <name>.crt exists and the CA index lists its serial as valid (not revoked/expired)."""
    d = Path(d).resolve()
    crt = d / f"{name}.crt"
    if not crt.exists():
        return False
    serial = _run("x509", "-in", str(crt), "-noout", "-serial", text=True).strip().partition("=")[2]
    with _locked(d):
        subprocess.run([openssl(), "ca", "-config", str(d / "openssl.cnf"), "-updatedb"], capture_output=True)
    return Authority(d).status(serial) == "V"


class Authority:
    """What the server asks management: validity of a serial, and renewals."""

    def __init__(self, d: str | Path, renew_after_days: float = RENEW_AFTER_DAYS, clock=time.time):
        self.d, self.clock = Path(d).resolve(), clock
        if (self.d / "ca.crt").exists():
            write_cnf(self.d)
        self.renew_after = dt.timedelta(days=renew_after_days)
        self._status: tuple[float, dict[str, str]] = (-1.0, {})
        self._confirmed: set[str] = set()

    def status(self, serial: str) -> str | None:
        """'V' valid, 'R' revoked, 'E' expired, None unknown. Re-read when index.txt changes."""
        mtime = (self.d / "index.txt").stat().st_mtime
        if self._status[0] != mtime:
            self._status = (mtime, {f[3].upper().lstrip("0"): f[0] for f in _index(self.d) if len(f) >= 4})
        return self._status[1].get(serial.upper().lstrip("0"))

    def due(self, not_before: float, not_after: float | None = None) -> bool:
        """Renew once RENEW_AFTER_DAYS old, or right away if the cert outlives MAX_DAYS."""
        too_long = not_after is not None and not_after - not_before > MAX_DAYS * 86400 + 60
        return too_long or self.clock() - not_before >= self.renew_after.total_seconds()

    def confirm(self, serial: str) -> None:
        """The holder of <serial> is using it: revoke the certificates it superseded."""
        if serial in self._confirmed:
            return
        book_f = self.d / "renewals.json"
        if book_f.exists():
            with _locked(self.d):
                book = json.loads(book_f.read_text())
                for entry in book.values():
                    if entry["serial"].upper().lstrip("0") == serial.upper().lstrip("0") and entry.get("supersedes"):
                        _revoke_serials(self.d, entry.pop("supersedes"))
                        book_f.write_text(json.dumps(book, indent=1))
        self._confirmed.add(serial)

    def renew_server(self, cert: str | Path) -> str | None:
        """The server's own certificate: re-certify it when due, replace the file atomically
        and return the new PEM; None when it is not due yet."""
        cert = Path(cert)
        dates = _run("x509", "-in", str(cert), "-noout", "-startdate", "-enddate", text=True)
        nb, na = (_ts(line.partition("=")[2]) for line in dates.strip().splitlines())
        if not self.due(nb, na):
            return None
        pem = self.renew(cert.read_bytes(), server=True)
        tmp = cert.with_suffix(".renew")
        tmp.write_text(pem)
        os.replace(tmp, cert)
        self.confirm(_run("x509", "-in", str(cert), "-noout", "-serial", text=True).strip().partition("=")[2])
        return pem

    def renew(self, der: bytes, server: bool = False) -> str:
        """Return a fresh PEM certificate for the same CN and public key as `der` (DER or PEM).

        Idempotent per key: while a renewal issued after the presented
        certificate is still fresh, the same one is returned again (so a
        client that failed to save it, or parallel requests, don't mint more)."""
        with tempfile.TemporaryDirectory() as tmp:
            cur = Path(tmp) / "cur.der"
            cur.write_bytes(der)
            pem = _run("x509", "-inform", "PEM" if der.lstrip().startswith(b"-----") else "DER",
                       "-in", str(cur), text=True)
            (Path(tmp) / "cur.pem").write_text(pem)
            _run("verify", "-CAfile", str(self.d / "ca.crt"), str(Path(tmp) / "cur.pem"))
            info = _run("x509", "-in", str(Path(tmp) / "cur.pem"), "-noout", "-serial", "-subject",
                        "-nameopt", "multiline", "-startdate", text=True)
            serial = info.split("serial=")[1].split()[0]
            cn = info.split("commonName")[1].split("=", 1)[1].splitlines()[0].strip()
            start = dt.datetime.strptime(info.split("notBefore=")[1].strip(), "%b %d %H:%M:%S %Y %Z") \
                .replace(tzinfo=dt.timezone.utc)
            pub = _run("x509", "-in", str(Path(tmp) / "cur.pem"), "-noout", "-pubkey", text=True)
            if self.status(serial) != "V":
                raise PermissionError(f"certificate {serial} of {cn} is not valid")
            key_id = hashlib.sha256(pub.encode()).hexdigest()
            with _locked(self.d):
                book_f = self.d / "renewals.json"
                book = json.loads(book_f.read_text()) if book_f.exists() else {}
                last = book.get(key_id)
                if last and self.status(last["serial"]) == "V" and last["issued"] > start.timestamp() \
                        and not self.due(last["issued"]):
                    return (self.d / "newcerts" / f"{last['serial']}.pem").read_text()
                (Path(tmp) / "pub.pem").write_text(pub)
                new_serial = (self.d / "serial").read_text().strip()
                out = self.d / "newcerts" / f"{new_serial}.pem"
                _run("x509", "-new", "-force_pubkey", str(Path(tmp) / "pub.pem"), "-subj", f"/CN={cn}",
                     "-CA", str(self.d / "ca.crt"), "-CAkey", str(self.d / "ca.key"),
                     "-set_serial", f"0x{new_serial}", "-days", str(SERVER_DAYS if server else CLIENT_DAYS),
                     "-extfile", str(self.d / "openssl.cnf"), "-extensions",
                     "server_ext" if server else "client_ext", "-out", str(out))
                end = _run("x509", "-in", str(out), "-noout", "-enddate", text=True).strip().partition("=")[2]
                end_ts = dt.datetime.strptime(end, "%b %d %H:%M:%S %Y %Z").strftime("%y%m%d%H%M%SZ")
                with open(self.d / "index.txt", "a", newline="\n") as idx:   # `openssl ca` format; no CRLF
                    idx.write(f"V\t{end_ts}\t\t{new_serial}\tunknown\t/CN={cn}\n")
                nxt = f"{int(new_serial, 16) + 1:X}"
                (self.d / "serial").write_text(nxt.zfill(len(nxt) + len(nxt) % 2) + "\n", newline="\n")
                book[key_id] = {"serial": new_serial, "cn": cn, "issued": self.clock(),
                                # revoked once the new cert is seen in use (confirm): never before,
                                # so a client that failed to save it is not locked out
                                "supersedes": [x for x in _same_key_valid(self.d, cn, pub) if x != new_serial]}
                book_f.write_text(json.dumps(book, indent=1))
                return out.read_text()


# --- client enrollment -----------------------------------------------------
def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "coord"


def enroll(d: str | Path, name: str, server: str, out: Path | None = None,
           make_default: bool = False) -> dict:
    """Issue (or reuse) <name>'s client cert and write its self-contained identity bundle."""
    d = Path(d).resolve()
    if not (d / "ca.crt").exists():
        raise SystemExit(f"coord-admin: no CA in {d} - run `coord-admin init` first")
    reused = is_valid(d, name)
    if not reused:
        issue(d, name)
    b = out or config_home() / name
    b.mkdir(parents=True, exist_ok=True)
    b.chmod(0o700)
    (b / "ca.crt").write_bytes((d / "ca.crt").read_bytes())
    (b / "agent.crt").write_bytes((d / f"{name}.crt").read_bytes())
    key = b / "agent.key"
    key.unlink(missing_ok=True)
    fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # never world-readable, even briefly
    with os.fdopen(fd, "wb") as f:
        f.write((d / f"{name}.key").read_bytes())
    (b / "env").write_text(f"# identity {name}, written by `coord-admin enroll`\n"
                           f"COORD_SERVER={server}\nCOORD_CA=ca.crt\nCOORD_CERT=agent.crt\nCOORD_KEY=agent.key\n")
    r = {"identity": name, "bundle": str(b), "cert_reused": reused, "default": False}
    if out is None:
        link = config_home() / "env"
        if make_default or not (link.exists() or link.is_symlink()):
            if link.exists() or link.is_symlink():
                link.unlink()
            try:
                link.symlink_to(Path(name) / "env")
            except OSError:   # Windows without Developer Mode: a pointer file `coord` follows
                link.write_text(f"# default identity, written by `coord-admin enroll`\nCOORD_IDENTITY={name}\n")
        r["default"] = default_identity() == name
    return r


def default_identity() -> str | None:
    """Which identity ~/.config/coord/env designates (symlink or pointer file)."""
    link = config_home() / "env"
    if link.is_symlink():
        return link.resolve().parent.name
    if link.is_file():
        for line in link.read_text().splitlines():
            if line.startswith("COORD_IDENTITY="):
                return line.partition("=")[2].strip()
    return None
    return r


def listing(d: str | Path) -> list[dict]:
    names = {"V": "valid", "R": "revoked", "E": "expired"}
    return [{"serial": f[3], "cn": f[-1].removeprefix("/CN="), "status": names.get(f[0], f[0]),
             "expires": f[1]} for f in _index(Path(d).resolve())]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="coord-admin", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", type=Path, default=Path(os.environ.get("COORD_PKI") or DEFAULT_DIR),
                   help=f"CA directory (default $COORD_PKI or {DEFAULT_DIR})")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create the CA (idempotent; refreshes the CRL, upgrades an old CA certificate)")
    s = sub.add_parser("server-cert", help="issue the server's own certificate (once; it renews itself)")
    s.add_argument("name", nargs="?", default="localhost", metavar="server-name",
                   help="the host clients connect to (default localhost)")
    s = sub.add_parser("enroll", help="issue a CLIENT (agent) identity and write its bundle - not the server")
    s.add_argument("name", metavar="client-name", help="the agent identity, e.g. alice-laptop; it becomes "
                   "the principal (mtls:<client-name>) and ~/.config/coord/<client-name>/")
    s.add_argument("--url", default=os.environ.get("COORD_SERVER") or "https://localhost:1337")
    s.add_argument("--out", type=Path, help="write the bundle here (to hand to another machine) "
                   "instead of ~/.config/coord/<name>")
    s.add_argument("--default", action="store_true", help="make it the identity used when "
                   "COORD_IDENTITY is unset (automatic for the first one)")
    s = sub.add_parser("revoke", help="revoke a client and all its renewals (effective immediately)")
    s.add_argument("name", metavar="client-name")
    s = sub.add_parser("issue", help="low level: issue <name>.crt/.key in the CA dir")
    s.add_argument("name")
    s.add_argument("--server", action="store_true")
    sub.add_parser("list", help="certificates known to the CA")
    s = sub.add_parser("tidy", help="revoke superseded certs (older ones with the same key); dry run "
                       "without --apply")
    s.add_argument("--apply", action="store_true")
    a = p.parse_args(argv)
    try:
        if a.cmd != "init" and (a.dir / "ca.crt").exists():
            write_cnf(a.dir.resolve())
        if a.cmd == "init":
            upgraded = (a.dir / "ca.crt").exists() and "Key Usage" not in _run(
                "x509", "-in", str(a.dir / "ca.crt"), "-noout", "-text", text=True)
            r = {"ca": str(init(a.dir) / "ca.crt"), "upgraded": upgraded}
            if upgraded:
                r["next"] = "coord-admin server-cert, then restart coord-server; copy the new ca.crt into bundles on other machines"
        elif a.cmd == "server-cert":
            d = a.dir.resolve()
            before = [f[3] for f in _index(d) if f[0] == "V" and f[-1] == f"/CN={a.name}"] if (d / "index.txt").exists() else []
            crt, key = issue(d, a.name, server=True)
            with _locked(d):   # the server loads the new files at its next start: retire the old ones
                _revoke_serials(d, before)
            r = {"cert": str(crt), "key": str(key), "revoked_previous": before,
                 "next": "restart coord-server" if before else None}
        elif a.cmd == "issue":
            crt, key = issue(a.dir, a.name, server=a.server)
            r = {"cert": str(crt), "key": str(key)}
        elif a.cmd == "enroll":
            r = enroll(a.dir, a.name, a.url, a.out, a.default)
        elif a.cmd == "revoke":
            r = {"revoked": a.name, "crl": str(revoke(a.dir, a.name))}
        elif a.cmd == "tidy":
            r = tidy(a.dir, a.apply)
        else:
            r = listing(a.dir)
    except OpensslMissing as e:
        print(f"coord-admin: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        if crashed(e.returncode):
            err = f"{openssl()} crashed (exit {e.returncode:#x}); point COORD_OPENSSL at a working openssl"
        elif "openssl.cnf" in err and "for reading" in err:
            err += f"\n{openssl()} looks for a config file that does not exist; point COORD_OPENSSL at another openssl"
        print(f"coord-admin: openssl failed: {err.strip()}", file=sys.stderr)
        return 1
    print(json.dumps(r, indent=1 if a.cmd in ("list", "tidy") else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
