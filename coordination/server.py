"""Server: JSON-over-HTTP transport, POST /call {"op": ..., "args": {...}}. CLI: `coord-server`.

Loopback by default with no auth. Any non-loopback bind requires TLS plus
an identity method: mTLS (client certs from management, pki.py) or OIDC
bearer tokens validated by token introspection (Keycloak etc). When an
identity is present, sessions are bound to it at whoami time.

With `--pki DIR` the server asks management (pki.Authority) on every
request whether the presented certificate is still valid - a revocation
applies at once, no restart - and, once the certificate is
RENEW_AFTER_DAYS old, for a renewed one, which it hands to the client in
the response ("certificate"). The client never contacts management.
"""

import argparse
import errno
import json
import os
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .net import is_loopback
from .service import READ_OPS, WRITE_OPS, Coord, CoordError

PROJECT_ROLES = {"viewer": 0, "contributor": 1, "admin": 2}


class OIDCIntrospector:
    """RFC 7662 introspection. Roles come from `coord:<project>:<role>` or
    `coord:*:<role>` entries in realm_access.roles / roles / groups."""

    def __init__(self, url: str, client_id: str, client_secret: str, cache_seconds: int = 60,
                 opener=urllib.request.urlopen):
        self.url, self.client_id, self.client_secret = url, client_id, client_secret
        self.cache_seconds, self.opener = cache_seconds, opener
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def introspect(self, token: str) -> dict:
        with self._lock:
            hit = self._cache.get(token)
            if hit and hit[0] > time.time():
                return hit[1]
        data = urllib.parse.urlencode({"token": token, "client_id": self.client_id,
                                       "client_secret": self.client_secret}).encode()
        req = urllib.request.Request(self.url, data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with self.opener(req, timeout=10) as resp:
            info = json.loads(resp.read())
        if not info.get("active"):
            raise CoordError("unauthenticated", "token is not active")
        with self._lock:
            self._cache[token] = (time.time() + self.cache_seconds, info)
        return info

    @staticmethod
    def project_role(info: dict, project: str) -> int:
        names = list(info.get("roles") or []) + list((info.get("realm_access") or {}).get("roles") or []) \
            + list(info.get("groups") or [])
        best = -1
        for n in names:
            parts = str(n).strip("/").split(":")
            if len(parts) == 3 and parts[0] == "coord" and parts[1] in (project, "*") \
                    and parts[2] in PROJECT_ROLES:
                best = max(best, PROJECT_ROLES[parts[2]])
        return best


def make_handler(coord, oidc: OIDCIntrospector | None, mtls: bool, authority=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, code: int, payload: dict):
            if getattr(self, "_renewal", None):
                payload["certificate"] = self._renewal
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _check_cert(self, cert: dict):
            """Ask management whether the cert is still valid; fetch a renewal when it is due."""
            if authority is None:
                return
            if authority.status(cert.get("serialNumber", "")) != "V":
                raise CoordError("unauthenticated", "client certificate is revoked or unknown to the CA")
            if authority.due(ssl.cert_time_to_seconds(cert["notBefore"])):
                try:
                    self._renewal = authority.renew(self.connection.getpeercert(binary_form=True))
                except Exception as e:   # a failed renewal must not fail the request
                    print(f"coord-server: renewal failed: {e!r}", file=sys.stderr, flush=True)

        def _identity(self):
            if mtls:
                cert = self.connection.getpeercert()
                self._check_cert(cert)
                subject = dict(x[0] for x in cert.get("subject", ()))
                return f"mtls:{subject.get('commonName')}", None
            if oidc:
                auth = self.headers.get("Authorization", "")
                if not auth.lower().startswith("bearer "):
                    raise CoordError("unauthenticated", "missing bearer token")
                info = oidc.introspect(auth[7:].strip())
                return f"oidc:{info.get('sub')}", info
            return None, None

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, {"ok": True})
            self._send(404, {"ok": False, "error": "not_found"})

        def do_POST(self):
            self._renewal = None
            if self.path != "/call":
                return self._send(404, {"ok": False, "error": "not_found"})
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                op, args = req.get("op"), dict(req.get("args") or {})
                if op not in READ_OPS | WRITE_OPS:
                    raise CoordError("bad_op", f"unknown op {op!r}")
                principal, info = self._identity()
                if principal is not None:
                    if op == "whoami":
                        args["principal"] = principal
                    elif args.get("session"):
                        coord.check_principal(args["session"], principal)
                    if info is not None:
                        project = args.get("project") or (
                            coord.session_project(args["session"]) if args.get("session") else None) or "default"
                        need = PROJECT_ROLES["contributor"] if op in WRITE_OPS else PROJECT_ROLES["viewer"]
                        if oidc.project_role(info, project) < need:
                            raise CoordError("forbidden", f"no {'contributor' if need else 'viewer'} role "
                                             f"on project {project}")
                result = getattr(coord, op)(**args)
                self._send(200, {"ok": True, "result": result})
            except CoordError as e:
                code = {"unauthenticated": 401, "forbidden": 403, "bad_op": 400}.get(e.code, 409)
                self._send(code, {"ok": False, "error": e.code, "message": str(e), "data": e.data})
            except TypeError as e:
                self._send(400, {"ok": False, "error": "bad_args", "message": str(e)})
            except Exception as e:
                self._send(500, {"ok": False, "error": "internal", "message": repr(e)})
    return Handler


def build_server(coord, host="127.0.0.1", port=1338, tls_cert=None, tls_key=None, client_ca=None,
                 crl=None, oidc: OIDCIntrospector | None = None,
                 authority=None) -> ThreadingHTTPServer:
    """`authority` is a pki.Authority (mTLS with management) or None: local and OIDC
    modes never load the PKI code."""
    if not is_loopback(host):
        if not (tls_cert and tls_key):
            raise SystemExit(f"refusing to listen on {host}: non-loopback requires --tls-cert/--tls-key")
        if not (client_ca or oidc):
            raise SystemExit(f"refusing to listen on {host}: non-loopback requires --client-ca (mTLS) "
                             "or --oidc-introspect-url")
    if client_ca and not (tls_cert and tls_key):
        raise SystemExit("--client-ca needs --tls-cert/--tls-key")
    httpd = ThreadingHTTPServer((host, port), make_handler(coord, oidc, mtls=bool(client_ca), authority=authority))
    if tls_cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(tls_cert, tls_key)
        if client_ca:
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(client_ca)
            if crl:
                ctx.load_verify_locations(crl)
                ctx.verify_flags |= ssl.VERIFY_CRL_CHECK_LEAF
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    return httpd


def main(argv=None) -> int:
    root = Path(__file__).resolve().parents[1]
    p = argparse.ArgumentParser(prog="coord-server", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--listen", default="127.0.0.1")
    p.add_argument("--port", type=int, default=1338)
    p.add_argument("--db", default=os.environ.get("COORD_DB") or str(root / "coord2.db"))
    p.add_argument("--pki", type=Path, help="mTLS with management's CA dir: client CA, server cert "
                   "(<dir>/localhost.crt/.key unless --tls-cert/--tls-key), live revocation and renewal")
    p.add_argument("--renew-after-days", type=float, help="with --pki (default 15)")
    p.add_argument("--tls-cert"); p.add_argument("--tls-key")
    p.add_argument("--client-ca"); p.add_argument("--crl")
    p.add_argument("--oidc-introspect-url"); p.add_argument("--oidc-client-id")
    p.add_argument("--oidc-client-secret")
    a = p.parse_args(argv)
    authority = None
    if a.renew_after_days is not None and not a.pki:
        p.error("--renew-after-days needs --pki")
    if a.pki:
        from .pki import RENEW_AFTER_DAYS, Authority   # only mTLS with management needs the PKI code
        a.renew_after_days = a.renew_after_days or RENEW_AFTER_DAYS
        d = a.pki.resolve()
        authority = Authority(d, a.renew_after_days)
        a.client_ca = a.client_ca or str(d / "ca.crt")
        a.tls_cert = a.tls_cert or str(d / "localhost.crt")
        a.tls_key = a.tls_key or str(d / "localhost.key")
    oidc = None
    if a.oidc_introspect_url:
        oidc = OIDCIntrospector(a.oidc_introspect_url, a.oidc_client_id or "",
                                os.environ.get("COORD_OIDC_SECRET") or a.oidc_client_secret or "")
    try:
        httpd = build_server(Coord(a.db), a.listen, a.port, a.tls_cert, a.tls_key, a.client_ca, a.crl,
                             oidc, authority)
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        print(f"error: {a.listen}:{a.port} is already in use - another `coord-server` is probably "
              f"running (find it with `ss -ltnp | grep :{a.port}`), or pick another --port", file=sys.stderr)
        return 1
    scheme = "https" if a.tls_cert else "http"
    print(f"coord-server on {scheme}://{a.listen}:{a.port}"
          + (f" (mTLS, renewal after {a.renew_after_days:g} days)" if authority else ""), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
