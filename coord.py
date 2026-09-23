"""coord - v2 coordination client CLI (local SQLite by default, COORD_SERVER for remote).

The agents' only command. The server is `coord-server` (coordination/server.py),
certificate management `coord-admin` (coordination/pki.py).

Session: `coord whoami claude` prints `export COORD_SESSION=<uuid>` and also
saves it to .coord-session at the repo root (env wins). Every command takes
--json. See README "Coordination v2".
"""

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from coordination.scopes import canonical_project
from coordination.service import MEMORY_KINDS, MESSAGE_KINDS, ROLES, STANCES, DOC_KINDS, Coord, CoordError


def git(*args) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def repo_root() -> Path:
    top = git("rev-parse", "--show-toplevel")
    return Path(top) if top else Path.cwd()


def detect_project() -> str:
    return os.environ.get("COORD_PROJECT") or canonical_project(git("remote", "get-url", "origin"))


def session_file() -> Path:
    return repo_root() / ".coord-session"


def load_session() -> str | None:
    if os.environ.get("COORD_SESSION"):
        return os.environ["COORD_SESSION"]
    f = session_file()
    return f.read_text().strip() if f.exists() else None


def rel(path: str) -> str:
    """Make a user-given path repo-relative (absolute paths inside the repo are OK)."""
    p = Path(path)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(repo_root().resolve()).as_posix() + ("/" if path.endswith(("/", "\\")) else "")
        except ValueError:
            return path
    return path


CONFIG_PATHS = ("COORD_CA", "COORD_CERT", "COORD_KEY", "COORD_DB")


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "coord"


def config_file() -> Path:
    if os.environ.get("COORD_CONFIG"):
        return Path(os.environ["COORD_CONFIG"]).expanduser()
    if os.environ.get("COORD_IDENTITY"):
        return config_home() / os.environ["COORD_IDENTITY"] / "env"
    return config_home() / "env"


def load_config() -> Path | None:
    """Fill unset COORD_* variables from the identity's config file.

    $COORD_CONFIG, else ~/.config/coord/$COORD_IDENTITY/env, else
    ~/.config/coord/env (written by `coord-admin enroll`). KEY=value lines,
    # comments; the environment always wins. Relative paths resolve against
    the file's real directory, so it works from any cwd and after reboots."""
    f = config_file()
    if not f.is_file():
        if os.environ.get("COORD_IDENTITY") and not os.environ.get("COORD_CONFIG"):
            raise SystemExit(f"coord: no identity {os.environ['COORD_IDENTITY']!r} "
                             f"({f} missing) - ask the administrator for `coord-admin enroll <client-name>`")
        return None
    f = f.resolve()
    for line in f.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if not sep or not key.startswith("COORD_") or key in os.environ:
            continue
        if key in CONFIG_PATHS and value:
            value = str((f.parent / Path(value).expanduser()).resolve())
        os.environ[key] = value
    return f


def token_source():
    """A fixed COORD_TOKEN wins; with COORD_OIDC_* settings, a self-refreshing Keycloak token."""
    if os.environ.get("COORD_TOKEN"):
        return os.environ["COORD_TOKEN"]
    if os.environ.get("COORD_OIDC_ISSUER") or os.environ.get("COORD_OIDC_TOKEN_URL"):
        from coordination.oidc_client import TokenProvider   # only OIDC mode loads it
        return TokenProvider.from_env()
    return None


def backend():
    url = os.environ.get("COORD_SERVER")
    if url:
        from coordination.client import RemoteCoord   # local mode never loads the network client
        return RemoteCoord(url, ca=os.environ.get("COORD_CA"), cert=os.environ.get("COORD_CERT"),
                           key=os.environ.get("COORD_KEY"), token=token_source(),
                           insecure=os.environ.get("COORD_INSECURE") == "1")
    return Coord(os.environ.get("COORD_DB") or repo_root() / "coord2.db")


def read_content(a) -> str:
    if getattr(a, "file", None):
        return Path(a.file).read_text(encoding="utf-8")
    if getattr(a, "content", None) is not None:
        return a.content
    return sys.stdin.read()


# --- human output --------------------------------------------------------
def fmt_msg(m):
    to = f" -> {m['to']}" if m.get("to") else ""
    re_ = f" re #{m['reply_to']}" if m.get("reply_to") else ""
    cl = f" [{m['claim']}]" if m.get("claim") else ""
    done = f"  (resolved by {m['resolved_by']}: {m['resolution']})" if m.get("resolved_at") else ""
    return f"#{m['id']} [{m['at']}] {m['from']}{to} {m['kind']}{re_}{cl}: {m['body']}{done}"


def fmt_claim(c):
    return (f"{c['claim']} {c['scope']} ({c['scope_type']}) {c['owner']} fence={c['fence']} "
            f"until {c['expires_at']}" + (f" - {c['note']}" if c["note"] else "")
            + (" [released]" if c.get("released") else ""))


def human(cmd, r):
    if cmd in ("inbox", "thread"):
        return "\n".join(fmt_msg(m) for m in r) or "(no messages)"
    if cmd == "locks":
        return "\n".join(fmt_claim(c) for c in r) or "(no active claims)"
    if cmd == "claim" or cmd == "renew":
        return fmt_claim(r)
    if cmd == "poll":
        out = [fmt_msg(m) for m in r["messages"]] or ["(no new messages)"]
        out += ["claims: " + (", ".join(f"{c['claim']} {c['scope']}" for c in r["my_claims"]) or "-")]
        out += ["tasks: " + (", ".join(f"{t['task']} {t['title']}" for t in r["tasks"]) or "-")]
        out += ["discussions: " + (", ".join(f"{d['discussion']} {d['topic']}" for d in r["discussions"]) or "-")]
        return "\n".join(out)
    if cmd == "doc-show":
        return f"{r['document']} r{r['revision']}/{r['latest_revision']} [{r['kind']}/{r['status']}] {r['title']}\n\n{r['content']}"
    if cmd == "tasks":
        return "\n".join(f"{t['task']} [{t['status']}] p{t['priority']} {t['title']}"
                         + (f" ({t['assigned']})" if t["assigned"] else "") for t in r) or "(no tasks)"
    if cmd == "discussion":
        lines = [f"{r['discussion']} [{r['status']}] {r['topic']} (by {r['created_by']})"]
        for p in r["proposals"]:
            t = " ".join(f"{k}={v}" for k, v in p["tally"].items() if v)
            lines.append(f"  {p['proposal']} [{p['status']}] {p['author']}: {p['body']}  {t}")
        if r["decision"]:
            lines.append(f"  decided by {r['decided_by']} (consensus={'yes' if r['consensus'] else 'no'}): "
                         f"{r['decision']} -> {r['decision_document']}")
        return "\n".join(lines)
    if cmd == "memory":
        return "\n\n".join(f"{m['memory']} [{m['kind']}] {m['title']} (r{m['revision']}, {m['updated_by']})\n{m['content']}"
                           for m in r) or "(no memory)"
    if cmd == "context":
        lines = [f"you: {r['me']['name']} gen {r['me']['generation']} project {r['me']['project']}"
                 f" | unread {r['unread']}"]
        lines += [f"overview: {m['title']}: {m['content']}" for m in r["overview"]]
        lines += [f"memory: {m['memory']} [{m['kind']}] {m['title']}" for m in r["memory"]]
        lines += [f"claim: {c['claim']} {c['scope']}" for c in r["my_claims"]]
        lines += [f"task: {t['task']} [{t['status']}] {t['title']}" for t in r["my_tasks"] + r["open_tasks"]]
        lines += [f"discussion: {d['discussion']} {d['topic']}" for d in r["discussions"]]
        return "\n".join(lines)
    if cmd == "check":
        if r["ok"]:
            return "ok: no staged file is claimed by another session"
        return "\n".join(f"CLAIMED {c['file']} by {c['owner']} ({c['claim']} {c['scope']})" for c in r["conflicts"])
    if isinstance(r, list):
        return "\n".join(json.dumps(x) for x in r) or "(none)"
    return json.dumps(r) if not isinstance(r, str) else r


# --- commands ------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(prog="coord", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = lambda name, **kw: sub.add_parser(name, **kw)

    s = a("whoami"); s.add_argument("family"); s.add_argument("--project")
    s = a("heartbeat"); s.add_argument("status", nargs="?", default="")
    a("end")
    a("login", help="SSO device login (Keycloak): sign in once in a browser; tokens refresh by themselves")
    a("logout", help="forget the stored SSO tokens")
    s = a("presence"); s.add_argument("--all", action="store_true"); s.add_argument("--project")

    s = a("post"); s.add_argument("body"); s.add_argument("--to"); s.add_argument("--kind", default="info", choices=MESSAGE_KINDS)
    s.add_argument("--claim"); s.add_argument("--client-id")
    s = a("reply"); s.add_argument("message", type=int); s.add_argument("body")
    s.add_argument("--kind", default="info", choices=MESSAGE_KINDS); s.add_argument("--client-id")
    s = a("inbox"); s.add_argument("--after", type=int); s.add_argument("--to-me", action="store_true")
    s.add_argument("--from", dest="sender"); s.add_argument("--kind"); s.add_argument("--limit", type=int, default=20)
    s.add_argument("--all", action="store_true", help="no limit"); s.add_argument("--unresolved", action="store_true")
    s.add_argument("--project")
    s = a("thread"); s.add_argument("message", type=int)
    s = a("resolve"); s.add_argument("message", type=int); s.add_argument("resolution", nargs="?", default="")
    a("poll")

    s = a("claim"); s.add_argument("scope"); s.add_argument("--tree", action="store_true")
    s.add_argument("--note", default=""); s.add_argument("--ttl", type=int, default=7200)
    s.add_argument("--release-on-commit", action="store_true"); s.add_argument("--client-id")
    s = a("renew"); s.add_argument("claim"); s.add_argument("--ttl", type=int, default=7200)
    s = a("release"); s.add_argument("claim", nargs="?"); s.add_argument("--all", action="store_true")
    s = a("locks"); s.add_argument("--all", action="store_true"); s.add_argument("--project")
    s = a("fence-check"); s.add_argument("claim"); s.add_argument("fence", type=int)
    s = a("grant"); s.add_argument("claim"); s.add_argument("to"); s.add_argument("role", choices=ROLES)
    s = a("revoke"); s.add_argument("claim"); s.add_argument("to"); s.add_argument("role", choices=ROLES)
    s = a("roles"); s.add_argument("claim")
    s = a("ask"); s.add_argument("body"); s.add_argument("--claim", required=True); s.add_argument("--to", required=True)
    s.add_argument("--role", default="advisor", choices=ROLES); s.add_argument("--kind", default="question", choices=MESSAGE_KINDS)

    s = a("check"); s.add_argument("files", nargs="*")
    s = a("post-commit"); s.add_argument("--sha")
    a("install-hooks")

    s = a("discuss"); s.add_argument("topic"); s.add_argument("--claim")
    s = a("propose"); s.add_argument("discussion"); s.add_argument("body")
    s = a("react"); s.add_argument("proposal"); s.add_argument("stance", choices=STANCES); s.add_argument("comment", nargs="?", default="")
    s = a("discussion"); s.add_argument("discussion")
    s = a("discussions"); s.add_argument("--project")
    s = a("decide"); s.add_argument("discussion"); s.add_argument("decision"); s.add_argument("--proposal")
    s.add_argument("--no-consensus", action="store_true")

    s = a("doc", help="collaborative documents"); ds = s.add_subparsers(dest="doc_cmd", required=True)
    d = ds.add_parser("create"); d.add_argument("title"); d.add_argument("--kind", default="note", choices=DOC_KINDS)
    d.add_argument("--file"); d.add_argument("--content", default="")
    d = ds.add_parser("show"); d.add_argument("document"); d.add_argument("--revision", type=int)
    d = ds.add_parser("edit"); d.add_argument("document"); d.add_argument("--base-revision", type=int, required=True)
    d.add_argument("--file"); d.add_argument("--content"); d.add_argument("-m", "--message", default="")
    d = ds.add_parser("history"); d.add_argument("document")
    d = ds.add_parser("list"); d.add_argument("--kind")

    s = a("tasks"); s.add_argument("--status"); s.add_argument("--project")
    s = a("task"); ts = s.add_subparsers(dest="task_cmd", required=True)
    t = ts.add_parser("create"); t.add_argument("title"); t.add_argument("--description", default="")
    t.add_argument("--priority", type=int, default=0); t.add_argument("--claim"); t.add_argument("--assign"); t.add_argument("--category")
    t = ts.add_parser("accept"); t.add_argument("task")
    t = ts.add_parser("done"); t.add_argument("task"); t.add_argument("note", nargs="?", default="")

    s = a("memory"); ms = s.add_subparsers(dest="mem_cmd", required=True)
    m = ms.add_parser("show"); m.add_argument("--kind", choices=MEMORY_KINDS)
    m = ms.add_parser("search"); m.add_argument("query")
    m = ms.add_parser("add"); m.add_argument("kind", choices=MEMORY_KINDS); m.add_argument("title")
    m.add_argument("--file"); m.add_argument("--content"); m.add_argument("--source", default="")
    m = ms.add_parser("edit"); m.add_argument("memory"); m.add_argument("--base-revision", type=int, required=True)
    m.add_argument("--file"); m.add_argument("--content"); m.add_argument("--archive", action="store_true")
    a("context")

    s = a("profile"); s.add_argument("--provider"); s.add_argument("--model"); s.add_argument("--family")
    s.add_argument("--category"); s.add_argument("--reasoning"); s.add_argument("--capability", action="append")
    s = a("suggest", help="suggest agents for a task (hint only; you choose)")
    s.add_argument("--task", help="free-text task label, recorded nowhere"); s.add_argument("--prefer-category")
    s.add_argument("--capability", action="append"); s.add_argument("--reasoning")

    a("projects")
    s = a("status"); s.add_argument("--project")
    s = a("events"); s.add_argument("--after", type=int, default=0)

    return p


def run(a, c) -> tuple[str, object, int]:
    S = load_session
    cmd = a.cmd
    if cmd == "whoami":
        r = c.whoami(family=a.family, project=a.project or detect_project(), client_id=str(uuid.uuid4()))
        # .coord-session is shared by every session in this checkout: never
        # overwrite one that still belongs to a live session, or that session
        # would silently start acting as this one.
        old = None if os.environ.get("COORD_SESSION") else load_session()
        if old:
            try:
                c.context(session=old)
            except CoordError:
                old = None
        if old:
            r["warning"] = (f"{session_file()} belongs to another live session; left as is - "
                            f"prefix your commands with COORD_SESSION={r['session_id']}")
        elif not os.environ.get("COORD_SESSION"):
            try:
                session_file().write_text(r["session_id"] + "\n")
            except OSError:
                pass
        return cmd, r, 0
    if cmd == "heartbeat": return cmd, c.heartbeat(session=S(), status=a.status), 0
    if cmd == "end": return cmd, c.end(session=S()), 0
    if cmd == "presence": return cmd, c.presence(project=a.project, include_dead=a.all), 0
    if cmd == "post":
        return cmd, c.post(session=S(), body=a.body, kind=a.kind, to=a.to, claim=a.claim,
                           client_id=a.client_id or str(uuid.uuid4())), 0
    if cmd == "reply":
        return cmd, c.reply(session=S(), message=a.message, body=a.body, kind=a.kind,
                            client_id=a.client_id or str(uuid.uuid4())), 0
    if cmd == "inbox":
        return cmd, c.inbox(session=S(), after=a.after, to_me=a.to_me, sender=a.sender, kind=a.kind,
                            project=a.project, limit=None if a.all else a.limit, unresolved=a.unresolved), 0
    if cmd == "thread": return cmd, c.thread(message=a.message, session=S()), 0
    if cmd == "resolve": return cmd, c.resolve(session=S(), message=a.message, resolution=a.resolution), 0
    if cmd == "poll": return cmd, c.poll(session=S()), 0
    if cmd == "claim":
        return cmd, c.claim(session=S(), scope=rel(a.scope), tree=a.tree or None, note=a.note, ttl=a.ttl,
                            release_on_commit=a.release_on_commit, client_id=a.client_id or str(uuid.uuid4())), 0
    if cmd == "renew": return cmd, c.renew(session=S(), claim=a.claim, ttl=a.ttl), 0
    if cmd == "release": return cmd, c.release(session=S(), claim=a.claim, all=a.all), 0
    if cmd == "locks": return cmd, c.locks(project=a.project or detect_project(), all=a.all), 0
    if cmd == "fence-check": return cmd, c.fence_check(claim=a.claim, fence=a.fence), 0
    if cmd == "grant": return cmd, c.grant(session=S(), claim=a.claim, to=a.to, role=a.role), 0
    if cmd == "revoke": return cmd, c.revoke(session=S(), claim=a.claim, to=a.to, role=a.role), 0
    if cmd == "roles": return cmd, c.roles(claim=a.claim), 0
    if cmd == "ask":
        return cmd, c.ask(session=S(), claim=a.claim, to=a.to, body=a.body, role=a.role, kind=a.kind,
                          client_id=str(uuid.uuid4())), 0
    if cmd == "check":
        files = [rel(f) for f in a.files] or git("diff", "--cached", "--name-only").splitlines()
        r = c.check(session=S(), files=files)
        return cmd, r, 0 if r["ok"] else 1
    if cmd == "post-commit":
        sha = a.sha or git("rev-parse", "HEAD")
        files = git("diff-tree", "--no-commit-id", "--name-only", "-r", sha).splitlines()
        return cmd, c.post_commit(session=S(), sha=sha, files=files), 0
    if cmd == "install-hooks":
        hooks = repo_root() / ".git" / "hooks"
        exe = f'"{sys.executable}" "{Path(__file__).resolve()}"'
        (hooks / "pre-commit").write_text(f"#!/bin/sh\n[ -z \"$COORD_SESSION\" ] && [ ! -f .coord-session ] && exit 0\n{exe} check\n")
        (hooks / "post-commit").write_text(f"#!/bin/sh\n[ -z \"$COORD_SESSION\" ] && [ ! -f .coord-session ] && exit 0\n{exe} post-commit || true\n")
        for h in ("pre-commit", "post-commit"):
            (hooks / h).chmod(0o755)
        return cmd, {"installed": [str(hooks / "pre-commit"), str(hooks / "post-commit")]}, 0
    if cmd == "discuss": return cmd, c.discuss(session=S(), topic=a.topic, claim=a.claim, client_id=str(uuid.uuid4())), 0
    if cmd == "propose": return cmd, c.propose(session=S(), discussion=a.discussion, body=a.body, client_id=str(uuid.uuid4())), 0
    if cmd == "react": return cmd, c.react(session=S(), proposal=a.proposal, stance=a.stance, comment=a.comment), 0
    if cmd == "discussion": return cmd, c.discussion(discussion=a.discussion), 0
    if cmd == "discussions": return cmd, c.discussions(project=a.project or detect_project()), 0
    if cmd == "decide":
        return cmd, c.decide(session=S(), discussion=a.discussion, decision=a.decision, proposal=a.proposal,
                             consensus=not a.no_consensus), 0
    if cmd == "doc":
        dc = a.doc_cmd
        if dc == "create":
            return "doc", c.doc_create(session=S(), title=a.title, kind=a.kind,
                                       content=a.content if not a.file else read_content(a), client_id=str(uuid.uuid4())), 0
        if dc == "show": return "doc-show", c.doc_show(document=a.document, revision=a.revision), 0
        if dc == "edit":
            return "doc", c.doc_edit(session=S(), document=a.document, base_revision=a.base_revision,
                                     content=read_content(a), message=a.message, client_id=str(uuid.uuid4())), 0
        if dc == "history": return "doc", c.doc_history(document=a.document), 0
        if dc == "list": return "doc", c.docs(project=detect_project(), kind=a.kind), 0
    if cmd == "tasks": return cmd, c.tasks(project=a.project or detect_project(), status=a.status), 0
    if cmd == "task":
        tc = a.task_cmd
        if tc == "create":
            return "task", c.task_create(session=S(), title=a.title, description=a.description, priority=a.priority,
                                         claim=a.claim, assign=a.assign, category=a.category, client_id=str(uuid.uuid4())), 0
        if tc == "accept": return "task", c.task_accept(session=S(), task=a.task), 0
        if tc == "done": return "task", c.task_done(session=S(), task=a.task, note=a.note), 0
    if cmd == "memory":
        mc = a.mem_cmd
        if mc == "show": return "memory", c.memory(project=detect_project(), kind=a.kind), 0
        if mc == "search": return "memory", c.memory(project=detect_project(), query=a.query), 0
        if mc == "add":
            return "memory-add", c.memory_add(session=S(), kind=a.kind, title=a.title, content=read_content(a),
                                              source=a.source, client_id=str(uuid.uuid4())), 0
        if mc == "edit":
            return "memory-edit", c.memory_edit(session=S(), memory=a.memory, base_revision=a.base_revision,
                                                content=read_content(a), status="archived" if a.archive else None), 0
    if cmd == "context": return cmd, c.context(session=S()), 0
    if cmd == "profile":
        return cmd, c.profile_set(session=S(), provider=a.provider, model_id=a.model, model_family=a.family,
                                  category=a.category, reasoning_level=a.reasoning, capabilities=a.capability), 0
    if cmd == "suggest":
        return cmd, c.suggest(project=detect_project(), category=a.prefer_category, capability=a.capability,
                              reasoning_level=a.reasoning, exclude_session=S()), 0
    if cmd == "projects": return cmd, c.projects(), 0
    if cmd == "status": return cmd, c.status(project=a.project), 0
    if cmd == "events": return cmd, c.events(after=a.after, project=detect_project()), 0
    raise SystemExit(f"unhandled command {cmd}")


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    load_config()
    try:
        if a.cmd in ("login", "logout"):
            tp = token_source()
            if tp is None or isinstance(tp, str):
                raise CoordError("oidc_config", "set COORD_OIDC_ISSUER and COORD_OIDC_CLIENT_ID "
                                 "(and unset COORD_TOKEN) to use SSO login")
            cmd, r, code = a.cmd, tp.login() if a.cmd == "login" else tp.logout(), 0
        else:
            cmd, r, code = run(a, backend())
    except CoordError as e:
        if a.json:
            print(json.dumps({"ok": False, "error": e.code, "message": str(e), "data": e.data}))
        else:
            print(f"error ({e.code}): {e}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(r, indent=2))
    else:
        if a.cmd == "whoami":
            print(f"you are {r['name']} (gen {r['generation']}, project {r['project']})\n"
                  f"export COORD_SESSION={r['session_id']}")
        else:
            print(human(cmd, r))
        if isinstance(r, dict) and r.get("warning"):
            print(f"warning: {r['warning']}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
