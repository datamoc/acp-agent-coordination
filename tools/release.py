"""Cut a coord release in one command: build, checksum, tag and publish.

What took ten manual steps for v0.13.0 (uv build, npm pack, SHA256SUMS,
git tag -a, gh release create with the wheel, sdist, npm tarball and
checksums) is now:

    uv run tools/release.py 0.14.0 --title "frozen contract" --dry-run
    uv run tools/release.py 0.14.0 --title "frozen contract"

The convention follows v0.10.0: annotated tag `vX` with the message
`coord X`; GitHub release `vX - <title>` carrying the wheel, the sdist,
the npm tarball and a GNU-format SHA256SUMS. --dry-run prints every
command without running anything that mutates.
"""

import argparse
import hashlib
import json
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
TS = ROOT / "clients" / "ts"

ASSETS = ("coord-server-{v}-py3-none-any.whl", "coord-server-{v}.tar.gz", "coord-client-{v}.tgz")


def run(cmd: list[str], cwd: Path | None = None, dry: bool = False) -> str:
    print("+ " + " ".join(cmd))
    if dry:
        return ""
    p = subprocess.run(cmd, cwd=cwd or ROOT, capture_output=True, text=True)
    if p.returncode:
        raise SystemExit(f"{cmd[0]} failed:\n{p.stdout}{p.stderr}")
    return p.stdout.strip()


def versions() -> dict[str, str]:
    with open(ROOT / "pyproject.toml", "rb") as f:
        py = tomllib.load(f)["project"]["version"]
    js = json.loads((TS / "package.json").read_text())["version"]
    return {"pyproject": py, "package.json": js}


def changelog_section(version: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text().splitlines()
    head = f"## {version} "
    start = next((i for i, line in enumerate(text) if line.startswith(head)), None)
    if start is None:
        raise SystemExit(f"CHANGELOG.md has no '{head.strip()}' section")
    end = next((i for i, line in enumerate(text[start + 1:], start + 1) if line.startswith("## ")), len(text))
    return "\n".join(text[start + 1:end]).strip() + "\n"


def news_key(version: str) -> bool:
    core = (ROOT / "coordination" / "core.py").read_text()
    return f'"{version}":' in core


def default_notes(version: str) -> str:
    section = changelog_section(version)
    base = f"https://github.com/datamoc/coord/releases/download/v{version}"
    return (section + "\n## Install\n```sh\n"
            f"uv tool install {base}/coord-server-{version}-py3-none-any.whl\n"
            f"npm install -g {base}/coord-client-{version}.tgz\n```\n")


def sha256sums(files: list[Path], out: Path) -> None:
    lines = []
    for f in files:
        h = hashlib.sha256()
        with open(f, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        lines.append(f"{h.hexdigest()}  {f.name}\n")
    out.write_text("".join(lines))


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("version", help="X.Y.Z (a leading v is stripped)")
    ap.add_argument("--title", default="", help="release title: the release is named 'vX - <title>'")
    ap.add_argument("--notes-file", default="", help="release notes file (default: built from CHANGELOG.md)")
    ap.add_argument("--dry-run", action="store_true", help="print every command, change nothing")
    ap.add_argument("--no-push", action="store_true", help="stop after tagging (no push, no release)")
    ap.add_argument("--no-release", action="store_true", help="push the tag but create no GitHub release")
    ap.add_argument("--force", action="store_true", help="skip the version-agreement preflight")
    a = ap.parse_args()
    version = a.version[1:] if a.version.startswith("v") else a.version
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"not a version: {a.version}")
    dry = a.dry_run

    if not a.force:
        found = versions()
        bad = [f"{k} says {v}" for k, v in found.items() if v != version]
        if bad:
            raise SystemExit("version mismatch: " + "; ".join(bad))
        changelog_section(version)
        if not news_key(version):
            raise SystemExit(f'NEWS in coordination/core.py has no "{version}" entry')
        print(f"versions agree: {version} (pyproject, package.json, CHANGELOG.md, NEWS)")
    status = run(["git", "status", "--porcelain"], dry=False)
    if status:
        raise SystemExit("working tree is not clean - commit first:\n" + status)
    if not dry:
        tag_missing = subprocess.run(["git", "rev-parse", f"v{version}"],
                                     capture_output=True).returncode != 0
        if not tag_missing:
            raise SystemExit(f"tag v{version} already exists locally")
    if not a.title and not a.notes_file and not dry:
        raise SystemExit("--title is required (the release is named 'vX - <title>')")

    run(["uv", "build"], dry=dry)
    run(["npm", "pack", "--pack-destination", str(DIST)], cwd=TS, dry=dry)
    for f in sorted(DIST.glob("coord_server-*")):
        target = DIST / f.name.replace("coord_server-", "coord-server-", 1)
        print(f"+ rename {f.name} -> {target.name}")
        if not dry:
            f.rename(target)
    assets = [DIST / t.format(v=version) for t in ASSETS]
    if not dry:
        missing = [str(f) for f in assets if not f.exists()]
        if missing:
            raise SystemExit("build did not produce: " + ", ".join(missing))
        sha256sums(assets, DIST / "SHA256SUMS")
        print("wrote " + str(DIST / "SHA256SUMS"))
    else:
        print(f"+ write {DIST / 'SHA256SUMS'} (sha256 of the 3 assets)")

    run(["git", "tag", "-a", f"v{version}", "-m", f"coord {version}"], dry=dry)
    if a.no_push:
        print("(stopping before push)")
        return
    run(["git", "push", "origin", f"v{version}"], dry=dry)
    if a.no_release:
        print("(stopping before gh release create)")
        return
    # the title travels in --title; the body stays the CHANGELOG section + Install
    notes = Path(a.notes_file).read_text() if a.notes_file else default_notes(version)
    notes_file = DIST / f"notes-{version}.md"
    if not dry:
        notes_file.write_text(notes)
    else:
        print(f"+ write {notes_file} ({len(notes)} chars of release notes)")
    upload = [str(f) for f in assets] + [str(DIST / "SHA256SUMS")]
    run(["gh", "release", "create", f"v{version}",
         "--title", f"v{version} - {a.title}" if a.title else f"v{version}",
         "--notes-file", str(notes_file), *upload], dry=dry)


if __name__ == "__main__":
    main()
