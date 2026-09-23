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
import urllib.error
import urllib.request
from pathlib import Path

from coordination import pki, scopes
from coordination.client import RemoteCoord
from coordination.server import OIDCIntrospector, build_server
from coordination.service import SESSION_TTL, Coord, CoordError
from coordination.sslbin import OpensslMissing, openssl


def have_openssl() -> bool:
    try:
        return bool(openssl())
    except OpensslMissing:
        return False

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
    assert {"session": "codex-01", "role": "advisor", "status": "granted"} in c.roles(cl["claim"])   # advice: immediate
    assert c.inbox(b["session_id"], to_me=True)[0]["claim"] == cl["claim"]
    assert not c.check(b["session_id"], ["src/parser/x.py"])["ok"]       # advisor: no write
    raises("not_owner", c.release, b["session_id"], cl["claim"])
    raises("conflict", c.claim, b["session_id"], "src/parser/x.py")
    assert c.grant(a, cl["claim"], b["name"], "delegate")["status"] == "offered"   # duties: needs consent
    raises("conflict", c.claim, b["session_id"], "src/parser/lexer/", tree=True)  # not in effect yet
    c.role_accept(b["session_id"], cl["claim"], "delegate")
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
    assert show["status"] == "decided" and show["consensus"] and show["decided_by"] == "a-01"   # a, b support
    assert show["decision_document"] == r["document"]
    assert "BEGIN IMMEDIATE" in c.doc_show(r["document"])["content"]
    assert [m["kind"] for m in c.thread(show["thread"])] == ["question", "proposal", "proposal", "decision"]
    raises("closed", c.react, a, p1["proposal"], "object")


@check
def consensus_is_computed_not_declared():
    """Point 1: `decide` records what the stances say, never what the decider claims."""
    c, _ = fresh()
    a, b, x = (c.whoami(n)["session_id"] for n in ("a", "b", "x"))
    d = c.discuss(a, "open question")["discussion"]                       # open discussion
    p = c.propose(a, d, "my idea")["proposal"]
    err = raises("no_consensus", c.decide, a, d, "go", proposal=p)       # nobody else took a stance
    assert "quorum not reached" in err.data["why"][0]                     # 0.2.1 recorded "consensus: yes"
    raises("reason_required", c.decide, a, d, "go", proposal=p, consensus=False)
    r = c.decide(a, d, "go", proposal=p, consensus=False, reason="nobody else around")
    assert r["consensus"] is False and c.discussion(d)["consensus"] is False
    d = c.discuss(a, "second")["discussion"]
    p = c.propose(a, d, "idea")["proposal"]
    c.react(b, p, "object", "breaks multi-process")
    raises("no_consensus", c.decide, a, d, "go anyway", proposal=p)     # objections block a silent decision
    r = c.decide(a, d, "go anyway", proposal=p, consensus=False, reason="deadline")
    assert r["consensus"] is False and any("b-01" in w for w in r["why"])
    detail = c.discussion(d)["consensus_detail"]
    assert {q["name"]: q["stance"] for q in detail["participants"]} == {"a-01": "support", "b-01": "object"}
    doc = c.doc_show(c.discussion(d)["decision_document"])["content"]
    assert "consensus: no" in doc and "b-01: object" in doc and "reason: deadline" in doc
    d = c.discuss(a, "third")["discussion"]                             # --no-consensus only lowers
    p = c.propose(a, d, "idea")["proposal"]
    c.react(b, p, "support")
    assert c.decide(a, d, "go", proposal=p, consensus=False, reason="not convinced")["consensus"] is False
    d = c.discuss(a, "fourth")["discussion"]
    raises("no_consensus", c.decide, a, d, "no proposal")               # nothing to agree on
    assert c.decide(a, d, "no proposal", consensus=False, reason="closing")["consensus"] is False


@check
def consensus_participants_rules_and_quorum():
    """Points 2 and 3: named participants, unanimous / majority / no-objection, quorum."""
    c, _ = fresh()
    a, b, x, y = (c.whoami(n)["session_id"] for n in ("a", "b", "x", "y"))

    def run(rule, stances, quorum=None, with_=("b-01", "x-01", "y-01")):
        d = c.discuss(a, f"{rule} {stances}", participants=list(with_), rule=rule, quorum=quorum)["discussion"]
        p = c.propose(a, d, "idea")["proposal"]
        for sid, st in zip((b, x, y), stances):
            if st:
                c.react(sid, p, st)
        ev = c.discussion(d)["proposals"][0]["consensus"]
        r = c.decide(a, d, "decided", proposal=p) if ev["met"] else \
            c.decide(a, d, "decided", proposal=p, consensus=False, reason="override")
        return d, dict(r, why=ev["why"])

    assert run("unanimous", ("support", "support", "support"))[1]["consensus"]
    assert run("unanimous", ("support", "abstain", "support"))[1]["consensus"]           # abstain is fine
    r = run("unanimous", ("support", "support", None))[1]
    assert not r["consensus"] and any("no stance from y-01" in w for w in r["why"])     # silence blocks
    assert not run("unanimous", ("support", "need-more-info", "support"))[1]["consensus"]
    assert run("majority", ("support", "support", "object"))[1]["consensus"]            # a, b, x: 3 of 4
    r = run("majority", ("support", "object", None))[1]                                 # a, b: 2 of 4 - a tie
    assert not r["consensus"] and "2 of 4" in r["why"][0]
    assert run("no-objection", (None, None, None))[1]["consensus"] is False               # quorum 2: only a
    assert run("no-objection", ("abstain", None, None))[1]["consensus"]                  # silence = consent
    assert not run("no-objection", (None, "object", None))[1]["consensus"]
    r = run("no-objection", ("support", None, None), quorum=3)[1]
    assert not r["consensus"] and "quorum not reached" in r["why"][0]
    # only participants count: an outsider's objection is shown, not counted
    d = c.discuss(a, "pair", participants=["b-01"])["discussion"]
    p = c.propose(b, d, "b's idea")["proposal"]                          # author b: implied support
    c.react(x, p, "object")
    show = c.discussion(d)
    assert show["participants"] == ["a-01", "b-01"] and show["rule"] == "unanimous" and show["quorum"] == 2
    ev = show["proposals"][0]["consensus"]
    assert ev["met"] and [q["implied"] for q in ev["participants"]] == [True, True]
    assert show["proposals"][0]["tally"]["object"] == 1                   # visible in the tally
    assert c.decide(a, d, "adopt b's idea", proposal=p)["consensus"]
    raises("bad_rule", c.discuss, a, "t", rule="dictator")
    raises("bad_quorum", c.discuss, a, "t", participants=["b-01"], quorum=3)
    raises("unknown_recipient", c.discuss, a, "t", participants=["ghost-01"])


def _dms(c, session):
    return [m["body"] for m in c.inbox(session, to_me=True, limit=None)]


@check
def joint_decisions_deadlines_and_notifications():
    """4-5: objections block a silent decision; any participant may decide once consensus is
    reached; after the deadline silence counts as agreement; invitations and outcomes are DMs."""
    c, clock = fresh()
    a, b, x, out = (c.whoami(n)["session_id"] for n in ("a", "b", "x", "out"))
    d = c.discuss(a, "Lock strategy?", participants=["b-01", "x-01"], deadline="48h")
    assert d["deadline"] and any("[" + d["discussion"] + "] a-01 asks for your agreement" in m for m in _dms(c, b))
    p = c.propose(a, d["discussion"], "BEGIN IMMEDIATE")["proposal"]
    c.react(b, p, "support")
    err = raises("no_consensus", c.decide, a, d["discussion"], "go", proposal=p)
    assert any("no stance from x-01" in w for w in err.data["why"])       # silence, before the deadline
    raises("forbidden", c.decide, out, d["discussion"], "go", proposal=p)  # not a participant
    raises("forbidden", c.decide, b, d["discussion"], "go", proposal=p,     # participants can't override
           consensus=False, reason="impatient")
    for _ in range(49 * 3):                                               # 49 h pass; agents stay live
        clock.t += 1200
        for sid in (a, b, x):
            c.heartbeat(sid)
    ev = c.discussion(d["discussion"])["proposals"][0]["consensus"]
    assert ev["met"] and [q.get("silent_past_deadline", False) for q in ev["participants"]] == [False, False, True]
    r = c.decide(b, d["discussion"], "BEGIN IMMEDIATE it is", proposal=p)   # joint: a participant decides
    assert r["consensus"] and c.discussion(d["discussion"])["decided_by"] == "b-01"
    assert any("decided" in m and "consensus: yes" in m for m in _dms(c, a))
    assert any("decided" in m for m in _dms(c, x))
    doc = c.doc_show(r["document"])["content"]
    assert "x-01: support (implied)" in doc
    raises("bad_deadline", c.discuss, a, "t", deadline="yesterday")
    raises("bad_deadline", c.discuss, a, "t", deadline="1970-01-01T00:00Z")   # past (the test clock is 1970)


@check
def tasks_and_roles_are_mutual():
    """6: an assignment is an offer the assignee accepts or declines; coeditor/delegate roles too."""
    c, _ = fresh()
    a, b, x = (c.whoami(n)["session_id"] for n in ("a", "b", "x"))
    t = c.task_create(a, "Review the parser", assign="b-01")["task"]
    assert c.task_get(t)["status"] == "offered" and c.task_get(t)["assigned"] == "b-01"
    assert any(f"[{t}] a-01 offers you a task" in m for m in _dms(c, b))
    assert [x["task"] for x in c.poll(b)["tasks"]] == [t]                # the offer shows in b's poll
    raises("forbidden", c.task_accept, x, t)                            # not offered to x
    c.task_accept(b, t)
    assert c.task_get(t)["status"] == "accepted" and any("b-01 accepted" in m for m in _dms(c, a))
    t2 = c.task_create(a, "Rewrite docs", assign="b-01")["task"]
    r = c.task_decline(b, t2, "no time this week")
    assert r["status"] == "open" and c.task_get(t2)["assigned"] is None
    assert "no time this week" in c.task_get(t2)["note"] and any("b-01 declined" in m for m in _dms(c, a))
    c.task_accept(x, t2)                                                  # back to open: anyone may take it
    raises("forbidden", c.task_decline, b, t2)                            # no longer b's
    # roles: advice is immediate, duties need consent
    cl = c.claim(a, "src/", tree=True)["claim"]
    assert c.grant(a, cl, "x-01", "reviewer")["status"] == "granted"
    assert c.grant(a, cl, "b-01", "coeditor")["status"] == "offered"
    assert any(f"offers you the coeditor role" in m for m in _dms(c, b))
    assert {"session": "b-01", "role": "coeditor", "status": "offered"} in c.roles(cl)
    c.role_decline(b, cl, "coeditor", "not my area")
    assert all(r["session"] != "b-01" for r in c.roles(cl)) and any("declined the coeditor" in m for m in _dms(c, a))
    raises("missing", c.role_accept, b, cl, "coeditor")                   # nothing left to accept
    c.grant(a, cl, "b-01", "delegate")
    assert c.role_accept(b, cl, "delegate")["status"] == "granted"
    assert c.grant(a, cl, "b-01", "delegate")["status"] == "granted"      # re-granting keeps it

@check
def old_database_is_migrated():
    """A coord2.db from 0.2.x (no rule/quorum columns) opens and gets the defaults."""
    import sqlite3
    path = TMP / "old.db"
    c = Coord(path)
    with sqlite3.connect(path) as db:                                   # rebuild the 0.2.x table
        db.executescript("DROP TABLE discussions; CREATE TABLE discussions(id INTEGER PRIMARY KEY"
                         " AUTOINCREMENT, project_id TEXT NOT NULL, created_by TEXT NOT NULL,"
                         " created_by_name TEXT NOT NULL, topic TEXT NOT NULL, status TEXT NOT NULL"
                         " DEFAULT 'open', claim_id INTEGER, message_id INTEGER, decision TEXT,"
                         " consensus INTEGER, decided_by TEXT, decided_at REAL,"
                         " decision_document_id INTEGER, created_at REAL NOT NULL);")
        db.execute("INSERT INTO discussions(project_id, created_by, created_by_name, topic, created_at)"
                   " VALUES('default', 'x', 'x-01', 'from 0.2.1', 0)")
        db.execute("CREATE TABLE requests(id INTEGER PRIMARY KEY, body TEXT)")      # unused 0.2.x table
    c = Coord(path)
    with sqlite3.connect(path) as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='requests'").fetchone()   # empty: dropped
        db.execute("CREATE TABLE requests(id INTEGER PRIMARY KEY, body TEXT)")
        db.execute("INSERT INTO requests(body) VALUES('keep me')")
    Coord(path)
    with sqlite3.connect(path) as db:                                   # not empty: never dropped
        assert db.execute("SELECT body FROM requests").fetchone()[0] == "keep me"
    show = c.discussion("D1")
    assert show["rule"] == "unanimous" and show["quorum"] == 2 and show["topic"] == "from 0.2.1"

def _udiff(a: str, b: str) -> str:
    """diff -u of two texts (difflib + GNU's "\\ No newline at end of file")."""
    import difflib
    return "".join(l if l.endswith("\n") else l + "\n\\ No newline at end of file\n"
                   for l in difflib.unified_diff(a.splitlines(True), b.splitlines(True), "a", "b"))


@check
def textpatch_engine():
    import random
    from coordination import textpatch as tp
    random.seed(11)
    words = ["alpha", "beta", "", "## Title", "- item", "--- rule", "+ plus", "@@ x", "\\ back"]
    n = 0
    for _ in range(1500):                                              # diff -> apply round-trips
        a = "\n".join(random.choice(words) for _ in range(random.randint(0, 25))) + ("\n" if random.random() < .7 else "")
        bl = a.splitlines()
        for _ in range(random.randint(1, 4)):
            r = random.random()
            if r < .4 and bl:
                bl.pop(random.randrange(len(bl)))
            elif r < .8:
                bl.insert(random.randint(0, len(bl)), random.choice(words))
            elif bl:
                bl[random.randrange(len(bl))] += "!"
        b = "\n".join(bl) + ("\n" if bl and random.random() < .7 else "")
        if a != b:
            assert tp.apply(a, _udiff(a, b))[0] == b, (a, b)
            n += 1
    assert n > 1000
    crlf = "one\r\ntwo\r\n"                                              # CRLF documents stay CRLF
    assert tp.apply(crlf, "@@ -1,2 +1,2 @@\n one\r\n-two\r\n+TWO\r\n")[0] == "one\r\nTWO\r\n"
    for bad in ("hello", "@@ -1,2 +1,2 @@\n x\n", "@@ -1 +1 @@\n*bad\n"):
        try:
            tp.parse(bad)
            raise AssertionError(bad)
        except tp.PatchError:
            pass


@check
def doc_patch_merges_concurrent_edits():
    c, _ = fresh()
    a = c.whoami("a")["session_id"]; b = c.whoami("b")["session_id"]
    sections = "\n".join(f"## Section {i}\n" + "\n".join(f"line {i}.{j}" for j in range(8)) + "\n" for i in range(1, 6))
    doc = c.doc_create(a, "Design", content=sections)["document"]
    r1 = c.doc_show(doc)["content"]
    only_s1 = r1.replace("line 1.3", "line 1.3 - clarified by a")         # a edits section 1 from r1
    only_s4 = r1.replace("line 4.5", "line 4.5 - fixed by b")             # b edits section 4 from r1
    assert c.doc_patch(b, doc, 1, _udiff(r1, only_s4))["merged"] is False  # b first: r2
    r = c.doc_patch(a, doc, 1, _udiff(r1, only_s1))                        # a's r1 patch lands on r2
    assert r["merged"] and r["revision"] == 3
    final = c.doc_show(doc)["content"]
    assert "clarified by a" in final and "fixed by b" in final             # both edits kept
    assert "merged onto r2" in c.doc_history(doc)[-1]["message"]
    # overlapping edits are refused, with the current content and the conflicting hunk
    clash = r1.replace("line 4.5", "line 4.5 - a's other idea")
    err = raises("revision_conflict", c.doc_patch, a, doc, 1, _udiff(r1, clash))
    assert err.data["current_revision"] == 3 and "fixed by b" in err.data["current_content"]
    assert err.data["failed_hunks"][0].startswith("@@")
    # a patch that does not even match its own base revision
    raises("patch_invalid", c.doc_patch, a, doc, 3, _udiff("unrelated\n", "text\n"))
    raises("patch_invalid", c.doc_patch, a, doc, 3, "not a diff")
    raises("missing", c.doc_patch, a, doc, 99, _udiff(r1, only_s1))
    small = c.doc_create(a, "Small", content="x\n")["document"]
    raises("no_change", c.doc_patch, a, small, 1, "@@ -1 +1 @@\n-x\n+x\n")
    edit = _udiff(final, final.replace("line 2.0", "line 2.0 edited"))
    first = c.doc_patch(a, doc, 3, edit, client_id="k1")
    again = c.doc_patch(a, doc, 3, edit, client_id="k1")                  # a retried call is replayed
    assert again["replayed"] and again["revision"] == first["revision"] == 4
    assert c.doc_show(doc)["revision"] == 4
    # a final document cannot be patched
    d = c.discuss(a, "t")["discussion"]
    dec = c.decide(a, d, "x", consensus=False, reason="test")["document"]
    raises("final", c.doc_patch, a, dec, 1, _udiff("a\n", "b\n"))

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


def _ca_race(args):
    d, name = args
    from coordination import pki as p
    return [p.issue(d, f"{name}-{i}")[0].name for i in range(3)]


@check
def openssl_is_chosen_and_failures_are_explained():
    """COORD_OPENSSL wins; a crashing or misconfigured openssl gets a message that names it."""
    import contextlib, io, subprocess
    from coordination import sslbin
    saved = os.environ.get("COORD_OPENSSL")
    fake = TMP / ("fake-openssl.cmd" if sys.platform == "win32" else "fake-openssl")
    try:
        os.environ["COORD_OPENSSL"] = str(fake)
        sslbin.openssl.cache_clear()
        assert sslbin.openssl() == str(fake)
        assert sslbin.crashed(3221225477) and sslbin.crashed(-11) and not sslbin.crashed(1)
        crash = subprocess.CalledProcessError(3221225477, [str(fake)], stderr=b"")
        cnf = subprocess.CalledProcessError(1, [str(fake)], stderr=b'Can\'t open "C:\\Craft\\etc\\ssl\\/openssl.cnf" for reading')
        for err, expect in ((crash, "crashed (exit 0xc0000005)"), (cnf, "config file that does not exist")):
            def boom(*a, _e=err, **k):
                raise _e
            real, pki._run = pki._run, boom
            out = io.StringIO()
            try:
                with contextlib.redirect_stderr(out):
                    rc = pki.main(["--dir", str(TMP / "pki-fake"), "init"])
            finally:
                pki._run = real
            assert rc == 1 and expect in out.getvalue() and str(fake) in out.getvalue(), out.getvalue()
    finally:
        if saved is None:
            os.environ.pop("COORD_OPENSSL", None)
        else:
            os.environ["COORD_OPENSSL"] = saved
        sslbin.openssl.cache_clear()


@check
def ca_is_safe_across_processes():
    """coord-server renewing while coord-admin enrolls: every serial unique, the CA database valid
    (Windows has no fcntl - the lock must still hold across processes)."""
    if not have_openssl():
        print("  (skipped: no openssl)")
        return
    d = pki.init(TMP / "pki-race")
    ctx = mp.get_context("spawn")
    with ctx.Pool(6) as pool:
        pool.map(_ca_race, [(str(d), f"agent{i}") for i in range(6)])
    serials = [c["serial"] for c in pki.listing(d)]
    assert len(serials) == 18 and len(set(serials)) == 18, serials
    pki.revoke(d, "agent0-0")                                           # openssl can still index the db

@check
def moved_ca_still_issues():
    """A CA dir moved with its checkout (renamed repo): openssl.cnf is rewritten, not left pointing at the old path."""
    if not have_openssl():
        print("  (skipped: no openssl)")
        return
    old = pki.init(TMP / "pki-old")
    new = TMP / "pki-moved"
    shutil.copytree(old, new)
    shutil.rmtree(old)
    assert pki.main(["--dir", str(new), "issue", "agent-m"]) == 0
    pki.Authority(new)
    assert new.as_posix() in (new / "openssl.cnf").read_text()

@check
def mtls_with_crl():
    if not have_openssl():
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
        import os
        import subprocess
        # one-step enroll (management CLI): identity bundle + default link; the client then
        # needs only COORD_IDENTITY
        cfg_home = TMP / "xdg"
        base = {k: v for k, v in os.environ.items() if not k.startswith("COORD_")}
        base.update(XDG_CONFIG_HOME=str(cfg_home), COORD_PROJECT="enroll", PYTHONPATH=str(Path(__file__).parent))

        def cli(*args, admin=True, **extra):
            cmd = [sys.executable, "-m", "coordination.pki", "--dir", str(d)]
            p = subprocess.run(cmd + list(args), env=dict(base, **extra), capture_output=True, text=True, cwd=TMP)
            assert p.returncode == 0, p.stdout + p.stderr
            return json.loads(p.stdout)

        r = cli("enroll", "agent-a", "--url", url, admin=True)
        assert r["cert_reused"] and r["default"]                      # first identity -> default
        if os.name != "nt":
            assert oct((cfg_home / "coord" / "agent-a" / "agent.key").stat().st_mode & 0o777) == "0o600"
        r = cli("enroll", "agent-b", "--url", url, admin=True)        # agent-b was revoked above
        assert not r["cert_reused"] and not r["default"]
        bundle = cfg_home / "coord" / "agent-a"                        # the bundle alone is enough
        via = RemoteCoord(url, ca=str(bundle / "ca.crt"), cert=str(bundle / "agent.crt"), key=str(bundle / "agent.key"))
        assert via.whoami(family="x")["name"].startswith("x-")
        assert cli("enroll", "agent-b", "--url", url, "--default", admin=True)["default"]
    finally:
        httpd.shutdown()


@check
def mtls_renewal_and_live_revocation():
    """The server asks management for a renewal once the cert is 15 days old and hands it to
    the client, which installs it; revocation applies to the next request, no restart."""
    if not have_openssl():
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
    if not have_openssl():
        print("  (skipped: no openssl)")
        return
    import socket
    import subprocess
    import time

    def lifetime_days(pem_or_path):
        src = ["-in", str(pem_or_path)] if isinstance(pem_or_path, Path) else []
        out = subprocess.run([openssl(), "x509", *src, "-noout", "-startdate", "-enddate"],
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
    if not have_openssl():
        print("  (skipped: no openssl)")
        return
    import subprocess
    d = TMP / "pki-old"
    d.mkdir()
    subprocess.run([openssl(), "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                    "-keyout", str(d / "ca.key"), "-out", str(d / "ca.crt"), "-days", "3650", "-subj", "/CN=coord-ca",
                    "-addext", "basicConstraints=critical,CA:TRUE"], check=True, capture_output=True)
    old_ca = (d / "ca.crt").read_text()
    text = lambda f: subprocess.run([openssl(), "x509", "-in", str(f), "-noout", "-text"], capture_output=True,
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
        subprocess.run([openssl(), "verify", "-x509_strict", "-CAfile", str(d / "ca.crt"), str(leaf)],
                       check=True, capture_output=True)                           # strict chain OK
    finally:
        if saved is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = saved

@check
def a2a_binding():
    """The A2A v1.0 face of the server: agent card per auth mode, JSON-RPC errors, webhook policy."""
    from coordination import a2a

    def rpc(url, method, params=None, token=None):
        headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
        body = json.dumps({"jsonrpc": "2.0", "id": 7, "method": method, "params": params or {}}).encode()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(urllib.request.Request(url + "/a2a", data=body, headers=headers), timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return json.loads(e.read())

    c, _ = fresh()
    httpd = build_server(c, "127.0.0.1", 0, push_allow=[".dci.local"])
    url = f"http://127.0.0.1:{_serve(httpd)}"
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        card = json.loads(opener.open(url + "/.well-known/agent-card.json", timeout=10).read())
        assert card["supportedInterfaces"][0] == {"url": url + "/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        assert card["capabilities"]["pushNotifications"] and card["securitySchemes"] == {}
        assert {s["id"] for s in card["skills"]} == {"coord-ops", "delegate"}
        assert rpc(url, "NoSuchMethod")["error"]["code"] == a2a.METHOD_NOT_FOUND
        assert rpc(url, "SubscribeToTask", {"id": "T1"})["error"]["code"] == a2a.UNSUPPORTED
        assert rpc(url, "GetTask", {"id": "T99"})["error"]["code"] == a2a.TASK_NOT_FOUND
        bad = opener.open(urllib.request.Request(url + "/a2a", data=b'{"id": 1}', headers={"Content-Type": "application/json"}))
        assert json.loads(bad.read())["error"]["code"] == a2a.INVALID_REQUEST
        sid = rpc(url, "SendMessage", {"message": {"messageId": "m", "role": "ROLE_USER",
                                                   "parts": [{"data": {"op": "whoami", "args": {"family": "a"}}}]}}
                  )["result"]["message"]["parts"][0]["data"]["session_id"]
        t = rpc(url, "SendMessage", {"message": {"messageId": "m2", "role": "ROLE_USER", "parts": [{"text": "do it"}],
                                                 "metadata": {"coord": {"session": sid}}}})["result"]["task"]
        assert t["status"]["state"] == "TASK_STATE_SUBMITTED"
        refused = rpc(url, "CreateTaskPushNotificationConfig", {"taskId": t["id"], "url": "http://10.0.0.5/hook"})
        assert refused["error"]["code"] == a2a.INVALID_PARAMS                  # SSRF guard: not allowed
        cfg = rpc(url, "CreateTaskPushNotificationConfig", {"taskId": t["id"], "url": "https://ci.dci.local/hook"})["result"]
        assert rpc(url, "ListTaskPushNotificationConfigs", {"taskId": t["id"]})["result"]["configs"][0]["id"] == cfg["id"]
        assert rpc(url, "DeleteTaskPushNotificationConfig", {"taskId": t["id"], "id": cfg["id"]})["result"] == {}
        assert rpc(url, "ListTaskPushNotificationConfigs", {"taskId": t["id"]})["result"]["configs"] == []
        assert rpc(url, "CancelTask", {"id": t["id"]})["error"]["code"] == a2a.INVALID_PARAMS   # needs a session
        assert rpc(url, "CancelTask", {"id": t["id"], "metadata": {"coord": {"session": sid}}}
                   )["result"]["status"]["state"] == "TASK_STATE_CANCELED"
        assert rpc(url, "CancelTask", {"id": t["id"], "metadata": {"coord": {"session": sid}}}
                   )["error"]["code"] == a2a.TASK_NOT_CANCELABLE
    finally:
        httpd.shutdown()
    assert a2a.host_allowed("http://127.0.0.1:9/x", []) and not a2a.host_allowed("file:///etc/passwd", ["*"])
    assert a2a.host_allowed("https://a.dci.local/", [".dci.local"]) and not a2a.host_allowed("https://dci.local.evil.com/", [".dci.local"])
    assert not a2a.host_allowed("http://169.254.169.254/", [".dci.local"])
    # mTLS / OIDC servers advertise their scheme
    card = a2a.agent_card("https://h", mtls=True, oidc_url=None, version="x")
    assert card["securitySchemes"]["mtls"] == {"mtlsSecurityScheme": {"description": "client certificate from coord-admin enroll"}}
    card = a2a.agent_card("https://h", mtls=False, oidc_url="https://sso/realms/r/.well-known/openid-configuration", version="x")
    assert card["securitySchemes"]["oidc"]["openIdConnectSecurityScheme"]["openIdConnectUrl"].endswith("openid-configuration")

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
    if not have_openssl():
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
from coordination import local
from coordination.client import RemoteCoord
from coordination.server import OIDCIntrospector, build_server
from coordination.service import Coord

# local mode (coord-local, what the TS client runs): SQLite, no server, no PKI
assert local.run({"db": sys.argv[1] + "-local", "op": "whoami", "args": {"family": "local"}})["ok"]

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
