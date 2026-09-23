"""Client side of Keycloak/OIDC: get and refresh access tokens (stdlib only).

Access tokens live minutes, so a fixed COORD_TOKEN stops working
mid-session. `TokenProvider` keeps one valid instead:

  client credentials  COORD_OIDC_CLIENT_SECRET set - an agent running as a
                      Keycloak service account; no human involved
  device login        otherwise - a person runs `coord login` once (SSO in a
                      browser); the refresh token is kept and used from then on

Settings (COORD_* so they can live in ~/.config/coord/env): COORD_OIDC_ISSUER
(e.g. https://sso.example.com/realms/corp; endpoints are discovered) or
COORD_OIDC_TOKEN_URL, COORD_OIDC_CLIENT_ID, COORD_OIDC_CLIENT_SECRET or
COORD_OIDC_CLIENT_SECRET_FILE, COORD_OIDC_SCOPE (default "openid"; add
offline_access for agents that outlive the SSO session), COORD_OIDC_CA.

State (access token cache, refresh token) is one 0600 file per issuer and
client under ~/.config/coord/oidc/, updated under a file lock so parallel
agent processes don't race a rotating refresh token.
"""

import hashlib
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from .net import is_loopback
from .service import CoordError

try:
    import fcntl
except ImportError:  # Windows: no cross-process lock
    fcntl = None

EARLY = 30  # seconds: treat a token as expired this long before it really is


def _state_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "coord" / "oidc"


class TokenProvider:
    def __init__(self, client_id: str, issuer: str | None = None, token_url: str | None = None,
                 client_secret: str | None = None, scope: str = "openid", ca: str | None = None,
                 state_dir: Path | None = None, clock=time.time):
        if not (issuer or token_url):
            raise CoordError("oidc_config", "set COORD_OIDC_ISSUER (or COORD_OIDC_TOKEN_URL)")
        if not client_id:
            raise CoordError("oidc_config", "set COORD_OIDC_CLIENT_ID")
        self.client_id, self.issuer, self.client_secret = client_id, issuer, client_secret
        self.scope, self.clock = scope, clock
        self._endpoints = {"token_endpoint": token_url} if token_url else None
        base = issuer or token_url
        handlers = [urllib.request.ProxyHandler({})] if is_loopback(urllib.parse.urlsplit(base).hostname or "") else []
        handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca)))
        self.opener = urllib.request.build_opener(*handlers)
        key = hashlib.sha256(f"{base}|{client_id}".encode()).hexdigest()[:16]
        self.state_file = (state_dir or _state_dir()) / f"{key}.json"

    @classmethod
    def from_env(cls, env=os.environ) -> "TokenProvider":
        secret = env.get("COORD_OIDC_CLIENT_SECRET")
        if not secret and env.get("COORD_OIDC_CLIENT_SECRET_FILE"):
            secret = Path(env["COORD_OIDC_CLIENT_SECRET_FILE"]).expanduser().read_text().strip()
        return cls(env.get("COORD_OIDC_CLIENT_ID", ""), issuer=env.get("COORD_OIDC_ISSUER"),
                   token_url=env.get("COORD_OIDC_TOKEN_URL"), client_secret=secret,
                   scope=env.get("COORD_OIDC_SCOPE") or "openid", ca=env.get("COORD_OIDC_CA"))

    # --- plumbing ---------------------------------------------------------
    def _post(self, url: str, form: dict) -> tuple[int, dict]:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with self.opener.open(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {"error": f"http_{e.code}"}
        except urllib.error.URLError as e:
            raise CoordError("oidc_unreachable", f"cannot reach the identity provider: {e.reason}")

    def endpoint(self, name: str) -> str:
        if self._endpoints is None or (name not in self._endpoints and self.issuer):
            url = self.issuer.rstrip("/") + "/.well-known/openid-configuration"
            try:
                with self.opener.open(url, timeout=30) as r:
                    self._endpoints = json.loads(r.read())
            except (urllib.error.URLError, ValueError) as e:
                raise CoordError("oidc_unreachable", f"OIDC discovery failed at {url}: {e}")
        if not self._endpoints.get(name):
            raise CoordError("oidc_config", f"the identity provider has no {name}")
        return self._endpoints[name]

    def _auth(self) -> dict:
        form = {"client_id": self.client_id}
        if self.client_secret:
            form["client_secret"] = self.client_secret
        return form

    @contextmanager
    def _state(self):
        """Read-modify-write the state file under an exclusive lock."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.parent.chmod(0o700)
        with open(self.state_file.with_suffix(".lock"), "a") as lock:
            if fcntl:
                fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
                before = dict(state)
                try:
                    yield state
                finally:   # also on error: a spent refresh token must not stay on disk
                    if state != before:
                        tmp = self.state_file.with_suffix(".tmp")
                        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                        with os.fdopen(fd, "w") as f:
                            json.dump(state, f)
                        os.replace(tmp, self.state_file)
            finally:
                if fcntl:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def _store(self, state: dict, tok: dict) -> str:
        state["access_token"] = tok["access_token"]
        state["expires_at"] = self.clock() + float(tok.get("expires_in", 60))
        if tok.get("refresh_token"):   # Keycloak may rotate it: always keep the newest
            state["refresh_token"] = tok["refresh_token"]
        return state["access_token"]

    # --- what the client uses ---------------------------------------------
    def token(self) -> str:
        with self._state() as state:
            if state.get("access_token") and state.get("expires_at", 0) - EARLY > self.clock():
                return state["access_token"]
            if self.client_secret:
                status, tok = self._post(self.endpoint("token_endpoint"),
                                         {**self._auth(), "grant_type": "client_credentials", "scope": self.scope})
                if status != 200 or "access_token" not in tok:
                    raise CoordError("unauthenticated", "client-credentials login refused: "
                                     f"{tok.get('error_description') or tok.get('error')}")
                return self._store(state, tok)
            if not state.get("refresh_token"):
                raise CoordError("unauthenticated", "not logged in to SSO: run `coord login`")
            status, tok = self._post(self.endpoint("token_endpoint"),
                                     {**self._auth(), "grant_type": "refresh_token",
                                      "refresh_token": state["refresh_token"]})
            if status != 200 or "access_token" not in tok:
                state.clear()
                raise CoordError("unauthenticated", "SSO session expired or revoked "
                                 f"({tok.get('error_description') or tok.get('error')}): run `coord login`")
            return self._store(state, tok)

    def invalidate(self) -> None:
        """The server refused the token (revoked, clock skew): drop the cached one."""
        with self._state() as state:
            state.pop("access_token", None)
            state.pop("expires_at", None)

    def login(self, show=lambda msg: print(msg, file=sys.stderr, flush=True), sleep=time.sleep) -> dict:
        """OAuth 2.0 device authorization grant (RFC 8628): the person signs in via SSO."""
        status, dev = self._post(self.endpoint("device_authorization_endpoint"),
                                 {**self._auth(), "scope": self.scope})
        if status != 200 or "device_code" not in dev:
            raise CoordError("oidc_login", f"device login refused: {dev.get('error_description') or dev.get('error')}"
                             " (is 'OAuth 2.0 Device Authorization Grant' enabled on the client?)")
        show(f"Open {dev.get('verification_uri_complete') or dev['verification_uri']}"
             f" and confirm code {dev['user_code']}")
        interval, deadline = float(dev.get("interval", 5)), self.clock() + float(dev.get("expires_in", 600))
        while self.clock() < deadline:
            sleep(interval)
            status, tok = self._post(self.endpoint("token_endpoint"),
                                     {**self._auth(), "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                      "device_code": dev["device_code"]})
            if status == 200 and "access_token" in tok:
                with self._state() as state:
                    state.clear()
                    self._store(state, tok)
                return {"logged_in": True, "refresh_token": bool(tok.get("refresh_token"))}
            err = tok.get("error")
            if err == "slow_down":
                interval += 5
            elif err != "authorization_pending":
                raise CoordError("oidc_login", f"device login failed: {tok.get('error_description') or err}")
        raise CoordError("oidc_login", "device login timed out")

    def logout(self) -> dict:
        with self._state() as state:
            had = bool(state)
            state.clear()
        return {"logged_out": had}
