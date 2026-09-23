"""v2 coordination tests: uv run test_coord.py (temp dirs only)."""

import json
import os
import multiprocessing as mp
import shutil
import ssl
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from coordination import pki, scopes
from coordination.client import RemoteCoord
from coordination.server import OIDCIntrospector, build_server
from coordination.service import SESSION_TTL, Coord, CoordError

TMP = Path(tempfile.mkdtemp(prefix="coordtest-"))
# Never let the developer's ~/.config/coord/env steer CLI subprocesses.
os.environ["COORD_CONFIG"] = str(TMP / "no-such-config")
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
    gl = "gitlab.dci.local/team/sub/repo"                          # self-hosted GitLab, subgroups
    for url in ("https://gitlab.dci.local/team/sub/repo.git", "git@gitlab.dci.local:team/sub/repo.git",
                "ssh://git@gitlab.dci.local:2222/team/sub/repo.git", "https://oauth2:tok@gitlab.dci.local:443/team/sub/repo"):
        assert scopes.canonical_project(url) == gl, (url, scopes.canonical_project(url))


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
        # the real CLI over mTLS (whoami used to pass a positional arg RemoteCoord can't send)
        import os
        import subprocess
        env = dict(os.environ, COORD_SERVER=url, COORD_CA=str(d / "ca.crt"), COORD_CERT=str(acrt),
                   COORD_KEY=str(akey), COORD_PROJECT="mtls-cli")
        env.pop("COORD_SESSION", None)
        p = subprocess.run([sys.executable, str(Path(__file__).parent / "coord.py"), "--json", "whoami", "cli"],
                           env=env, capture_output=True, text=True, cwd=TMP)
        assert p.returncode == 0 and json.loads(p.stdout)["name"].startswith("cli-"), p.stdout + p.stderr
        # one-step enroll (management CLI): identity bundle + default link; the client then
        # needs only COORD_IDENTITY
        cfg_home = TMP / "xdg"
        base = {k: v for k, v in os.environ.items() if not k.startswith("COORD_")}
        base.update(XDG_CONFIG_HOME=str(cfg_home), COORD_PROJECT="enroll", PYTHONPATH=str(Path(__file__).parent))

        def cli(*args, admin=False, **extra):
            cmd = [sys.executable, "-m", "coordination.pki", "--dir", str(d)] if admin else \
                [sys.executable, str(Path(__file__).parent / "coord.py"), "--json"]
            p = subprocess.run(cmd + list(args), env=dict(base, **extra), capture_output=True, text=True, cwd=TMP)
            assert p.returncode == 0, p.stdout + p.stderr
            return json.loads(p.stdout)

        r = cli("enroll", "agent-a", "--url", url, admin=True)
        assert r["cert_reused"] and r["default"]                      # first identity -> default
        if os.name != "nt":
            assert oct((cfg_home / "coord" / "agent-a" / "agent.key").stat().st_mode & 0o777) == "0o600"
        r = cli("enroll", "agent-b", "--url", url, admin=True)        # agent-b was revoked above
        assert not r["cert_reused"] and not r["default"]
        assert cli("whoami", "x", COORD_IDENTITY="agent-a")["name"].startswith("x-")
        assert cli("enroll", "agent-b", "--url", url, "--default", admin=True)["default"]
    finally:
        httpd.shutdown()


@check
def mtls_renewal_and_live_revocation():
    """The server asks management for a renewal once the cert is 15 days old and hands it to
    the client, which installs it; revocation applies to the next request, no restart."""
    if not shutil.which("openssl"):
        print("  (skipped: no openssl)")
        return
    import time
    d = pki.init(TMP / "pki-renew")
    pki.issue(d, "localhost", server=True)
    b = pki.enroll(d, "agent-r", "unused", out=TMP / "bundle-r")
    bundle = Path(b["bundle"])
    later = time.time() + 16 * 86400                                   # "16 days later"
    authority = pki.Authority(d, clock=lambda: later)
    httpd = build_server(fresh()[0], "127.0.0.1", 0, tls_cert=str(d / "localhost.crt"),
                         tls_key=str(d / "localhost.key"), client_ca=str(d / "ca.crt"), authority=authority)
    url = f"https://localhost:{_serve(httpd)}"
    old = (bundle / "agent.crt").read_text()
    (TMP / "old-r.crt").write_text(old)
    (TMP / "old-r-kept.crt").write_text(old)             # a stale copy nobody updates
    try:
        cl = RemoteCoord(url, ca=str(bundle / "ca.crt"), cert=str(bundle / "agent.crt"),
                         key=str(bundle / "agent.key"))
        s = cl.whoami(family="r")["session_id"]                        # renewal rides on this reply
        new = (bundle / "agent.crt").read_text()
        assert new != old, "client did not install the renewal"
        status = lambda: {c["serial"]: c["status"] for c in pki.listing(d) if c["cn"] == "agent-r"}
        assert list(status().values()) == ["valid", "valid"]
        again = RemoteCoord(url, ca=str(bundle / "ca.crt"), cert=str(TMP / "old-r.crt"),
                            key=str(bundle / "agent.key"))
        again.presence()                                   # new not used yet: old still works and
        assert (bundle / "agent.crt").read_text() == new   # gets the same renewal, not a 3rd
        assert len(status()) == 2
        cl.heartbeat(session=s, status="renewed")          # new cert in use -> old one retired
        assert list(status().values()) == ["revoked", "valid"]
        stale = RemoteCoord(url, ca=str(bundle / "ca.crt"), cert=str(TMP / "old-r-kept.crt"),
                            key=str(bundle / "agent.key"))
        raises("unauthenticated", stale.presence)          # the superseded cert no longer works
        pki.revoke(d, "agent-r")                                       # all of agent-r's certs
        raises("unauthenticated", cl.presence)                         # no restart needed
    finally:
        httpd.shutdown()


@check
def server_cert_auto_renewal_and_47_day_cap():
    """The server renews its own certificate through management and loads it live; no leaf
    certificate outlives MAX_DAYS - older, longer ones are renewed at the first check/use."""
    if not shutil.which("openssl"):
        print("  (skipped: no openssl)")
        return
    import socket
    import subprocess
    import time

    def lifetime_days(pem_or_path):
        src = ["-in", str(pem_or_path)] if isinstance(pem_or_path, Path) else []
        out = subprocess.run(["openssl", "x509", *src, "-noout", "-startdate", "-enddate"],
                             input=None if src else pem_or_path, capture_output=True, text=True, check=True).stdout
        nb, na = (pki._ts(line.partition("=")[2]) for line in out.strip().splitlines())
        return round((na - nb) / 86400)

    d = pki.init(TMP / "pki-srv")
    pki.issue(d, "localhost", server=True, days=365)                 # issued before the 47-day rule
    pki.issue(d, "old-client", days=365)
    b = Path(pki.enroll(d, "old-client", "unused", out=TMP / "bundle-old")["bundle"])
    clock = [time.time()]
    authority = pki.Authority(d, clock=lambda: clock[0])
    httpd = build_server(fresh()[0], "127.0.0.1", 0, tls_cert=str(d / "localhost.crt"),
                         tls_key=str(d / "localhost.key"), client_ca=str(d / "ca.crt"), authority=authority)
    port = _serve(httpd)
    ctx = ssl.create_default_context(cafile=str(b / "ca.crt"))
    ctx.load_cert_chain(str(b / "agent.crt"), str(b / "agent.key"))

    def served():   # the certificate a new handshake gets
        with ctx.wrap_socket(socket.create_connection(("127.0.0.1", port)), server_hostname="localhost") as t:
            return ssl.DER_cert_to_PEM_cert(t.getpeercert(binary_form=True))
    try:
        assert lifetime_days(served()) == 365
        assert httpd.refresh_server_cert()                              # 365 > 47: renewed at once
        assert served().strip() == (d / "localhost.crt").read_text().strip()   # live, no restart
        assert lifetime_days(d / "localhost.crt") == pki.SERVER_DAYS == 47
        assert not httpd.refresh_server_cert()                          # fresh: nothing to do
        cl = RemoteCoord(f"https://localhost:{port}", ca=str(b / "ca.crt"), cert=str(b / "agent.crt"),
                         key=str(b / "agent.key"))
        st = lambda cn: [c["status"] for c in pki.listing(d) if c["cn"] == cn]
        assert st("localhost") == ["revoked", "valid"]                # old server cert retired at swap
        cl.presence()                                                 # client 365 d -> 30 d on first use
        assert lifetime_days(b / "agent.crt") == pki.CLIENT_DAYS
        assert [x["cn"] for x in pki.tidy(d)] == ["old-client"]       # dry run: renewal not used yet
        assert st("old-client") == ["valid", "valid"]
        cl.presence()                                                 # used -> the 365 d one retired
        assert st("old-client") == ["revoked", "valid"] and pki.tidy(d) == []
        clock[0] += 16 * 86400
        assert httpd.refresh_server_cert()                              # 15-day rule for the server too
        cl.presence()                                                 # still trusted after the swap
    finally:
        httpd.shutdown()

@check
def old_ca_is_upgraded_for_strict_clients():
    """A CA made before keyUsage existed (refused by Python >= 3.13) is re-signed in place by
    `init`: same key/subject, old certs still verify, local bundles refreshed."""
    if not shutil.which("openssl"):
        print("  (skipped: no openssl)")
        return
    import subprocess
    d = TMP / "pki-old"
    d.mkdir()
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                    "-keyout", str(d / "ca.key"), "-out", str(d / "ca.crt"), "-days", "3650", "-subj", "/CN=coord-ca",
                    "-addext", "basicConstraints=critical,CA:TRUE"], check=True, capture_output=True)
    old_ca = (d / "ca.crt").read_text()
    text = lambda f: subprocess.run(["openssl", "x509", "-in", str(f), "-noout", "-text"], capture_output=True,
                                    text=True, check=True).stdout
    assert "Key Usage" not in text(d / "ca.crt")
    saved = os.environ.get("XDG_CONFIG_HOME")
    os.environ["XDG_CONFIG_HOME"] = str(TMP / "xdg-old")
    try:
        (TMP / "xdg-old" / "coord" / "someone").mkdir(parents=True)
        (TMP / "xdg-old" / "coord" / "someone" / "ca.crt").write_text(old_ca)     # a local bundle
        pki.init(d)                                                               # upgrades
        assert "Key Usage" in text(d / "ca.crt") and (d / "ca.crt.pre-keyusage").read_text() == old_ca
        assert pki._run("x509", "-in", str(d / "ca.crt"), "-noout", "-pubkey", text=True) == \
            pki._run("x509", "-in", str(d / "ca.crt.pre-keyusage"), "-noout", "-pubkey", text=True)
        assert (TMP / "xdg-old" / "coord" / "someone" / "ca.crt").read_text() == (d / "ca.crt").read_text()
        assert pki.upgrade_ca(d) is False                                          # idempotent
        leaf, _ = pki.issue(d, "srv", server=True)
        subprocess.run(["openssl", "verify", "-x509_strict", "-CAfile", str(d / "ca.crt"), str(leaf)],
                       check=True, capture_output=True)                           # strict chain OK
    finally:
        if saved is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = saved

class FakeKeycloak:
    """Just enough of a Keycloak realm: discovery, token (client credentials, refresh with
    rotation, device code), device authorization, introspection, revocation."""

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import urllib.parse as up
        self.active, self.refresh, self.requests, self.n = set(), set(), [], 0
        self.pending = {}
        kc = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                base = kc.realm
                self.reply(200, {"issuer": base, "token_endpoint": base + "/token",
                                 "device_authorization_endpoint": base + "/device",
                                 "introspection_endpoint": base + "/introspect"})

            def do_POST(self):
                f = dict(up.parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode()))
                path = self.path.rsplit("/", 1)[-1]
                if path == "introspect":
                    ok = f.get("token") in kc.active
                    return self.reply(200, {"active": ok, "sub": "agent", "roles": ["coord:*:contributor"]}
                                      if ok else {"active": False})
                if path == "device":
                    kc.pending["dc1"] = 1                      # one "authorization_pending" first
                    return self.reply(200, {"device_code": "dc1", "user_code": "WDJB-MJHT", "interval": 0,
                                            "expires_in": 60, "verification_uri": kc.realm + "/device-ui"})
                g = f.get("grant_type")
                kc.requests.append(g)
                if g == "client_credentials" and f.get("client_secret") != "s3cret":
                    return self.reply(401, {"error": "unauthorized_client", "error_description": "bad secret"})
                if g == "refresh_token":
                    if f.get("refresh_token") not in kc.refresh:
                        return self.reply(400, {"error": "invalid_grant", "error_description": "Session not active"})
                    kc.refresh.discard(f["refresh_token"])       # rotation: the old one is spent
                if g.endswith("device_code"):
                    if kc.pending.get(f.get("device_code"), 0) > 0:
                        kc.pending[f["device_code"]] -= 1
                        return self.reply(400, {"error": "authorization_pending"})
                kc.n += 1
                tok = {"access_token": f"at{kc.n}", "expires_in": 60, "token_type": "Bearer"}
                kc.active.add(tok["access_token"])
                if g != "client_credentials":
                    tok["refresh_token"] = f"rt{kc.n}"
                    kc.refresh.add(tok["refresh_token"])
                self.reply(200, tok)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.realm = f"http://127.0.0.1:{self.httpd.server_address[1]}/realms/corp"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def introspector(self):
        return OIDCIntrospector(self.realm + "/introspect", "coord", "srv", cache_seconds=0)


@check
def keycloak_tokens_refresh_themselves():
    from coordination.oidc_client import TokenProvider
    kc = FakeKeycloak()
    httpd = build_server(fresh()[0], "127.0.0.1", 0, oidc=kc.introspector())
    url = f"http://127.0.0.1:{_serve(httpd)}"
    clock = [time.time()]
    try:
        # service account (client credentials): no human, token fetched, cached, refetched
        tp = TokenProvider("agent-svc", issuer=kc.realm, client_secret="s3cret",
                           state_dir=TMP / "oidc-a", clock=lambda: clock[0])
        cl = RemoteCoord(url, token=tp)
        sid = cl.whoami(family="svc")["session_id"]
        cl.post(session=sid, body="hello from a service account")
        assert kc.requests == ["client_credentials"]
        if os.name != "nt":                                          # no POSIX modes on Windows
            assert oct(tp.state_file.stat().st_mode & 0o777) == "0o600"
        again = TokenProvider("agent-svc", issuer=kc.realm, client_secret="s3cret",
                              state_dir=TMP / "oidc-a", clock=lambda: clock[0])
        RemoteCoord(url, token=again).presence()                     # next process: cached token
        assert kc.requests == ["client_credentials"]
        clock[0] += 61                                               # the old bug: token expired
        cl.heartbeat(session=sid, status="still here")
        assert kc.requests == ["client_credentials"] * 2
        kc.active.clear()                                            # revoked server-side
        cl.heartbeat(session=sid, status="after revocation")         # 401 -> new token -> retry
        assert kc.requests == ["client_credentials"] * 3
        bad = TokenProvider("agent-svc", issuer=kc.realm, client_secret="nope", state_dir=TMP / "oidc-x")
        raises("unauthenticated", RemoteCoord(url, token=bad).presence)

        # a person (device login): `coord login` once, then refresh tokens do the rest
        tp2 = TokenProvider("agent-cli", issuer=kc.realm, state_dir=TMP / "oidc-b", clock=lambda: clock[0])
        err = raises("unauthenticated", RemoteCoord(url, token=tp2).presence)
        assert "coord login" in str(err)
        shown = []
        assert tp2.login(show=shown.append, sleep=lambda s: None)["logged_in"]
        assert "WDJB-MJHT" in shown[0]
        cl2 = RemoteCoord(url, token=tp2)
        sid2 = cl2.whoami(family="human")["session_id"]
        rt_before = json.loads(tp2.state_file.read_text())["refresh_token"]
        clock[0] += 61
        cl2.heartbeat(session=sid2, status="refreshed")               # refresh grant, rotated
        assert kc.requests[-1] == "refresh_token"
        assert json.loads(tp2.state_file.read_text())["refresh_token"] != rt_before
        kc.refresh.clear()                                           # SSO session ended
        clock[0] += 61
        err = raises("unauthenticated", cl2.presence)
        assert "coord login" in str(err)
        assert tp2.logout() == {"logged_out": False}                 # state already cleared

        # the real CLI, configured only through COORD_OIDC_* (e.g. in ~/.config/coord/env)
        import subprocess
        env = {k: v for k, v in os.environ.items() if not k.startswith("COORD_")}
        env.update(COORD_CONFIG=str(TMP / "no-such-config"), XDG_CONFIG_HOME=str(TMP / "xdg-oidc"),
                   COORD_SERVER=url, COORD_OIDC_ISSUER=kc.realm, COORD_OIDC_CLIENT_ID="agent-svc",
                   COORD_OIDC_CLIENT_SECRET="s3cret", COORD_PROJECT="kc")
        for args in (["whoami", "cli"], ["presence"]):
            p = subprocess.run([sys.executable, str(Path(__file__).parent / "coord.py"), "--json", *args],
                               env=env, capture_output=True, text=True, cwd=TMP)
            assert p.returncode == 0, p.stdout + p.stderr
    finally:
        httpd.shutdown()
        kc.httpd.shutdown()

_EXTERNAL_SCRIPT = r"""
import json, shutil, socket, ssl, sys, threading, time
from pathlib import Path
sys.modules["coordination.pki"] = None          # corporate mode: no local PKI code at all
from coordination.certsource import CommandSource, WatchSource
from coordination.client import RemoteCoord
from coordination.server import OIDCIntrospector, build_server
from coordination.service import Coord

T = Path(sys.argv[1])
class Resp:
    def __init__(self, b): self.b = b
    def read(self): return self.b
    def __enter__(self): return self
    def __exit__(self, *a): pass
keycloak = OIDCIntrospector("https://sso/introspect", "coord", "s", opener=lambda req, timeout: Resp(
    json.dumps({"active": True, "sub": "agent", "roles": ["coord:*:contributor"]}).encode()))
ca = str(T / "ca.crt")

def serve(source):
    live_c, live_k = T / "live.crt", T / "live.key"
    httpd = build_server(Coord(T / f"{source.name}.db"), "127.0.0.1", 0, tls_cert=str(live_c),
                         tls_key=str(live_k), oidc=keycloak, cert_source=source)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]

def served(port):
    ctx = ssl.create_default_context(cafile=ca)
    with ctx.wrap_socket(socket.create_connection(("127.0.0.1", port)), server_hostname="localhost") as t:
        return ssl.DER_cert_to_PEM_cert(t.getpeercert(binary_form=True)).strip()

pem = lambda n: (T / f"{n}.crt").read_text().strip()
def put(n, key=None):   # what cert-manager / certmonger / step would do to the files
    shutil.copy(T / f"{n}.crt", T / "live.crt"); shutil.copy(T / f"{key or n}.key", T / "live.key")

# watch (cert-manager, certmonger/AD CS): reload when an external renewer rewrites the files
put("srv1")
httpd, port = serve(WatchSource(T / "live.crt", T / "live.key"))
agent = RemoteCoord(f"https://localhost:{port}", ca=ca, token="sso-token")
sid = agent.whoami(family="agent")["session_id"]
assert served(port) == pem("srv1") and not httpd.refresh_server_cert()
put("srv2", key="srv1")                                   # half-rotated: mismatched pair
assert not httpd.refresh_server_cert() and served(port) == pem("srv1")   # never loaded
put("srv2")
assert httpd.refresh_server_cert() and served(port) == pem("srv2")
agent.post(session=sid, body="still served after rotation")
httpd.shutdown()

# command (step-ca `step ca renew --force {cert} {key}`, AD CS script): run when due
put("srv1")
clock = [time.time()]
renew = f"{sys.executable} {T / 'fake_step.py'} {{cert}} {{key}}"
src = CommandSource(T / "live.crt", T / "live.key", renew, clock=lambda: clock[0])
httpd, port = serve(src)
assert not httpd.refresh_server_cert() and not (T / "ran").exists()     # 1-day cert, not due
clock[0] += 17 * 3600                                                     # > 2/3 of 24 h
assert httpd.refresh_server_cert() and served(port) == pem("srv3")
bad = CommandSource(T / "live.crt", T / "live.key", f"{sys.executable} -c 'raise SystemExit(3)'",
                    clock=lambda: clock[0] + 10**6)
try:
    bad.refresh(); raise AssertionError("failed command not reported")
except RuntimeError as e:
    assert "exited 3" in str(e)
RemoteCoord(f"https://localhost:{port}", ca=ca, token="sso-token").presence()
httpd.shutdown()
print("ok")
"""


@check
def corporate_oidc_with_external_cert_sources():
    """Keycloak agents + server cert renewed by cert-manager-style files or a step-ca-style
    command, with the local PKI code unimportable."""
    if not shutil.which("openssl"):
        print("  (skipped: no openssl)")
        return
    import subprocess
    t = TMP / "corp"
    d = pki.init(t / "fake-corp-ca")                     # stands in for the corporate CA
    for n in ("srv1", "srv2", "srv3"):
        pki.issue(d, n, server=True, days=1)             # short-lived like step-ca's 24 h default
        for ext in ("crt", "key"):
            shutil.copy(d / f"{n}.{ext}", t / f"{n}.{ext}")
    shutil.copy(d / "ca.crt", t / "ca.crt")
    (t / "fake_step.py").write_text(
        "import shutil, sys\nfrom pathlib import Path\nT = Path(__file__).parent\n"
        "shutil.copy(T / 'srv3.crt', sys.argv[1]); shutil.copy(T / 'srv3.key', sys.argv[2])\n"
        "(T / 'ran').touch()\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("COORD_")}
    env.update(COORD_CONFIG=str(TMP / "no-such-config"), PYTHONPATH=str(Path(__file__).parent))
    p = subprocess.run([sys.executable, "-c", _EXTERNAL_SCRIPT, str(t)], env=env,
                       capture_output=True, text=True, cwd=TMP)
    assert p.returncode == 0 and p.stdout.strip().endswith("ok"), p.stdout + p.stderr

_NO_PKI_SCRIPT = r"""
import json, sys, threading
sys.modules["coordination.pki"] = None          # any `import coordination.pki` now fails
import coord
from coordination.client import RemoteCoord
from coordination.server import OIDCIntrospector, build_server
from coordination.service import Coord

# local mode: SQLite, no server, and not even the network client
sys.modules.pop("coordination.client")
assert coord.main(["whoami", "local"]) == 0
assert "coordination.client" not in sys.modules and "coordination.oidc_client" not in sys.modules

class Resp:
    def __init__(self, b): self.b = b
    def read(self): return self.b
    def __enter__(self): return self
    def __exit__(self, *a): pass

idp = lambda req, timeout: Resp(json.dumps({"active": True, "sub": "u", "roles": ["coord:*:contributor"]}).encode())
for oidc in (None, OIDCIntrospector("http://idp", "c", "s", opener=idp)):   # plain HTTP, then Keycloak
    httpd = build_server(Coord(sys.argv[1] + ("-oidc" if oidc else "")), "127.0.0.1", 0, oidc=oidc)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    c = RemoteCoord(f"http://127.0.0.1:{httpd.server_address[1]}", token="t" if oidc else None)
    sid = c.whoami(family="x")["session_id"]
    c.post(session=sid, body="no pki here")
    httpd.shutdown()
print("ok")
"""


@check
def local_and_oidc_modes_never_load_pki():
    import subprocess
    env = {k: v for k, v in os.environ.items() if not k.startswith("COORD_")}
    env.update(COORD_CONFIG=str(TMP / "no-such-config"), COORD_DB=str(TMP / "nopki-local.db"),
               PYTHONPATH=str(Path(__file__).parent))
    p = subprocess.run([sys.executable, "-c", _NO_PKI_SCRIPT, str(TMP / "nopki.db")], env=env,
                       capture_output=True, text=True, cwd=TMP)
    assert p.returncode == 0 and p.stdout.strip().endswith("ok"), p.stdout + p.stderr

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


@check
def config_file():
    import coord
    cfg = TMP / "cfgdir" / "env"
    cfg.parent.mkdir()
    cfg.write_text("# comment\nexport COORD_SERVER=https://localhost:1338\nCOORD_CA=pki/ca.crt\n"
                   "COORD_KEY='~/k.key'\nCOORD_PROJECT=from-file\nOTHER=x\n")
    saved = dict(os.environ)
    try:
        os.environ.update(COORD_CONFIG=str(cfg), COORD_PROJECT="from-env")
        for k in ("COORD_SERVER", "COORD_CA", "COORD_KEY", "OTHER"):
            os.environ.pop(k, None)
        assert coord.load_config() == cfg.resolve()                 # Windows: 8.3 temp names
        assert os.environ["COORD_SERVER"] == "https://localhost:1338"
        assert os.environ["COORD_CA"] == str((cfg.parent / "pki/ca.crt").resolve())
        assert os.environ["COORD_KEY"] == str(Path.home() / "k.key")
        assert os.environ["COORD_PROJECT"] == "from-env"      # environment wins
        assert "OTHER" not in os.environ                      # only COORD_* keys
    finally:
        os.environ.clear()
        os.environ.update(saved)


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
