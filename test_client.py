"""Client whoami UUID attach-and-verify and compact-post lint.
Run: uv run python test_client.py.

The CLI attaches a one-off UUID to bare-family whoami calls and refuses
(listed as a loud failure, exit 2) any reply that does not echo it back,
so a mismatched name can never be silently adopted.
"""

import contextlib
import io
import uuid

import ACP_client as c


def run_main(reply_fn, argv):
    orig = c.call
    c.call = lambda agent, text: reply_fn(agent, text)  # noqa: E731
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            c.main(argv)
        return ("ok", out.getvalue(), err.getvalue())
    except SystemExit as e:
        return (e.code, out.getvalue(), err.getvalue())
    finally:
        c.call = orig


def check(name, fn):
    fn()
    print(f"PASS {name}")


def attaches_and_accepts_echo():
    seen = {}

    def reply(agent, text):
        seen["agent"], seen["text"] = agent, text
        return f"you are muse-01 [{text.split()[-1]}]"

    code, out, _ = run_main(reply, ["whoami", "muse"])
    assert code == "ok", (code, out)
    assert seen["agent"] == "whoami", seen
    nonce = seen["text"].split()[-1]
    uuid.UUID(nonce)  # a real UUID was attached
    assert out.strip() == f"you are muse-01 [{nonce}]", out


def rejects_mismatched_echo():
    def reply(agent, text):
        return "you are muse-99 [00000000-0000-0000-0000-000000000000]"

    code, out, err = run_main(reply, ["whoami", "muse"])
    assert code == 2, (code, out)
    assert out == "", out  # nothing usable on stdout
    assert "did not echo" in err, err


def keeps_caller_supplied_uuid():
    seen = {}
    mine = "11111111-2222-4333-8444-555555555555"

    def reply(agent, text):
        seen["text"] = text
        return f"you are muse-07 [{mine}]"

    code, out, _ = run_main(reply, ["whoami", f"muse {mine}"])
    assert code == "ok", (code, out)
    assert seen["text"] == f"muse {mine}", seen  # sent through untouched
    assert out.strip() == f"you are muse-07 [{mine}]", out


def lint_accepts_compact_posts():
    for text in (
        "claude-02: D @abc123 ok:tsc,tests. R claim.",
        "codex-01: T #66 combat rolls",
        "opencode-session: Q/問 #12 scope?",
        "muse-01: V",
    ):
        assert c.lint_post(text) is None, text


def lint_rejects_bad_posts():
    cases = {
        "D/portStrings budget: 3 lines left": "no session prefix",
        "no colon at all": "no session prefix",
        "D: tag used as sender": "no session prefix",
        "codex-02: adding only the pins": "status tag",
        "codex-02: Trinity is not a tag": "status tag",
        "claude-01: D " + "x" * c.MAX_POST_CHARS: "chars (max",
    }
    for text, why in cases.items():
        problem = c.lint_post(text)
        assert problem and why in problem, (text, problem)


def rejected_post_is_not_sent():
    sent = []
    code, _, err = run_main(lambda a, t: sent.append(t) or "posted #1", ["post", "codex-02: no tag"])
    assert code == 1 and not sent, (code, sent)
    assert "ACP REJECTED (nothing sent)" in err, err


if __name__ == "__main__":
    check("whoami attaches UUID and accepts echo", attaches_and_accepts_echo)
    check("whoami rejects mismatched echo", rejects_mismatched_echo)
    check("whoami keeps caller-supplied UUID", keeps_caller_supplied_uuid)
    check("lint accepts compact posts", lint_accepts_compact_posts)
    check("lint rejects untagged, unprefixed and long posts", lint_rejects_bad_posts)
    check("rejected post is not sent", rejected_post_is_not_sent)
    print("\nAll 6 client checks passed.")
