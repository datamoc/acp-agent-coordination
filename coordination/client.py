"""Client: proxy to a `coord-server` - attribute access becomes a /call round-trip.

The client only holds the identity bundle the administrator gave it (CA
cert, its cert and key). It knows nothing about the CA: when the server
hands back a renewed certificate ("certificate" in a response), the client
checks it matches its own key and replaces its cert file in place.
"""

import json
import os
import ssl
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .net import is_loopback
from .service import CoordError


class RemoteCoord:
    def __init__(self, url: str, ca=None, cert=None, key=None, token=None, insecure=False):
        self.url = url.rstrip("/") + "/call"
        self.token, self.cert, self.key = token, cert, key
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

    def _install_renewal(self, pem: str) -> None:
        """Atomically replace our cert with the renewed one - only if it fits our private key."""
        if not self.cert:
            return
        path = Path(self.cert)
        if path.exists() and path.read_text().strip() == pem.strip():
            return
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".renew-", suffix=".crt")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(pem)
            ssl.create_default_context().load_cert_chain(tmp, self.key)   # raises on key mismatch
            os.replace(tmp, path)
        except (OSError, ssl.SSLError) as e:
            Path(tmp).unlink(missing_ok=True)
            print(f"coord: could not install the renewed certificate: {e}", file=sys.stderr)
            return
        self.ctx.load_cert_chain(self.cert, self.key)
        print(f"coord: certificate renewed by the server ({path})", file=sys.stderr)

    def __getattr__(self, op):
        if op.startswith("_"):
            raise AttributeError(op)

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
            if payload.get("certificate"):
                self._install_renewal(payload["certificate"])
            if not payload.get("ok"):
                raise CoordError(payload.get("error", "error"), payload.get("message", ""), payload.get("data"))
            return payload["result"]
        return call
