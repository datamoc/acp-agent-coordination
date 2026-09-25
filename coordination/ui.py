"""The server's graphical interface: `coord-server --ui`.

A second HTTP listener, loopback only, serving a small single-page app (coordination/ui/) that
calls the coordination service in-process - no certificate in the browser, no proxy. The human
takes part under an explicit identity, principal `ui:<name>`, with one session per project.

Access is a per-launch token: `http://127.0.0.1:<port>/?t=<token>` sets an HttpOnly,
SameSite=Strict cookie. Every request must carry that cookie and a loopback Host header (DNS
rebinding); every write must also come from the UI's own Origin (CSRF). The token lives only in
memory and in the link coord-server prints; `--ui` opens that link in a chromeless app window
(Edge / Chrome `--app`), else in the default browser.
"""

import contextlib
import getpass
import http.cookies
import inspect
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .service import READ_OPS, WRITE_OPS, CoordError

log = logging.getLogger("coord-server")
STATIC = Path(__file__).with_name("ui")
FILES = {"/": "index.html", "/app.js": "app.js", "/app.css": "app.css", "/logo.svg": "logo.svg",
         "/favicon.ico": "favicon.ico"}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
KEEPALIVE_SECONDS = 600


class HumanSessions:
    """One coord session per project for the person at the UI, kept alive while the UI runs."""

    def __init__(self, coord, name: str):
        self.coord, self.name = coord, name
        self.family = re.sub(r"[^A-Za-z0-9_-]", "-", name)[:40] or "human"
        self.principal = f"ui:{self.family}"
        self._by_project: dict[str, str] = {}
        self._lock = threading.Lock()

    def session(self, project: str) -> str:
        with self._lock:
            sid = self._by_project.get(project)
            if sid:
                try:
                    self.coord.heartbeat(sid, "at the coord UI")
                    return sid
                except CoordError:
                    pass                                        # expired: take a new one
            sid = self.coord.whoami("ui", project=project, principal=self.principal, user=self.family)["session_id"]
            self.coord.heartbeat(sid, "at the coord UI")
            self._by_project[project] = sid
            return sid

    def keepalive(self) -> None:
        with self._lock:
            ids = list(self._by_project.values())
        for sid in ids:
            with contextlib.suppress(CoordError):
                self.coord.heartbeat(sid, "at the coord UI")

    def end_all(self) -> None:
        with self._lock:
            ids, self._by_project = list(self._by_project.values()), {}
        for sid in ids:
            with contextlib.suppress(CoordError):
                self.coord.end(sid)
                pass


def _takes_session(op: str, coord) -> bool:
    return "session" in inspect.signature(getattr(coord, op)).parameters


def make_ui_handler(coord, token: str, humans: HumanSessions, port_ref: dict):
    class UIHandler(BaseHTTPRequestHandler):
        server_version = "coord-ui"

        def log_message(self, fmt, *args):
            # the request line carries the one-shot link (?t=...): replace the token, never log it
            log.debug("ui: " + fmt, *(str(a).replace(token, "<token>") for a in args))

        # --- guards ---------------------------------------------------------
        def _origins(self) -> set[str]:
            p = port_ref["port"]
            return {f"127.0.0.1:{p}", f"localhost:{p}"}

        def _host_ok(self) -> bool:
            return (self.headers.get("Host") or "") in self._origins()      # DNS rebinding

        def _cookie_ok(self) -> bool:
            c = http.cookies.SimpleCookie(self.headers.get("Cookie") or "")
            return "coord_ui" in c and secrets.compare_digest(c["coord_ui"].value, token)

        def _origin_ok(self) -> bool:                                    # CSRF: writes come from us
            origin = self.headers.get("Origin") or ""
            return origin in {f"http://{h}" for h in self._origins()}

        def _reply(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload) -> None:
            self._reply(code, json.dumps(payload, default=str).encode(), "application/json")

        # --- routes ---------------------------------------------------------
        def do_GET(self):
            if not self._host_ok():
                return self._json(403, {"ok": False, "error": "bad_host"})
            url = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(url.query)
            if url.path == "/" and q.get("t"):
                if not secrets.compare_digest(q["t"][0], token):
                    return self._json(403, {"ok": False, "error": "bad_token"})
                return self._reply(303, b"", "text/plain", {
                    "Location": "/", "Set-Cookie": f"coord_ui={token}; HttpOnly; SameSite=Strict; Path=/"})
            if not self._cookie_ok():
                return self._reply(403, "Open the coord UI from the link coord-server printed (…/?t=…).".encode(),
                                   "text/plain; charset=utf-8")
            if url.path in FILES:
                name = FILES[url.path]
                ctype = {".ico": "image/x-icon", ".svg": "image/svg+xml"}.get(Path(name).suffix)                     or mimetypes.guess_type(name)[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype.endswith(("javascript", "svg+xml")):
                    ctype += "; charset=utf-8"
                return self._reply(200, (STATIC / name).read_bytes(), ctype,
                                   {"Content-Security-Policy": CSP} if name.endswith(".html") else None)
            if url.path == "/api/state":
                info = coord.server_info()
                with coord._read() as db:
                    last = db.execute("SELECT COALESCE(MAX(event_id), 0) FROM events").fetchone()[0]
                return self._json(200, {"ok": True, "result": {
                    "me": {"name": humans.family, "principal": humans.principal}, "last_event": last,
                    "server": {k: info[k] for k in ("version", "features", "news", "limits")},
                    "projects": coord.projects()}})
            if url.path == "/api/events":
                return self._events(q)
            return self._json(404, {"ok": False, "error": "not_found"})

        def do_POST(self):
            if not (self._host_ok() and self._cookie_ok() and self._origin_ok()):
                return self._json(403, {"ok": False, "error": "forbidden"})
            if urllib.parse.urlsplit(self.path).path != "/api/call":
                return self._json(404, {"ok": False, "error": "not_found"})
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                op, args, project = req.get("op"), dict(req.get("args") or {}), req.get("project") or "default"
                if op not in READ_OPS | WRITE_OPS or op in ("whoami", "end"):
                    raise CoordError("bad_op", f"unknown op {op!r}")
                args.pop("principal", None)
                if _takes_session(op, coord) and not args.get("session"):
                    args["session"] = humans.session(project)
                elif args.get("session"):
                    coord.check_principal(args["session"], humans.principal)
                result = getattr(coord, op)(**args)
                self._json(200, {"ok": True, "result": result})
            except CoordError as e:
                self._json(200, {"ok": False, "error": e.code, "message": str(e), "data": e.data})
            except TypeError as e:
                self._json(200, {"ok": False, "error": "bad_args", "message": str(e)})

        def _events(self, q):
            project = (q.get("project") or [None])[0]
            after = int(self.headers.get("Last-Event-ID") or (q.get("after") or ["0"])[0] or 0)
            session = humans.session(project) if project else None   # the human's session: restricted projects need it
            try:
                coord.events(after=after, project=project, limit=1, session=session)   # view right up front
            except CoordError as e:
                return self._json(403, {"ok": False, "error": e.code, "message": str(e)})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.close_connection = True
            beat = time.monotonic()
            try:
                self.wfile.write(b": coord ui events\n\n")
                while True:
                    for e in coord.events(after=after, project=project, limit=200, session=session):
                        self.wfile.write(f"id: {e['event']}\ndata: {json.dumps(e)}\n\n".encode())
                        after = e["event"]
                    if time.monotonic() - beat > 15:
                        self.wfile.write(b": keep-alive\n\n")
                        beat = time.monotonic()
                    self.wfile.flush()
                    time.sleep(1)
            except (BrokenPipeError, ConnectionResetError, OSError, CoordError):
                pass

    return UIHandler


def app_browser() -> list[str] | None:
    """A Chromium-family browser for a chromeless app window (Edge is always there on Windows)."""
    candidates = []
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
            if base:
                candidates += [Path(base) / "Microsoft/Edge/Application/msedge.exe",
                               Path(base) / "Google/Chrome/Application/chrome.exe"]
    elif sys.platform == "darwin":
        candidates += [Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                       Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    for c in candidates:
        if c.exists():
            return [str(c)]
    for name in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return [found]
    return None


def open_window(url: str, mode: str) -> str:
    """mode: auto (app window, else the default browser), app, browser, none. Returns what it did."""
    if mode == "none":
        return "none"
    exe = app_browser() if mode in ("auto", "app") else None
    if exe:
        subprocess.Popen([*exe, f"--app={url}", "--window-size=1440,900"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "app"
    if mode == "app":
        return "none"
    webbrowser.open(url)
    return "browser"


def start_ui(coord, port: int = 0, name: str | None = None, open_mode: str = "auto") -> dict:
    """Start the UI listener in the background; returns {url, port, token, opened, humans}."""
    token = secrets.token_urlsafe(24)
    humans = HumanSessions(coord, name or getpass.getuser())
    port_ref = {"port": port}
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_ui_handler(coord, token, humans, port_ref))
    httpd.daemon_threads = True
    port_ref["port"] = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, name="coord-ui", daemon=True).start()

    def keepalive():
        while True:
            time.sleep(KEEPALIVE_SECONDS)
            humans.keepalive()
    threading.Thread(target=keepalive, name="coord-ui-keepalive", daemon=True).start()
    url = f"http://127.0.0.1:{port_ref['port']}/?t={token}"
    return {"url": url, "port": port_ref["port"], "token": token, "httpd": httpd, "humans": humans,
            "opened": open_window(url, open_mode)}
