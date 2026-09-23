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
The server's own certificate is renewed by a --cert-source (certsource.py):
our management (local), an external renewer's files (watch: cert-manager,
certmonger/AD CS) or a command (step-ca, AD CS scripts); it is loaded live.
No certificate lives longer than pki.MAX_DAYS (47); longer ones are
renewed at the first check or request.
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

from . import state_home
from . import a2a
from .net import is_loopback
from .service import READ_OPS, WRITE_OPS, Coord, CoordError

PROJECT_ROLES = {"viewer": 0, "contributor": 1, "admin": 2}


class OIDCIntrospector:
    """RFC 7662 introspection. Roles come from `coord:<project>:<role>` or
    `coord:*:<role>` entries in realm_access.roles / roles / groups."""

    def __init__(self, url: str, client_id: str, client_secret: str, cache_seconds: int = 60,
                 opener=None):
        self.url, self.client_id, self.client_secret = url, client_id, client_secret
        if opener is None:   # a loopback IdP never goes through a proxy (Windows reads the system one)
            handlers = [urllib.request.ProxyHandler({})] if is_loopback(urllib.parse.urlsplit(url).hostname or "") else []
            opener = urllib.request.build_opener(*handlers).open
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


def make_handler(coord, oidc: OIDCIntrospector | None, mtls: bool, authority=None, pusher=None,
                 public_url: str | None = None, version: str = "0"):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, code: int, payload: dict, content_type: str = "application/json", renewal_in_body=True):
            if getattr(self, "_renewal", None) and renewal_in_body:
                payload["certificate"] = self._renewal
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if getattr(self, "_renewal", None) and not renewal_in_body:
                self.send_header("Coord-Certificate", a2a.certificate_header(self._renewal))
            self.end_headers()
            self.wfile.write(body)

        def _check_cert(self, cert: dict):
            """Ask management whether the cert is still valid; fetch a renewal when it is due."""
            if authority is None:
                return
            if authority.status(cert.get("serialNumber", "")) != "V":
                raise CoordError("unauthenticated", "client certificate is revoked or unknown to the CA")
            try:
                authority.confirm(cert["serialNumber"])   # in use: retire what it superseded
            except Exception as e:
                print(f"coord-server: could not retire superseded certs: {e!r}", file=sys.stderr, flush=True)
            if authority.due(ssl.cert_time_to_seconds(cert["notBefore"]), ssl.cert_time_to_seconds(cert["notAfter"])):
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

        def _base_url(self) -> str:
            if public_url:
                return public_url
            scheme = "https" if isinstance(self.connection, ssl.SSLSocket) else "http"
            return f"{scheme}://{self.headers.get('Host') or '%s:%s' % self.server.server_address[:2]}"

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, {"ok": True})
            if self.path == "/.well-known/agent-card.json":
                oidc_url = oidc.url.split("/protocol/openid-connect/")[0] + "/.well-known/openid-configuration" \
                    if oidc else None
                return self._send(200, a2a.agent_card(self._base_url(), mtls, oidc_url, version))
            self._send(404, {"ok": False, "error": "not_found"})

        def _run_op(self, op: str, args: dict, principal, info):
            """One coord op with the caller's identity checks - shared by /call and /a2a."""
            if op not in READ_OPS | WRITE_OPS:
                raise CoordError("bad_op", f"unknown op {op!r}")
            args.pop("principal", None)                      # only the server sets it
            if principal is not None:
                if op == "whoami":
                    args["principal"] = principal
                elif args.get("session"):
                    coord.check_principal(args["session"], principal)
                if info is not None:
                    project = args.get("project") or (
                        coord.session_project(args["session"]) if args.get("session") else None) or (
                        coord.task_get(args["task"])["project"] if args.get("task") and op.startswith("task_") else None
                    ) or "default"
                    need = PROJECT_ROLES["contributor"] if op in WRITE_OPS else PROJECT_ROLES["viewer"]
                    if oidc.project_role(info, project) < need:
                        raise CoordError("forbidden", f"no {'contributor' if need else 'viewer'} role "
                                         f"on project {project}")
            try:
                result = getattr(coord, op)(**args)
            except TypeError as e:
                raise CoordError("bad_args", str(e))
            if pusher is not None and op in a2a.TASK_OPS:
                pusher.notify(result["task"])                # webhooks: accepted / done / cancelled
            return result

        def _body(self):
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

        def do_POST(self):
            self._renewal = None
            if self.path == "/a2a":
                return self._a2a()
            if self.path != "/call":
                return self._send(404, {"ok": False, "error": "not_found"})
            try:
                req = self._body()
                principal, info = self._identity()
                result = self._run_op(req.get("op"), dict(req.get("args") or {}), principal, info)
                self._send(200, {"ok": True, "result": result})
            except CoordError as e:
                code = {"unauthenticated": 401, "forbidden": 403, "bad_op": 400, "bad_args": 400}.get(e.code, 409)
                self._send(code, {"ok": False, "error": e.code, "message": str(e), "data": e.data})
            except Exception as e:
                self._send(500, {"ok": False, "error": "internal", "message": repr(e)})

        def _a2a(self):
            send = lambda code, payload: self._send(code, payload, renewal_in_body=False)
            try:
                rpc = self._body()
            except ValueError as e:
                return send(200, {"jsonrpc": "2.0", "id": None, "error": {"code": a2a.PARSE_ERROR, "message": str(e)}})
            try:
                principal, info = self._identity()
            except CoordError as e:
                return send(401, {"jsonrpc": "2.0", "id": rpc.get("id") if isinstance(rpc, dict) else None,
                                  "error": {"code": a2a.COORD_ERROR, "message": str(e), "data": {"error": e.code}}})
            try:
                status, out = a2a.handle(rpc, lambda op, args: self._run_op(op, args, principal, info),
                                         pusher or a2a.Pusher(coord, []), principal)
            except Exception as e:
                status, out = 200, {"jsonrpc": "2.0", "id": rpc.get("id") if isinstance(rpc, dict) else None,
                                    "error": {"code": a2a.INTERNAL, "message": repr(e)}}
            send(status, out)
    return Handler


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("acp-agent-coordination")
    except Exception:
        return "0"


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, ssl.SSLError)):
            return          # client went away or failed the handshake: not a server error
        super().handle_error(request, client_address)


def build_server(coord, host="127.0.0.1", port=1337, tls_cert=None, tls_key=None, client_ca=None,
                 crl=None, oidc: OIDCIntrospector | None = None,
                 authority=None, cert_source=None, push_allow: list[str] | None = None,
                 public_url: str | None = None) -> ThreadingHTTPServer:
    """`authority`: a pki.Authority (mTLS with our management) or None - local and OIDC
    modes never load the PKI code. `cert_source` (certsource.py) renews the server's own
    certificate; with an authority it defaults to the local one."""
    if not is_loopback(host):
        if not (tls_cert and tls_key):
            raise SystemExit(f"refusing to listen on {host}: non-loopback requires --tls-cert/--tls-key")
        if not (client_ca or oidc):
            raise SystemExit(f"refusing to listen on {host}: non-loopback requires --client-ca (mTLS) "
                             "or --oidc-introspect-url")
    if client_ca and not (tls_cert and tls_key):
        raise SystemExit("--client-ca needs --tls-cert/--tls-key")
    pusher = a2a.Pusher(coord, push_allow or [], log=lambda m: print(f"coord-server: {m}", file=sys.stderr, flush=True))
    httpd = _Server((host, port), make_handler(coord, oidc, mtls=bool(client_ca), authority=authority, pusher=pusher,
                                               public_url=public_url, version=_version()))
    httpd.pusher = pusher
    httpd.refresh_server_cert = lambda: False
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
        if cert_source is None and authority is not None:
            from .certsource import LocalSource
            cert_source = LocalSource(tls_cert, tls_key, authority)
        if cert_source is not None:
            def refresh_server_cert() -> bool:
                """Let the source renew/pick up the server cert; load it live (new handshakes
                use it, open connections are untouched). True if a new cert is in use."""
                try:
                    if not cert_source.refresh():
                        return False
                    ctx.load_cert_chain(tls_cert, tls_key)
                except Exception as e:   # keep serving on the current cert, retry next check
                    print(f"coord-server: server certificate renewal ({cert_source.name}) failed: {e!r}",
                          file=sys.stderr, flush=True)
                    return False
                print(f"coord-server: server certificate renewed ({cert_source.name}: {tls_cert})",
                      file=sys.stderr, flush=True)
                return True
            httpd.refresh_server_cert = refresh_server_cert
    return httpd


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="coord-server", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--listen", default="127.0.0.1")
    p.add_argument("--port", type=int, default=1337)   # 1337 = "leet": an old hacker nod, kept since 0.1
    p.add_argument("--db", default=os.environ.get("COORD_DB") or str(state_home() / "coord2.db"),
                   help="default $COORD_DB, else coord2.db in the checkout or ~/.local/share/coord")
    p.add_argument("--pki", type=Path, help="mTLS with management's CA dir: client CA, server cert "
                   "(<dir>/localhost.crt/.key unless --tls-cert/--tls-key), live revocation and renewal")
    p.add_argument("--tls-cert"); p.add_argument("--tls-key")
    p.add_argument("--cert-source", choices=("none", "local", "watch", "command"),
                   help="how the server's own cert is renewed (default: local with --pki, else none)")
    p.add_argument("--renew-command", help="for --cert-source command, e.g. "
                   "'step ca renew --force {cert} {key}'")
    p.add_argument("--renew-after-days", type=float,
                   help="renew once this old or 2/3 of the lifetime, whichever comes first (default 15)")
    p.add_argument("--check-seconds", type=float,
                   help="how often to check the server cert (default 60 for watch, else 3600)")
    p.add_argument("--client-ca"); p.add_argument("--crl")
    p.add_argument("--push-allow", default="", help="A2A push-notification webhooks may target these hosts "
                   "(comma list; .suffix for a domain; loopback is always allowed), e.g. .dci.local")
    p.add_argument("--public-url", help="base URL advertised in the A2A agent card (default: from the request)")
    p.add_argument("--oidc-introspect-url"); p.add_argument("--oidc-client-id")
    p.add_argument("--oidc-client-secret")
    p.add_argument("--oidc-cache-seconds", type=int, default=60,
                   help="how long a token's introspection result is reused (a revoked token may work that long)")
    a = p.parse_args(argv)
    authority = None
    source_kind = a.cert_source or ("local" if a.pki else "none")
    if a.pki:
        from .pki import RENEW_AFTER_DAYS, Authority   # only mTLS with management needs the PKI code
        d = a.pki.resolve()
        authority = Authority(d, a.renew_after_days or RENEW_AFTER_DAYS)
        a.client_ca = a.client_ca or str(d / "ca.crt")
        a.tls_cert = a.tls_cert or str(d / "localhost.crt")
        a.tls_key = a.tls_key or str(d / "localhost.key")
    cert_source = None
    if source_kind != "none":
        if not (a.tls_cert and a.tls_key):
            p.error(f"--cert-source {source_kind} needs --tls-cert/--tls-key (or --pki)")
        from . import certsource
        kw = {"renew_after_days": a.renew_after_days} if a.renew_after_days else {}
        cert_source = certsource.make(source_kind, a.tls_cert, a.tls_key, command=a.renew_command,
                                      authority=authority, **kw)
    oidc = None
    if a.oidc_introspect_url:
        oidc = OIDCIntrospector(a.oidc_introspect_url, a.oidc_client_id or "",
                                os.environ.get("COORD_OIDC_SECRET") or a.oidc_client_secret or "",
                                cache_seconds=a.oidc_cache_seconds)
    try:
        httpd = build_server(Coord(a.db), a.listen, a.port, a.tls_cert, a.tls_key, a.client_ca, a.crl,
                             oidc, authority, cert_source,
                             push_allow=[h for h in a.push_allow.split(",") if h.strip()], public_url=a.public_url)
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        print(f"error: {a.listen}:{a.port} is already in use - another `coord-server` is probably "
              f"running (find it with `ss -ltnp | grep :{a.port}`), or pick another --port", file=sys.stderr)
        return 1
    if cert_source is not None:
        every = a.check_seconds or (60 if source_kind == "watch" else 3600)

        def watch_server_cert():   # at start, then every --check-seconds
            while True:
                httpd.refresh_server_cert()
                time.sleep(every)
        threading.Thread(target=watch_server_cert, name="server-cert-renewal", daemon=True).start()
    scheme = "https" if a.tls_cert else "http"
    mode = ", ".join(x for x in ("mTLS" if a.client_ca else "", "OIDC" if oidc else "",
                                 f"server cert: {source_kind}" if cert_source else "") if x)
    print(f"coord-server on {scheme}://{a.listen}:{a.port}" + (f" ({mode})" if mode else ""), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
