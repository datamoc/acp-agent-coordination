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

try:
    import fcntl
except ImportError:  # Windows: in-process lock only
    fcntl = None

CLIENT_DAYS = 30
SERVER_DAYS = 365
RENEW_AFTER_DAYS = 15
DEFAULT_DIR = Path(__file__).resolve().parents[1] / "pki"

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
basicConstraints = CA:FALSE
extendedKeyUsage = clientAuth
[server_ext]
basicConstraints = CA:FALSE
extendedKeyUsage = serverAuth
subjectAltName = @alt
[alt]
DNS.1 = localhost
IP.1 = 127.0.0.1
"""

_thread_lock = threading.Lock()


def _run(*args, text=False) -> str | bytes:
    return subprocess.run(["openssl", *args], check=True, capture_output=True, text=text).stdout


@contextmanager
def _locked(d: Path):
    """Serialize CA database changes across threads and processes (server + CLI)."""
    with _thread_lock, open(d / ".lock", "a") as f:
        if fcntl:
            fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl:
                fcntl.flock(f, fcntl.LOCK_UN)


def _index(d: Path) -> list[list[str]]:
    return [line.split("\t") for line in (d / "index.txt").read_text().splitlines() if line]


def init(d: str | Path, cn: str = "coord-ca") -> Path:
    d = Path(d).resolve()
    (d / "newcerts").mkdir(parents=True, exist_ok=True)
    (d / "index.txt").touch()
    for f in ("serial", "crlnumber"):
        if not (d / f).exists():
            (d / f).write_text("1000\n")
    (d / "openssl.cnf").write_text(_CNF.format(d=d))
    if not (d / "ca.crt").exists():
        _run("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
             "-keyout", str(d / "ca.key"), "-out", str(d / "ca.crt"), "-days", "3650", "-subj", f"/CN={cn}")
    gencrl(d)
    return d


def issue(d: str | Path, name: str, server: bool = False) -> tuple[Path, Path]:
    d = Path(d).resolve()
    key, csr, crt = d / f"{name}.key", d / f"{name}.csr", d / f"{name}.crt"
    with _locked(d):
        _run("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
             "-keyout", str(key), "-out", str(csr), "-subj", f"/CN={name}")
        _run("ca", "-batch", "-config", str(d / "openssl.cnf"), "-extensions",
             "server_ext" if server else "client_ext", "-days", str(SERVER_DAYS if server else CLIENT_DAYS),
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
        subprocess.run(["openssl", "ca", "-config", str(d / "openssl.cnf"), "-updatedb"], capture_output=True)
    return Authority(d).status(serial) == "V"


class Authority:
    """What the server asks management: validity of a serial, and renewals."""

    def __init__(self, d: str | Path, renew_after_days: float = RENEW_AFTER_DAYS, clock=time.time):
        self.d, self.clock = Path(d).resolve(), clock
        self.renew_after = dt.timedelta(days=renew_after_days)
        self._status: tuple[float, dict[str, str]] = (-1.0, {})

    def status(self, serial: str) -> str | None:
        """'V' valid, 'R' revoked, 'E' expired, None unknown. Re-read when index.txt changes."""
        mtime = (self.d / "index.txt").stat().st_mtime
        if self._status[0] != mtime:
            self._status = (mtime, {f[3].upper().lstrip("0"): f[0] for f in _index(self.d) if len(f) >= 4})
        return self._status[1].get(serial.upper().lstrip("0"))

    def due(self, not_before: float) -> bool:
        return self.clock() - not_before >= self.renew_after.total_seconds()

    def renew(self, der: bytes) -> str:
        """Return a fresh PEM certificate for the same CN and public key as `der`.

        Idempotent per key: while a renewal issued after the presented
        certificate is still fresh, the same one is returned again (so a
        client that failed to save it, or parallel requests, don't mint more)."""
        with tempfile.TemporaryDirectory() as tmp:
            cur = Path(tmp) / "cur.der"
            cur.write_bytes(der)
            pem = _run("x509", "-inform", "DER", "-in", str(cur), text=True)
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
                     "-set_serial", f"0x{new_serial}", "-days", str(CLIENT_DAYS),
                     "-extfile", str(self.d / "openssl.cnf"), "-extensions", "client_ext", "-out", str(out))
                end = _run("x509", "-in", str(out), "-noout", "-enddate", text=True).strip().partition("=")[2]
                end_ts = dt.datetime.strptime(end, "%b %d %H:%M:%S %Y %Z").strftime("%y%m%d%H%M%SZ")
                with open(self.d / "index.txt", "a") as idx:   # same line format `openssl ca` writes
                    idx.write(f"V\t{end_ts}\t\t{new_serial}\tunknown\t/CN={cn}\n")
                nxt = f"{int(new_serial, 16) + 1:X}"
                (self.d / "serial").write_text(nxt.zfill(len(nxt) + len(nxt) % 2) + "\n")
                book[key_id] = {"serial": new_serial, "cn": cn,
                                "issued": self.clock()}
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
            link.symlink_to(Path(name) / "env")
        r["default"] = link.is_symlink() and link.resolve() == (b / "env").resolve()
    return r


def listing(d: str | Path) -> list[dict]:
    names = {"V": "valid", "R": "revoked", "E": "expired"}
    return [{"serial": f[3], "cn": f[-1].removeprefix("/CN="), "status": names.get(f[0], f[0]),
             "expires": f[1]} for f in _index(Path(d).resolve())]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="coord-admin", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", type=Path, default=Path(os.environ.get("COORD_PKI") or DEFAULT_DIR),
                   help="CA directory (default $COORD_PKI or this checkout's pki/)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create the CA (idempotent; also refreshes the CRL)")
    s = sub.add_parser("server-cert", help="issue the server certificate")
    s.add_argument("name", nargs="?", default="localhost")
    s = sub.add_parser("enroll", help="issue a client cert and write its identity bundle")
    s.add_argument("name")
    s.add_argument("--url", default=os.environ.get("COORD_SERVER") or "https://localhost:1338")
    s.add_argument("--out", type=Path, help="write the bundle here (to hand to another machine) "
                   "instead of ~/.config/coord/<name>")
    s.add_argument("--default", action="store_true", help="make it the identity used when "
                   "COORD_IDENTITY is unset (automatic for the first one)")
    s = sub.add_parser("revoke", help="revoke a client and all its renewals (effective immediately)")
    s.add_argument("name")
    s = sub.add_parser("issue", help="low level: issue <name>.crt/.key in the CA dir")
    s.add_argument("name")
    s.add_argument("--server", action="store_true")
    sub.add_parser("list", help="certificates known to the CA")
    a = p.parse_args(argv)
    try:
        if a.cmd == "init":
            r = {"ca": str(init(a.dir) / "ca.crt")}
        elif a.cmd == "server-cert":
            crt, key = issue(a.dir, a.name, server=True)
            r = {"cert": str(crt), "key": str(key)}
        elif a.cmd == "issue":
            crt, key = issue(a.dir, a.name, server=a.server)
            r = {"cert": str(crt), "key": str(key)}
        elif a.cmd == "enroll":
            r = enroll(a.dir, a.name, a.url, a.out, a.default)
        elif a.cmd == "revoke":
            r = {"revoked": a.name, "crl": str(revoke(a.dir, a.name))}
        else:
            r = listing(a.dir)
    except subprocess.CalledProcessError as e:
        print(f"coord-admin: openssl failed: {(e.stderr or b'').decode(errors='replace').strip()}", file=sys.stderr)
        return 1
    print(json.dumps(r, indent=None if a.cmd != "list" else 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
