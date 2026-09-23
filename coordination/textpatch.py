"""Apply unified diffs (`diff -u`, `git diff`) to text - the engine behind `doc_patch`.

Exact matching only (no fuzz): a hunk applies where its context and removed lines appear
verbatim, searched from the position the diff names and moving outwards, never before the
previous hunk. That lets a diff made against an older revision land on a newer one when the
edits do not overlap, and refuses it - hunk by hunk - when they do.
"""

import re
from dataclasses import dataclass, field

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(Exception):
    """The diff cannot be parsed, or some hunks do not apply (`failed` = their indexes)."""

    def __init__(self, message: str, failed: list[int] | None = None):
        super().__init__(message)
        self.failed = failed or []


@dataclass
class Hunk:
    old_start: int
    header: str
    want: tuple[int, int] = (1, 1)                               # (old, new) line counts from the header
    lines: list[tuple[str, str]] = field(default_factory=list)   # (" " | "-" | "+", text)
    no_newline: set[str] = field(default_factory=set)            # "old" / "new" / "both": last line has no \n

    @property
    def old(self) -> list[str]:
        return [t for tag, t in self.lines if tag != "+"]

    @property
    def new(self) -> list[str]:
        return [t for tag, t in self.lines if tag != "-"]


def _lines(text: str) -> list[str]:
    """Split on \n only: \r (CRLF documents), form feeds, U+2028 stay part of the line."""
    parts = text.split("\n")
    return parts[:-1] if parts and parts[-1] == "" else parts


def parse(patch: str) -> list[Hunk]:
    hunks: list[Hunk] = []
    for raw in _lines(patch):
        if raw.startswith(("--- ", "+++ ", "diff ", "index ", "new file", "deleted file")) and not (
                hunks and _inside(hunks[-1])):
            continue
        m = _HUNK.match(raw)
        if m:
            hunks.append(Hunk(old_start=int(m.group(1)), header=raw,
                              want=(int(m.group(2) or 1), int(m.group(4) or 1))))
            continue
        if not hunks:
            if raw.strip():
                raise PatchError(f"not a unified diff (no @@ hunk before: {raw[:60]!r})")
            continue
        h = hunks[-1]
        if raw.startswith("\\"):                                  # "\ No newline at end of file"
            if h.lines:
                h.no_newline.add("old" if h.lines[-1][0] == "-" else "new" if h.lines[-1][0] == "+" else "both")
            continue
        tag, text = (raw[0], raw[1:]) if raw else (" ", "")
        if tag not in " -+":
            raise PatchError(f"bad line in hunk {h.header!r}: {raw[:60]!r}")
        h.lines.append((tag, text))
    for h in hunks:
        want_old, want_new = h.want
        if (len(h.old), len(h.new)) != (want_old, want_new):
            raise PatchError(f"hunk {h.header!r} has {len(h.old)}/{len(h.new)} lines, header says {want_old}/{want_new}")
    if not hunks:
        raise PatchError("empty patch: no @@ hunk")
    return hunks


def _inside(h: Hunk) -> bool:
    return len(h.old) < h.want[0] or len(h.new) < h.want[1]


def _find(lines: list[str], block: list[str], expected: int, lo: int) -> int | None:
    """Index where `block` occurs in lines[lo:], nearest to `expected`."""
    if not block:
        return max(lo, min(expected, len(lines)))
    best = None
    for p in range(lo, len(lines) - len(block) + 1):
        if lines[p:p + len(block)] == block and (best is None or abs(p - expected) < abs(best - expected)):
            best = p
    return best


def apply(text: str, patch: str | list[Hunk]) -> tuple[str, list[int]]:
    """Return (new text, per-hunk offset from the position the diff names). Raises PatchError."""
    hunks = parse(patch) if isinstance(patch, str) else patch
    lines = _lines(text)
    final_newline = text.endswith("\n")
    out: list[str] = []
    cursor, shift, offsets, failed = 0, 0, [], []
    for i, h in enumerate(hunks):
        expected = (h.old_start if not h.old else h.old_start - 1) + shift
        p = _find(lines, h.old, expected, cursor)
        if p is None:
            failed.append(i)
            continue
        offsets.append(p - (expected - shift))
        shift = p - (h.old_start if not h.old else h.old_start - 1)
        out += lines[cursor:p] + h.new
        cursor = p + len(h.old)
        if cursor >= len(lines):     # this hunk ends the text: its markers say how the new text ends
            final_newline = not (h.no_newline & {"new", "both"})
    if failed:
        names = ", ".join(hunks[i].header for i in failed)
        raise PatchError(f"{len(failed)} of {len(hunks)} hunk(s) do not apply: {names}", failed)
    out += lines[cursor:]
    return "\n".join(out) + ("\n" if final_newline and out else ""), offsets
