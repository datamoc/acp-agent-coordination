"""Minimal local CA for mTLS (wraps the openssl CLI): init, issue, revoke, CRL."""

import subprocess
from pathlib import Path

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


def _run(*args):
    subprocess.run(["openssl", *args], check=True, capture_output=True)


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
    _run("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
         "-keyout", str(key), "-out", str(csr), "-subj", f"/CN={name}")
    _run("ca", "-batch", "-config", str(d / "openssl.cnf"), "-extensions",
         "server_ext" if server else "client_ext", "-in", str(csr), "-out", str(crt), "-notext")
    return crt, key


def revoke(d: str | Path, name: str) -> Path:
    d = Path(d).resolve()
    _run("ca", "-config", str(d / "openssl.cnf"), "-revoke", str(d / f"{name}.crt"))
    return gencrl(d)


def gencrl(d: str | Path) -> Path:
    d = Path(d).resolve()
    _run("ca", "-config", str(d / "openssl.cnf"), "-gencrl", "-out", str(d / "crl.pem"))
    return d / "crl.pem"
