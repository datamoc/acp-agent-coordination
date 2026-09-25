"""A2A v1.0 binding of coord (JSON-RPC over HTTP; https://a2a-protocol.org).

coord is an A2A agent. Its Agent Card is served at /.well-known/agent-card.json
and its JSON-RPC endpoint is /a2a:

  SendMessage with a data part {"op": <coord op>, "args": {...}}
      -> any coord operation (claims, messages, documents, ... - schema/ops.json);
         the reply is a Message whose data part is the op's result.
  SendMessage with text parts and metadata {"coord": {"session": <id>, "assign": <name>,
      "title": ...}}   -> delegation: creates a coord task and returns it as an A2A Task.
  GetTask / ListTasks / CancelTask        -> coord tasks as A2A tasks (open and offered =
      SUBMITTED, accepted=WORKING, done=COMPLETED, cancelled=CANCELED); an assigned task is an
      offer until the assignee accepts it.
  Create/Get/List/DeleteTaskPushNotificationConfig -> webhooks: the server POSTs a
      StreamResponse {"statusUpdate": ...} when a task changes - no polling.

Claims, fences, documents and the rest have no A2A counterpart: they travel as
data-part ops. A renewed mTLS client certificate comes back in the
`Coord-Certificate` response header (base64 PEM). Only the standard library.
"""

import base64
import ipaddress
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .net import is_loopback, prefer_loopback_ipv4
from .service import FEATURES, CoordError

A2A_VERSION = "1.0"
CONTENT_TYPE = "application/a2a+json"
STATE = {"open": "TASK_STATE_SUBMITTED", "offered": "TASK_STATE_SUBMITTED", "accepted": "TASK_STATE_WORKING",
         "done": "TASK_STATE_COMPLETED", "cancelled": "TASK_STATE_CANCELED"}
STATE_BACK = {"TASK_STATE_SUBMITTED": ("open", "offered"), "TASK_STATE_WORKING": ("accepted",),
              "TASK_STATE_COMPLETED": ("done",), "TASK_STATE_CANCELED": ("cancelled",),
              "TASK_STATE_CANCELLED": ("cancelled",)}
# JSON-RPC / A2A error codes (same values as @a2a-js/sdk)
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL = -32700, -32600, -32601, -32602, -32603
TASK_NOT_FOUND, TASK_NOT_CANCELABLE, UNSUPPORTED, EXTENDED_CARD = -32001, -32002, -32004, -32007
COORD_ERROR = -32000   # a coord refusal (conflict, forbidden, ...): data.error holds the coord code
TASK_OPS = {"task_accept", "task_done", "task_cancel", "task_decline"}


class A2AError(Exception):
    def __init__(self, code: int, message: str, data=None, http: int = 200):
        super().__init__(message)
        self.code, self.data, self.http = code, data, http


def agent_card(base_url: str, mtls: bool, oidc_url: str | None, version: str) -> dict:
    schemes, requirements = {}, []
    if mtls:
        schemes["mtls"] = {"mtlsSecurityScheme": {"description": "client certificate from coord-admin enroll"}}
        requirements.append({"schemes": {"mtls": {"list": []}}})
    if oidc_url:
        schemes["oidc"] = {"openIdConnectSecurityScheme": {"openIdConnectUrl": oidc_url,
                                                           "description": "Keycloak bearer token"}}
        requirements.append({"schemes": {"oidc": {"list": []}}})
    def skill(sid, name, desc, tags, ex):
        return {"id": sid, "name": name, "description": desc, "tags": tags,
                "examples": ex, "inputModes": ["application/json"], "outputModes": ["application/json"]}
    return {
        "name": "coord",
        "description": "Coordination for concurrent agent sessions: claims on files/dirs, messages, "
                       "tasks, discussions, documents, shared memory.",
        "supportedInterfaces": [{"url": base_url.rstrip("/") + "/a2a", "protocolBinding": "JSONRPC",
                                 "protocolVersion": A2A_VERSION}],
        "provider": {"organization": "coord", "url": base_url},
        "version": version,
        "capabilities": {"streaming": False, "pushNotifications": True, "extendedAgentCard": False},
        "securitySchemes": schemes,
        "securityRequirements": requirements,
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            skill("coord-ops", "coord operations",
                  'Any coord operation: send a data part {"op": "<name>", "args": {...}} (see schema/ops.json).',
                  ["coordination", *FEATURES],
                  ['{"op": "whoami", "args": {"family": "my-agent", "project": "github.com/org/repo"}}',
                   '{"op": "claim", "args": {"session": "<id>", "scope": "src/auth/", "tree": true}}']),
            skill("delegate", "delegate a task",
                  'Text parts describe the work; metadata {"coord": {"session": "<id>", "assign": "<name>"}} '
                  "creates a coord task, returned as an A2A Task; follow it with GetTask or push notifications.",
                  ["tasks", "delegation"], ["Review the parser changes (assign: codex-01)"]),
        ],
    }


def task_to_a2a(t: dict) -> dict:
    status = {"state": STATE.get(t["status"], "TASK_STATE_UNSPECIFIED"), "timestamp": t.get("updated_at")}
    if t.get("note"):
        status["message"] = message([{"text": t["note"]}], context=t["project"], task=t["task"])
    return {"id": t["task"], "contextId": t["project"], "status": status, "artifacts": [], "history": [],
            "metadata": {"coord": {k: t.get(k) for k in ("title", "description", "assigned", "created_by",
                                                          "priority", "claim", "category")}}}


def message(parts: list, context: str = "", task: str = "", metadata: dict | None = None) -> dict:
    m = {"messageId": str(uuid.uuid4()), "role": "ROLE_AGENT", "parts": parts}
    if context:
        m["contextId"] = context
    if task:
        m["taskId"] = task
    if metadata:
        m["metadata"] = metadata
    return m


def host_allowed(url: str, allow: list[str]) -> bool:
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    h = u.hostname.lower()
    if is_loopback(h):
        return True
    try:
        ipaddress.ip_address(h)
        literal = True
    except ValueError:
        literal = False
    for a in allow:
        a = a.strip().lower()
        if a == "*" or h == a.lstrip(".") or (a.startswith(".") and not literal and h.endswith(a)):
            return True
    return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A push follows no redirect. `host_allowed` is checked once, before the request; the default
    opener would follow a 302 and post the same body - and the same Authorization header - to a
    host nobody allow-listed, which is the SSRF the allow-list exists to prevent."""

    def http_error_302(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(
            req.full_url, code,
            "redirect refused: the push allow-list was checked for this URL, not for the next one",
            headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _opener(host: str) -> urllib.request.OpenerDirector:
    """How a push is sent: no redirects, and never through the developer's proxy for a loopback
    hook (ProxyHandler({}) turns the environment's proxy off)."""
    handlers: list = [_NoRedirect]
    if is_loopback(host):
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


class Pusher:
    """Deliver task status updates to registered webhooks (in the background)."""

    def __init__(self, coord, allow: list[str], timeout: float = 10.0, log=None):
        self.coord, self.allow, self.timeout = coord, allow, timeout
        self.log = log or (lambda m: None)
        self.sent: list[tuple[str, int]] = []   # (url, http status) - for tests and diagnostics

    def notify(self, task: str) -> list[threading.Thread]:
        try:
            t, configs = self.coord.task_get(task), self.coord.push_list(task)
        except CoordError:
            return []
        body = json.dumps({"statusUpdate": {"taskId": t["task"], "contextId": t["project"],
                                            "status": task_to_a2a(t)["status"],
                                            "metadata": {"coord": {"title": t["title"]}}}}).encode()
        threads = []
        for c in configs:
            th = threading.Thread(target=self._post, args=(c, body), daemon=True)
            th.start()
            threads.append(th)
        return threads

    def wake(self, wake_id: int) -> threading.Thread | None:
        """Deliver a wake-up request to its hook (in the background) and record delivered or failed."""
        plan = self.coord._wake_delivery_plan(wake_id)
        if plan is None:
            return None
        if not host_allowed(plan["url"], self.allow):
            self.coord._wake_delivered(wake_id, False, f"hook {plan['url']} is not an allowed host (--push-allow)")
            return None

        def send():
            headers = {"Content-Type": "application/json"}
            if plan["token"]:
                headers["Authorization"] = f"Bearer {plan['token']}"
            host = urllib.parse.urlsplit(plan["url"]).hostname or ""
            opener = _opener(host)
            try:
                with opener.open(urllib.request.Request(prefer_loopback_ipv4(plan["url"]), data=plan["body"],
                                                         headers=headers),
                                 timeout=self.timeout) as r:
                    self.sent.append((plan["url"], r.status))
                    self.coord._wake_delivered(wake_id, 200 <= r.status < 300, f"HTTP {r.status}")
            except Exception as e:     # a dead hook must not affect the server: the request says failed
                self.sent.append((plan["url"], getattr(e, "code", 0)))
                self.coord._wake_delivered(wake_id, False, repr(e)[:300])
                self.log(f"wake hook {plan['url']} failed: {e!r}")
        th = threading.Thread(target=send, daemon=True)
        th.start()
        return th

    def _post(self, c: dict, body: bytes) -> None:
        headers = {"Content-Type": CONTENT_TYPE}
        if c.get("auth_scheme") and c.get("auth_credentials"):
            headers["Authorization"] = f"{c['auth_scheme']} {c['auth_credentials']}"
        elif c.get("token"):
            headers["X-A2A-Notification-Token"] = c["token"]
        host = urllib.parse.urlsplit(c["url"]).hostname or ""
        if not host_allowed(c["url"], self.allow):     # registration checked it; --push-allow may have changed
            self.sent.append((c["url"], 0))
            self.log(f"push to {c['url']} refused: not an allowed host (--push-allow)")
            return
        opener = _opener(host)
        try:
            with opener.open(urllib.request.Request(prefer_loopback_ipv4(c["url"]), data=body,
                                                     headers=headers), timeout=self.timeout) as r:
                self.sent.append((c["url"], r.status))
        except Exception as e:   # a dead webhook must not affect the server
            self.sent.append((c["url"], getattr(e, "code", 0)))
            self.log(f"push to {c['url']} failed: {e!r}")


def _rpc_error(rid, code: int, msg: str, data=None) -> dict:
    err = {"code": code, "message": msg}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def _coord_error(e: CoordError) -> A2AError:
    code = {"missing": TASK_NOT_FOUND, "not_cancelable": TASK_NOT_CANCELABLE, "bad_args": INVALID_PARAMS,
            "bad_op": INVALID_PARAMS}.get(e.code, COORD_ERROR)
    http = {"unauthenticated": 401, "forbidden": 403}.get(e.code, 200)
    return A2AError(code, str(e), {"error": e.code, "data": e.data}, http)


def handle(rpc, run_op, pusher: Pusher, principal: str | None) -> tuple[int, dict]:
    """One JSON-RPC request -> (HTTP status, JSON-RPC response). `run_op(op, args)` runs a coord
    op with the caller's identity checks and raises CoordError."""
    rid = rpc.get("id") if isinstance(rpc, dict) else None
    if not isinstance(rpc, dict) or rpc.get("jsonrpc") != "2.0" or not isinstance(rpc.get("method"), str):
        return 200, _rpc_error(rid, INVALID_REQUEST, "Invalid JSON-RPC Request.")
    method, params = rpc["method"], rpc.get("params") or {}
    if not isinstance(params, dict):
        return 200, _rpc_error(rid, INVALID_PARAMS, "params must be an object")
    try:
        return 200, {"jsonrpc": "2.0", "id": rid, "result": _dispatch(method, params, run_op, pusher, principal)}
    except CoordError as e:
        err = _coord_error(e)
        return err.http, _rpc_error(rid, err.code, str(err), err.data)
    except A2AError as e:
        return e.http, _rpc_error(rid, e.code, str(e), e.data)


def _task_id(params: dict, key: str = "id") -> str:
    tid = params.get(key)
    if not isinstance(tid, str) or not tid:
        raise A2AError(INVALID_PARAMS, f"{key} is required")
    return tid


def _session(params: dict, msg: dict | None = None) -> str:
    meta = (msg or {}).get("metadata") or params.get("metadata") or {}
    s = (meta.get("coord") or {}).get("session")
    if not s:
        raise A2AError(INVALID_PARAMS, 'metadata {"coord": {"session": "<id>"}} is required (from the whoami op)')
    return s


def _push_json(c: dict) -> dict:
    out = {"id": c["id"], "taskId": c["task"], "url": c["url"]}
    if c.get("token"):
        out["token"] = c["token"]
    if c.get("auth_scheme"):
        out["authentication"] = {"scheme": c["auth_scheme"], "credentials": c.get("auth_credentials") or ""}
    return out


def _dispatch(method, params, run_op, pusher, principal):
    if method == "SendMessage":
        msg = params.get("message") or {}
        parts = msg.get("parts") or []
        ops = [p["data"] for p in parts if isinstance(p, dict) and isinstance(p.get("data"), dict) and "op" in p["data"]]
        if ops:
            d = ops[0]
            if not isinstance(d.get("args", {}), dict):
                raise A2AError(INVALID_PARAMS, "data.args must be an object")
            result = run_op(d["op"], dict(d.get("args") or {}))
            return {"message": message([{"data": result, "mediaType": "application/json"}],
                                       context=msg.get("contextId", ""), metadata={"coord": {"op": d["op"]}})}
        text = "\n".join(p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)).strip()
        if not text:
            raise A2AError(INVALID_PARAMS, 'send a data part {"op": ..., "args": ...}, or text to delegate a task')
        meta = (msg.get("metadata") or {}).get("coord") or {}
        created = run_op("task_create", {"session": _session(params, msg), "title": meta.get("title") or text.splitlines()[0][:120],
                                         "description": text, "assign": meta.get("assign"), "claim": meta.get("claim"),
                                         "priority": int(meta.get("priority") or 0), "category": meta.get("category")})
        return {"task": task_to_a2a(run_op("task_get", {"task": created["task"]}))}
    if method == "GetTask":
        return task_to_a2a(run_op("task_get", {"task": _task_id(params)}))
    if method == "ListTasks":
        status = params.get("status")
        if status and status not in STATE_BACK and status != "TASK_STATE_UNSPECIFIED":
            raise A2AError(INVALID_PARAMS, f"unknown status {status}")
        project = params.get("contextId") or None
        rows = [r for st in STATE_BACK.get(status, (None,)) for r in run_op("tasks", {"project": project, "status": st})]
        rows.sort(key=lambda r: int(r["task"][1:]))
        size = max(1, min(int(params.get("pageSize") or 50), 100))
        start = int(params.get("pageToken") or 0)
        page = rows[start:start + size]
        tasks = [task_to_a2a(run_op("task_get", {"task": r["task"]})) for r in page]
        nxt = str(start + size) if start + size < len(rows) else ""
        return {"tasks": tasks, "nextPageToken": nxt, "pageSize": size, "totalSize": len(rows)}
    if method == "CancelTask":
        tid = _task_id(params)
        run_op("task_cancel", {"session": _session(params), "task": tid})
        return task_to_a2a(run_op("task_get", {"task": tid}))
    if method == "CreateTaskPushNotificationConfig":
        tid, url = _task_id(params, "taskId"), params.get("url")
        if not isinstance(url, str) or not host_allowed(url, pusher.allow):
            raise A2AError(INVALID_PARAMS, f"webhook {url!r} is not allowed: loopback or --push-allow hosts only")
        run_op("task_get", {"task": tid})                      # caller must be able to see the task
        auth = params.get("authentication") or {}
        return _push_json(pusher.coord.push_create(tid, url, token=params.get("token") or None,
                                                   auth_scheme=auth.get("scheme"), auth_credentials=auth.get("credentials"),
                                                   principal=principal, config_id=params.get("id") or None))
    if method in ("GetTaskPushNotificationConfig", "DeleteTaskPushNotificationConfig"):
        tid = _task_id(params, "taskId")
        run_op("task_get", {"task": tid})
        if method == "DeleteTaskPushNotificationConfig":
            pusher.coord.push_delete(tid, _task_id(params))
            return {}
        return _push_json(pusher.coord.push_get(tid, _task_id(params)))
    if method == "ListTaskPushNotificationConfigs":
        tid = _task_id(params, "taskId")
        run_op("task_get", {"task": tid})
        return {"configs": [_push_json(c) for c in pusher.coord.push_list(tid)], "nextPageToken": ""}
    if method in ("SendStreamingMessage", "SubscribeToTask"):
        raise A2AError(UNSUPPORTED, f"{method} needs streaming, which this agent does not offer: use push notifications")
    if method == "GetExtendedAgentCard":
        raise A2AError(EXTENDED_CARD, "no extended agent card")
    raise A2AError(METHOD_NOT_FOUND, f"method {method!r} not found")


def certificate_header(pem: str) -> str:
    return base64.b64encode(pem.encode()).decode()
