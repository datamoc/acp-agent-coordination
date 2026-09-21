"""Client whoami UUID attach-and-verify. Run: uv run python test_client.py.

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


if __name__ == "__main__":
    check("whoami attaches UUID and accepts echo", attaches_and_accepts_echo)
    check("whoami rejects mismatched echo", rejects_mismatched_echo)
    check("whoami keeps caller-supplied UUID", keeps_caller_supplied_uuid)
    print("\nAll 3 client checks passed.")
