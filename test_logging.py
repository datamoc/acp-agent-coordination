"""Logging split: state-changing calls log at INFO by default, reads at
DEBUG (-v shows everything). Run: uv run python test_logging.py
"""

import logging
from contextlib import contextmanager

import ACP_server as s


def check(name, fn):
    fn()
    print(f"PASS {name}")


@contextmanager
def capture(level):
    records = []

    class H(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = H(level=level)
    old_level, old_handlers, old_propagate = (
        s.logger.level, s.logger.handlers[:], s.logger.propagate,
    )
    s.logger.handlers = [handler]
    s.logger.propagate = False
    s.logger.setLevel(level)
    try:
        yield records
    finally:
        s.logger.setLevel(old_level)
        s.logger.handlers = old_handlers
        s.logger.propagate = old_propagate


def note_levels():
    with capture(logging.DEBUG) as records:
        s._note("post", "in: x")
        s._note("inbox", "in: x")
    by_msg = {r.getMessage(): r.levelno for r in records}
    assert by_msg["post in: x"] == logging.INFO, by_msg
    assert by_msg["inbox in: x"] == logging.DEBUG, by_msg


def classification():
    for agent in ("echo", "inbox", "locks", "presence", "requests", "status"):
        assert agent in s._READ_ONLY, agent
    for agent in ("post", "resolve", "claim", "release",
                  "request", "done", "heartbeat", "whoami"):
        assert agent not in s._READ_ONLY, agent


def default_hides_reads():
    s._configure_logging(False)
    try:
        assert logging.getLogger("acp").level == logging.WARNING
        assert s.logger.level == logging.INFO
        with capture(logging.INFO) as records:
            s._note("post", "in: x")
            s._note("inbox", "in: x")
        assert [r.getMessage() for r in records] == ["post in: x"], records
    finally:
        s.logger.setLevel(logging.NOTSET)


def verbose_shows_all():
    s._configure_logging(True)
    assert logging.getLogger("acp").level == logging.INFO
    assert s.logger.level == logging.DEBUG
    s._configure_logging(False)


if __name__ == "__main__":
    check("note levels route reads to DEBUG", note_levels)
    check("read-only classification", classification)
    check("default mode hides reads", default_hides_reads)
    check("verbose mode shows everything", verbose_shows_all)
    print("\nAll 4 logging checks passed.")
