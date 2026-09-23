"""v2 coordination tests: uv run test_coord.py (temp dirs only)."""

import json
import multiprocessing as mp
import shutil
import ssl
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

from coordination import pki, scopes
from coordination.http import OIDCIntrospector, RemoteCoord, build_server
from coordination.service import SESSION_TTL, Coord, CoordError

TMP = Path(tempfile.mkdtemp(prefix="coordtest-"))
_n = 0


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def fresh(**kw):
    global _n
    _n += 1
    clock = Clock()
    return Coord(TMP / f"t{_n}.db", clock=clock, **kw), clock


def raises(code, fn, *a, **k):
    try:
        fn(*a, **k)
    except CoordError as e:
        assert e.code == code, f"expected {code}, got {e.code}: {e}"
        return e
    raise AssertionError(f"expected CoordError {code}")


CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


# --- paths ---------------------------------------------------------------
@check
def paths():
    assert scopes.normalize("src\\auth\\x.py", False) == ("src/auth/x.py", False)
    assert scopes.normalize("./src//auth/", False) == ("src/auth", True)
    assert scopes.normalize("SRC/A.py", True) == ("src/a.py", False)
    for bad in ("../x", "src/../../x", "/etc/passwd", "C:\\x"):
        try:
            scopes.normalize(bad, False)
            raise AssertionError(bad)
        except scopes.ScopeError:
            pass
    assert scopes.canonical_project("git@github.com:Org/Repo.git") == "github.com/org/repo"
    assert scopes.canonical_project("https://user@github.com/org/repo") == "github.com/org/repo"


@check
def scope_conflicts():
    c, _ = fresh(case_insensitive=False)
    a = c.whoami("a")["session_id"]; b = c.whoami("b")["session_id"]
    c.claim(a, "src/auth/")
    raises("conflict", c.claim, b, "src/auth/login.py")          # child of tree
    raises("conflict", c.claim, b, "src/")                       # parent of tree
    c.claim(b, "src/billing/")                                   # sibling ok
    c.claim(b, "src/authx.py")                                   # prefix-but-not-child ok
    raises("already_held", c.claim, a, "src/auth/")


@check
def windows_case():
    c, _ = fresh(case_insensitive=True)
    a = c.whoami("a")["session_id"]; b = c.whoami("b")["session_id"]
    c.claim(a, "Src\\Main.PY")
    raises("conflict", c.claim, b, "src/main.py")


# --- sessions ------------------------------------------------------------
@check
def recycled_names():
    c, clock = fresh()
    old = c.whoami("claude")
    cl = c.claim(old["session_id"], "a.py")
    clock.t += SESSION_TTL + 1
    new = c.whoami("claude")
    assert new["name"] == old["name"] and new["generation"] == old["generation"] + 1
    raises("not_owner", c.release, new["session_id"], cl["claim"])        # new can't touch old claim
    raises("dead_session", c.post, old["session_id"], "zombie")          # dead can't mutate
    raises("dead_session", c.release, old["session_id"], cl["claim"])
    assert c.locks() == []                                              # dead owner's claims lapse
    assert c.locks(all=True)[0]["released"]


@check
def old_session_cannot_release_new_claim():
    c, clock = fresh()
    s1 = c.whoami("x")["session_id"]
    c.end(s1)
    s2 = c.whoami("x")["session_id"]
    cl = c.claim(s2, "f.py")
    raises("dead_session", c.release, s1, cl["claim"])


# --- claims --------------------------------------------------------------
@check
def fence_and_renew():
    c, clock = fresh()
    a = c.whoami("a")["session_id"]; b = c.whoami("b")["session_id"]
    c1 = c.claim(a, "f.py", ttl=100)
    assert c.fence_check(c1["claim"], c1["fence"])["ok"]
    c.renew(a, c1["claim"], ttl=100)
    assert c.fence_check(c1["claim"], c1["fence"])["ok"]   # renewal keeps the fence
    clock.t += 101
    c.heartbeat(a); c.heartbeat(b)
    raises("stale_fence", c.fence_check, c1["claim"], c1["fence"])
    raises("expired", c.renew, a, c1["claim"])
    c2 = c.claim(b, "f.py")
    assert c2["fence"] > c1["fence"]
    raises("not_owner", c.release, a, c2["claim"])
    c.claim(b, "g.py"); c.claim(b, "h/", tree=True)
    assert len(c.release(b, all=True)["released"]) == 3
    assert c.locks() == []


@check
def ask_keeps_claim_and_advisor_cannot_write():
    c, _ = fresh()
    a = c.whoami("claude")["session_id"]; b = c.whoami("codex")
    cl = c.claim(a, "src/parser/", tree=True)
    r = c.ask(a, cl["claim"], b["name"], "Second opinion on the deadlock?")
    assert r["kept"] and c.locks()[0]["owner"] == "claude-01"
    assert {"session": "codex-01", "role": "advisor"} in c.roles(cl["claim"])
    assert c.inbox(b["session_id"], to_me=True)[0]["claim"] == cl["claim"]
    assert not c.check(b["session_id"], ["src/parser/x.py"])["ok"]       # advisor: no write
    raises("not_owner", c.release, b["session_id"], cl["claim"])
    raises("conflict", c.claim, b["session_id"], "src/parser/x.py")
    c.grant(a, cl["claim"], b["name"], "delegate")                       # explicit subdomain
    c.claim(b["session_id"], "src/parser/lexer/", tree=True)
    assert c.check(b["session_id"], ["src/parser/x.py"])["ok"]


@check
def git_check_and_post_commit():
    c, _ = fresh()
    a = c.whoami("a")["session_id"]; b = c.whoami("b")["session_id"]
    c.claim(a, "x.py", release_on_commit=True); c.claim(a, "y.py")
    r = c.check(b, ["x.py", "z.py"])
    assert not r["ok"] and r["conflicts"][0]["file"] == "x.py"
    r = c.post_commit(a, "abc123", ["x.py", "y.py"])
    assert len(r["released"]) == 1
    assert [cl["scope"] for cl in c.locks()] == ["y.py"]
    assert any(e["kind"] == "commit.created" and e["id"] == "abc123" for e in c.events())


def _race(args):
    path, name = args
    c = Coord(path)
    s = c.whoami(name)["session_id"]
    try:
        c.claim(s, "hot/file.py")
        return 1
    except CoordError:
        return 0


def _post_many(args):
    path, name = args
    c = Coord(path)
    s = c.whoami(name)["session_id"]
    return [c.post(s, f"{name} {i}", client_id=f"{name}-{i}")["id"] for i in range(20)]


@check
def multiprocess():
    path = TMP / "mp.db"
    Coord(path)
    ctx = mp.get_context("spawn")
    with ctx.Pool(8) as pool:
        wins = pool.map(_race, [(path, f"p{i}") for i in range(8)])
        ids = pool.map(_post_many, [(path, f"w{i}") for i in range(4)])
    assert sum(wins) == 1, wins
    flat = sorted(i for l in ids for i in l)
    assert flat == list(range(flat[0], flat[0] + 80)), "ids must be dense and unique"
    names = {s["name"] for s in Coord(path).presence()}
    assert len(names) == 12


# --- messages ------------------------------------------------------------
@check
def messages():
    c, _ = fresh()
    a = c.whoami("a")["session_id"]; b = c.whoami("b")["session_id"]; x = c.whoami("x")["session_id"]
    m1 = c.post(a, "hello", client_id="k1")
    assert c.post(a, "hello", client_id="k1")["replayed"] and len(c.inbox(a)) == 1   # idempotent
    raises("idempotency_conflict", c.resolve, a, m1["id"], client_id="k1")
    long = c.post(a, "x" * 500)
    assert "warning" in long
    raises("too_long", c.post, a, "x" * 10001)
    dm = c.post(a, "private?", to="b-01", kind="question")
    assert dm["id"] not in [m["id"] for m in c.inbox(x)]                 # not visible to third party
    r = c.reply(b, dm["id"], "yes")
    assert c.inbox(a, to_me=True)[-1]["id"] == r["id"]
    assert [m["id"] for m in c.thread(r["id"], session=a)] == [dm["id"], r["id"]]
    c.resolve(b, m1["id"], "done")
    assert c.inbox(a)[0]["resolved_by"] == "b-01"                          # actor recorded
    assert c.inbox(a, sender="a-01", limit=1)[0]["id"] == dm["id"]
    raises("bad_kind", c.post, a, "x", kind="shout")
    # the sender cannot be spoofed: it comes from the session, not the body
    assert c.inbox(a, after=r["id"] - 1)[0]["from"] == "b-01"


@check
def poll_cursor():
    c, _ = fresh()
    a = c.whoami("a")["session_id"]; b = c.whoami("b")["session_id"]
    for i in range(5):
        c.post(b, f"m{i}")
    assert len(c.poll(a)["messages"]) == 5
    assert c.poll(a)["messages"] == []
    c.post(b, "later")
    assert [m["body"] for m in c.poll(a)["messages"]] == ["later"]


@check
def projects_isolated():
    c, _ = fresh()
    a = c.whoami("a", project="github.com/o/one")["session_id"]
    b = c.whoami("b", project="github.com/o/two")["session_id"]
    c.claim(a, "src/")
    c.claim(b, "src/")                                                  # different project: ok
    c.post(a, "one only")
    assert c.inbox(b) == []
    assert {p["project"] for p in c.projects()} == {"github.com/o/one", "github.com/o/two"}
    assert c.status("github.com/o/one")["active_claims"] == 1


# --- consensus & documents ---------------------------------------------
@check
def consensus():
    c, _ = fresh()
    a = c.whoami("a")["session_id"]; b = c.whoami("b")["session_id"]
    d = c.discuss(a, "Best fix for race?")
    p1 = c.propose(b, d["discussion"], "Use BEGIN IMMEDIATE")
    p2 = c.propose(a, d["discussion"], "Add a mutex")
    c.react(a, p1["proposal"], "support"); c.react(b, p1["proposal"], "support")
    c.react(b, p2["proposal"], "object", "doesn't cover multi-process")
    c.react(b, p2["proposal"], "need-more-info")                         # upsert
    show = c.discussion(d["discussion"])
    assert show["proposals"][0]["tally"]["support"] == 2
    assert show["proposals"][1]["tally"] == {"support": 0, "object": 0, "abstain": 0, "need-more-info": 1}
    raises("forbidden", c.decide, b, d["discussion"], "x")
    r = c.decide(a, d["discussion"], "Use BEGIN IMMEDIATE", proposal=p1["proposal"])
    show = c.discussion(d["discussion"])
    assert show["status"] == "decided" and show["consensus"] and show["decided_by"] == "a-01"
    assert show["decision_document"] == r["document"]
    assert "BEGIN IMMEDIATE" in c.doc_show(r["document"])["content"]
    assert [m["kind"] for m in c.thread(show["thread"])] == ["question", "proposal", "proposal", "decision"]
    raises("closed", c.react, a, p1["proposal"], "object")


@check
def documents():
    c, _ = fresh()
    a = c.whoami("a")["session_id"]; b = c.whoami("b")["session_id"]
    d = c.doc_create(a, "Parser deadlock", kind="diagnosis", content="v1")
    c.doc_edit(a, d["document"], 1, "v2 by a")
    e = raises("revision_conflict", c.doc_edit, b, d["document"], 1, "v2 by b")
    assert e.data["current_revision"] == 2 and e.data["current_content"] == "v2 by a"
    c.doc_edit(b, d["document"], 2, "v3 merged", message="merge")
    h = c.doc_history(d["document"])
    assert [x["author"] for x in h] == ["a-01", "a-01", "b-01"] and h[-1]["message"] == "merge"
    assert c.doc_show(d["document"], revision=1)["content"] == "v1"
    raises("bad_kind", c.doc_create, a, "x", kind="poem")


# --- tasks, memory, routing, context -----------------------------------
@check
def tasks_memory_routing():
    c, _ = fresh()
    a = c.whoami("claude")["session_id"]; b = c.whoami("codex")["session_id"]
    t = c.task_create(a, "Fix parser", priority=2, category="debugging")
    c.task_accept(b, t["task"])
    raises("taken", c.task_accept, a, t["task"])
    raises("forbidden", c.task_done, a, t["task"])
    c.task_done(b, t["task"], "fixed @abc")
    assert c.tasks(status="done")[0]["assigned"] == "codex-01"
    m = c.memory_add(a, "overview", "Project", "Local-first coordination server", source="README")
    c.memory_add(a, "pitfall", "Transactions", "Always BEGIN IMMEDIATE for claims")
    assert [x["title"] for x in c.memory(query="transactions immediate")] == ["Transactions"]
    c.memory_edit(b, m["memory"], 1, "Local-first coordination server v2")
    raises("revision_conflict", c.memory_edit, a, m["memory"], 1, "stale")
    c.profile_set(b, provider="openai", model_id="gpt", category="reasoning", capabilities=["debugging"])
    c.profile_set(a, provider="anthropic", category="fast")
    s = c.suggest(category="reasoning", capability=["debugging"], exclude_session=a)
    assert s[0]["name"] == "codex-01" and s[0]["score"] == 3
    c.claim(a, "src/")
    ctx = c.context(a)
    assert ctx["overview"][0]["content"].endswith("v2") and ctx["my_claims"][0]["scope"] == "src/"
    assert ctx["memory"][0]["title"] == "Transactions"


# --- transport -----------------------------------------------------------
def _serve(httpd):
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd.server_address[1]


@check
def http_loopback():
    c, _ = fresh()
    httpd = build_server(c, "127.0.0.1", 0)
    port = _serve(httpd)
    try:
        r = RemoteCoord(f"http://127.0.0.1:{port}")
        s = r.whoami(family="remote")["session_id"]
        cl = r.claim(session=s, scope="a.py")
        assert r.locks()[0]["claim"] == cl["claim"]
        try:
            r.claim(session=s, scope="../x")
            raise AssertionError
        except CoordError as e:
            assert e.code == "bad_scope"
    finally:
        httpd.shutdown()
    try:
        build_server(c, "0.0.0.0", 0)
        raise AssertionError("non-loopback without TLS must refuse")
    except SystemExit:
        pass


@check
def oidc_roles():
    tokens = {"alice": {"active": True, "sub": "alice", "realm_access": {"roles": ["coord:p1:contributor"]}},
              "bob": {"active": True, "sub": "bob", "roles": ["coord:*:viewer"]},
              "dead": {"active": False}}

    class Resp:
        def __init__(self, b): self.b = b
        def read(self): return self.b
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def opener(req, timeout):
        tok = dict(x.split("=") for x in req.data.decode().split("&"))["token"]
        return Resp(json.dumps(tokens.get(tok, {"active": False})).encode())

    c, _ = fresh()
    httpd = build_server(c, "127.0.0.1", 0, oidc=OIDCIntrospector("http://idp", "c", "s", opener=opener))
    port = _serve(httpd)
    url = f"http://127.0.0.1:{port}"
    try:
        alice = RemoteCoord(url, token="alice")
        s = alice.whoami(family="alice", project="p1")["session_id"]
        alice.post(session=s, body="hi")
        bob = RemoteCoord(url, token="bob")
        assert bob.inbox(project="p1")[0]["body"] == "hi"            # viewer reads
        for who, code in ((bob, "forbidden"), (RemoteCoord(url, token="dead"), "unauthenticated")):
            try:
                who.post(session=s, body="nope")
                raise AssertionError
            except CoordError as e:
                assert e.code == code, e.code
        try:
            alice.whoami(family="x", project="p2")                       # no role on p2
            raise AssertionError
        except CoordError as e:
            assert e.code == "forbidden"
    finally:
        httpd.shutdown()


@check
def mtls_with_crl():
    if not shutil.which("openssl"):
        print("  (skipped: no openssl)")
        return
    d = pki.init(TMP / "pki")
    scrt, skey = pki.issue(d, "localhost", server=True)
    acrt, akey = pki.issue(d, "agent-a")
    bcrt, bkey = pki.issue(d, "agent-b")
    crl = pki.revoke(d, "agent-b")
    c, _ = fresh()
    httpd = build_server(c, "127.0.0.1", 0, tls_cert=str(scrt), tls_key=str(skey),
                         client_ca=str(d / "ca.crt"), crl=str(crl))
    port = _serve(httpd)
    url = f"https://localhost:{port}"
    try:
        a = RemoteCoord(url, ca=str(d / "ca.crt"), cert=str(acrt), key=str(akey))
        s = a.whoami(family="a")["session_id"]
        a.heartbeat(session=s, status="ok")
        b = RemoteCoord(url, ca=str(d / "ca.crt"), cert=str(bcrt), key=str(bkey))
        try:
            b.inbox()
            raise AssertionError("revoked cert accepted")
        except (ssl.SSLError, OSError, urllib.request.URLError):
            pass
        try:
            RemoteCoord(url, ca=str(d / "ca.crt")).inbox()
            raise AssertionError("no client cert accepted")
        except (ssl.SSLError, OSError, urllib.request.URLError):
            pass
        # session bound to principal: another valid cert cannot drive it
        e_crt, e_key = pki.issue(d, "agent-e")
        e = RemoteCoord(url, ca=str(d / "ca.crt"), cert=str(e_crt), key=str(e_key))
        try:
            e.post(session=s, body="hijack")
            raise AssertionError
        except CoordError as err:
            assert err.code == "forbidden"
    finally:
        httpd.shutdown()


@check
def cli_roundtrip():
    import os
    import subprocess
    env = dict(os.environ, COORD_DB=str(TMP / "cli.db"), COORD_PROJECT="cli-proj")
    env.pop("COORD_SERVER", None)
    here = Path(__file__).parent

    def run(*args, session=None, ok=True):
        e = dict(env, **({"COORD_SESSION": session} if session else {}))
        p = subprocess.run([sys.executable, str(here / "coord.py"), "--json", *args], env=e,
                           capture_output=True, text=True, cwd=TMP)
        if ok:
            assert p.returncode == 0, p.stdout + p.stderr
        return json.loads(p.stdout) if p.stdout.strip() else None

    s = run("whoami", "cli")["session_id"]
    c = run("claim", "src/", session=s)
    assert c["scope_type"] == "tree"
    run("post", "--kind", "info", "starting", session=s)
    assert run("inbox", session=s)[0]["body"] == "starting"
    d = run("doc", "create", "--kind", "plan", "--content", "step 1", "Plan", session=s)
    run("doc", "edit", d["document"], "--base-revision", "1", "--content", "step 2", session=s)
    assert run("doc", "show", d["document"])["revision"] == 2
    bad = run("doc", "edit", d["document"], "--base-revision", "1", "--content", "x", session=s, ok=False)
    assert bad["error"] == "revision_conflict"
    assert run("release", "--all", session=s)["released"] == [c["claim"]]
    assert run("context", session=s)["me"]["project"] == "cli-proj"

    # A second whoami in the same checkout must not hijack a live session's file.
    (TMP / ".coord-session").unlink(missing_ok=True)
    first = run("whoami", "cli")["session_id"]
    assert (TMP / ".coord-session").read_text().strip() == first
    second = run("whoami", "cli")
    assert "warning" in second and (TMP / ".coord-session").read_text().strip() == first
    run("end", session=first)
    third = run("whoami", "cli")["session_id"]
    assert (TMP / ".coord-session").read_text().strip() == third


def main():
    failed = 0
    for fn in CHECKS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {e!r}")
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} coord checks passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
