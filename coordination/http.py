"""JSON-over-HTTP transport: POST /call {"op": ..., "args": {...}}.

Loopback by default with no auth. Any non-loopback bind requires TLS plus
an identity method: mTLS (client certs from pki.py, optional CRL) or OIDC
bearer tokens validated by token introspection (Keycloak etc). When an
identity is present, sessions are bound to it at whoami time.
"""

import ipaddress
import json
import ssl
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .service import READ_OPS, WRITE_OPS, CoordError

PROJECT_ROLES = {"viewer": 0, "contributor": 1, "admin": 2}


def is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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


def make_handler(coord, oidc: OIDCIntrospector | None, mtls: bool):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, code: int, payload: dict):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _identity(self):
            if mtls:
                cert = self.connection.getpeercert()
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
                 crl=None, oidc: OIDCIntrospector | None = None) -> ThreadingHTTPServer:
    if not is_loopback(host):
        if not (tls_cert and tls_key):
            raise SystemExit(f"refusing to listen on {host}: non-loopback requires --tls-cert/--tls-key")
        if not (client_ca or oidc):
            raise SystemExit(f"refusing to listen on {host}: non-loopback requires --client-ca (mTLS) "
                             "or --oidc-introspect-url")
    if client_ca and not (tls_cert and tls_key):
        raise SystemExit("--client-ca needs --tls-cert/--tls-key")
    httpd = ThreadingHTTPServer((host, port), make_handler(coord, oidc, mtls=bool(client_ca)))
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


class RemoteCoord:
    """Client proxy: attribute access becomes a /call round-trip."""

    def __init__(self, url: str, ca=None, cert=None, key=None, token=None, insecure=False):
        self.url = url.rstrip("/") + "/call"
        self.token = token
        host = urllib.parse.urlsplit(self.url).hostname or ""
        handlers = [urllib.request.ProxyHandler({})] if is_loopback(host) else []
        self.ctx = None
        if self.url.startswith("https"):
            self.ctx = ssl.create_default_context(cafile=ca)
            if insecure:
                self.ctx.check_hostname = False
                self.ctx.verify_mode = ssl.CERT_NONE
            if cert:
                self.ctx.load_cert_chain(cert, key)
        handlers.append(urllib.request.HTTPSHandler(context=self.ctx))
        self.opener = urllib.request.build_opener(*handlers)

    def __getattr__(self, op):
        def call(**args):
            headers = {"Content-Type": "application/json"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            req = urllib.request.Request(self.url, data=json.dumps({"op": op, "args": args}).encode(),
                                         headers=headers)
            try:
                with self.opener.open(req, timeout=30) as r:
                    payload = json.loads(r.read())
            except urllib.error.HTTPError as e:
                payload = json.loads(e.read() or b"{}")
            if not payload.get("ok"):
                raise CoordError(payload.get("error", "error"), payload.get("message", ""), payload.get("data"))
            return payload["result"]
        return call
