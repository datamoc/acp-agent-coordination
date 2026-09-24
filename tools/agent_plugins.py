"""coord for agent CLIs other than Claude Code and Codex.

plugins/coord is the one source (skill, commands, JS client). It already is:
  - a Claude Code / Codex plugin (.claude-plugin, .codex-plugin; commands in claude-commands/),
  - a Muse Code plugin (reads the Claude manifest): `muse plugins install plugins/coord`,
  - a Gemini CLI / Qwen Code extension (gemini-extension.json + the TOML
    commands/coord/*.toml this script generates): `gemini extensions link plugins/coord`,
    `qwen extensions install plugins/coord` (Qwen reads the Gemini manifest).

CLIs without a plugin format get the skill and commands copied into their
config dir, pointing at this checkout's client (keep the checkout in place):

    uv run tools/agent_plugins.py gen [--check]      # commands/coord/*.toml from claude-commands/*.md
    uv run tools/agent_plugins.py install opencode kilo crush deepcode [--dry-run]
    uv run tools/agent_plugins.py uninstall opencode kilo crush deepcode

A CLI the client cannot recognise from its environment (no marker variable, unlike Muse and
opencode) gets its enrolled identity written into the copied commands: `coord-admin enroll deepcode`
before `install deepcode` makes it run as `COORD_IDENTITY=deepcode node .../cli.js`.
"""

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "coord"
CLIENT = PLUGIN / "client" / "cli.js"
SKILL = PLUGIN / "skills" / "coord" / "SKILL.md"
RUN_LINE = re.compile(r"^Run coord with `node \"\$\{CLAUDE_PLUGIN_ROOT\}/client/cli\.js\"`\..*?SKILL\.md\.\) ", re.M)
SKILL_CLIENT = ("`node <plugin root>/client/cli.js` (the plugin root is two directories up\n"
                "from this SKILL.md)")


def commands() -> dict[str, tuple[str, str]]:
    """name -> (description, body) for plugins/coord/claude-commands/*.md."""
    out = {}
    for f in sorted((PLUGIN / "claude-commands").glob("*.md")):
        m = re.match(r"---\n(.*?)\n---\n\n(.*)", f.read_text(encoding="utf-8"), re.S)
        desc = re.search(r"^description: (.*)$", m.group(1), re.M).group(1).strip()
        body = m.group(2).strip()
        assert RUN_LINE.search(body), f"{f.name}: the 'Run coord with ...' sentence changed, update RUN_LINE"
        out[f.stem] = (desc, body)
    return out


def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def gemini_toml(desc: str, body: str) -> str:
    """A Gemini/Qwen command: /coord:<name>, arguments in {{args}}."""
    run = ("Run coord with `node <extension dir>/client/cli.js`, the extension dir being two directories "
           "up from the `coord` skill's SKILL.md. ")
    prompt = RUN_LINE.sub(lambda _: run, body) + "\n\nArguments given: {{args}}\n"
    assert "'''" not in prompt
    return (f"# Generated from claude-commands/{'{name}'}.md by tools/agent_plugins.py gen - edit that file.\n"
            f"description = {_toml_str(desc)}\nprompt = '''\n{prompt}'''\n")


def generate() -> dict[Path, str]:
    return {PLUGIN / "commands" / "coord" / f"{name}.toml": gemini_toml(desc, body).replace("{name}", name)
            for name, (desc, body) in commands().items()}


def gen(check: bool) -> int:
    stale = []
    for path, text in generate().items():
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            stale.append(path)
            if not check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8", newline="\n")
    for p in stale:
        print(("stale: " if check else "wrote ") + str(p.relative_to(PLUGIN.parents[1])))
    return 1 if check and stale else 0


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def targets(cli: str) -> tuple[Path, Path | None]:
    """(skills dir, commands dir or None) for a CLI without a plugin format."""
    if cli in ("opencode", "kilo"):          # kilo is an opencode fork: same layout under its own name
        root = _config_home() / cli
        return root / "skills", root / "commands"
    if cli == "crush":                        # ~/.config/crush on Windows too (`crush dirs`)
        return Path(os.environ.get("CRUSH_SKILLS_DIR") or _config_home() / "crush" / "skills"), None
    if cli == "deepcode":                     # Deep Code CLI (@vegamo/deepcode-cli): skills only, in ~/.deepcode
        return Path.home() / ".deepcode" / "skills", None
    raise SystemExit(f"unknown CLI {cli!r}: opencode, kilo, crush, deepcode "
                     "(Gemini, Qwen, Muse: install plugins/coord as an extension/plugin, see the README)")


NO_HOST_MARKER = ("deepcode",)   # CLIs the client's hostIdentity() cannot detect (clients/ts/src/config.ts)
SKILL_PATH_FIRST = "`coord` if it is on PATH, else\n"


def identity_prefix(cli: str) -> str:
    """`COORD_IDENTITY=<cli> ` when this CLI cannot be detected but has an enrolled identity."""
    home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "coord"
    return f"COORD_IDENTITY={cli} " if cli in NO_HOST_MARKER and (home / cli / "env").exists() else ""


def installed(cli: str) -> dict[Path, str]:
    prefix = identity_prefix(cli)
    client = CLIENT.as_posix()
    skill = SKILL.read_text(encoding="utf-8")
    assert SKILL_CLIENT in skill and SKILL_PATH_FIRST in skill, "SKILL.md's client sentence changed, update SKILL_CLIENT"
    if prefix:   # a bare `coord` on PATH would run as the default identity: always use the prefixed form
        skill = skill.replace(SKILL_PATH_FIRST + SKILL_CLIENT, f'`{prefix}node "{client}"` (this CLI\'s own identity)')
    files = {Path("coord") / "SKILL.md": skill.replace(SKILL_CLIENT, f'`node "{client}"`')}
    skills, cmds = targets(cli)
    out = {skills / rel: text for rel, text in files.items()}
    if cmds is not None:
        for name, (desc, body) in commands().items():
            body = RUN_LINE.sub(lambda _: f'Run coord with `{prefix}node "{client}"`. ', body)
            body = body.replace("`/coord:join`", "`/coord-join`")
            out[cmds / f"coord-{name}.md"] = (f"---\ndescription: {desc}\n---\n\n{body}\n\n"
                                               "Arguments given: $ARGUMENTS\n")
    return out


def install(clis: list[str], dry_run: bool) -> int:
    for cli in clis:
        for path, text in installed(cli).items():
            print(f"{cli}: {path}")
            if not dry_run:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8", newline="\n")
    return 0


def uninstall(clis: list[str]) -> int:
    for cli in clis:
        skills, cmds = targets(cli)
        if (skills / "coord").is_dir():
            shutil.rmtree(skills / "coord")
            print(f"{cli}: removed {skills / 'coord'}")
        for f in sorted(cmds.glob("coord-*.md")) if cmds and cmds.is_dir() else []:
            f.unlink()
            print(f"{cli}: removed {f}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen", help="write the Gemini/Qwen TOML commands")
    g.add_argument("--check", action="store_true", help="fail if they are stale, write nothing")
    i = sub.add_parser("install", help="copy the skill (and commands) into a CLI's config dir")
    i.add_argument("clis", nargs="+")
    i.add_argument("--dry-run", action="store_true")
    u = sub.add_parser("uninstall")
    u.add_argument("clis", nargs="+")
    a = p.parse_args(argv)
    if a.cmd == "gen":
        return gen(a.check)
    return install(a.clis, a.dry_run) if a.cmd == "install" else uninstall(a.clis)


if __name__ == "__main__":
    sys.exit(main())
