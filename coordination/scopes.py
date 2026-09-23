"""Repo-relative claim scopes: normalization and overlap rules."""

import os
import re


class ScopeError(ValueError):
    pass


def normalize(raw: str, case_insensitive: bool | None = None) -> tuple[str, bool]:
    """Return (normalized_path, looks_like_dir). Rejects absolute paths and
    `..` escapes; backslashes become slashes; Windows folds case."""
    if case_insensitive is None:
        case_insensitive = os.name == "nt"
    s = (raw or "").strip().replace("\\", "/")
    if not s:
        raise ScopeError("empty scope")
    is_dir = s.endswith("/") or s in (".", "./")
    if s.startswith("/") or re.match(r"^[A-Za-z]:", s):
        raise ScopeError(f"absolute path not allowed (use a repo-relative path): {raw}")
    parts = []
    for p in s.split("/"):
        if p in ("", "."):
            continue
        if p == "..":
            raise ScopeError(f"'..' escapes are not allowed: {raw}")
        parts.append(p)
    path = "/".join(parts)
    if case_insensitive:
        path = path.lower()
    return path, is_dir or path == ""


def display(scope_type: str, path: str) -> str:
    if scope_type == "tree":
        return (path + "/") if path else "./"
    return path


def _within(child: str, parent: str) -> bool:
    return parent == "" or child == parent or child.startswith(parent + "/")


def overlaps(t1: str, s1: str, t2: str, s2: str) -> bool:
    if t1 == "exact" and t2 == "exact":
        return s1 == s2
    if t1 == "tree" and t2 == "tree":
        return _within(s1, s2) or _within(s2, s1)
    if t1 == "tree":
        return _within(s2, s1)
    return _within(s1, s2)


def contains(outer_type: str, outer: str, inner_type: str, inner: str) -> bool:
    """True when (inner_type, inner) lies entirely inside the outer scope."""
    if outer_type == "exact":
        return inner_type == "exact" and inner == outer
    return _within(inner, outer)


def canonical_project(remote_url: str) -> str:
    """git remote URL -> canonical key like github.com/org/repo."""
    u = (remote_url or "").strip()
    if not u:
        return "default"
    u = re.sub(r"^[a-z+]+://", "", u)
    u = re.sub(r"^[^@/]+@", "", u)
    u = re.sub(r"^([^/:]+):(?!\d)", r"\1/", u)
    u = re.sub(r"^([^/:]+):\d+/", r"\1/", u)       # ssh://host:2222/... == https://host/...
    u = re.sub(r"\.git/?$", "", u).rstrip("/")
    return u.lower()
