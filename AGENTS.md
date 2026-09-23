# AGENTS.md

## Agent efficiency: token economy first

**Critical for coordination:** Any task automatable by a script should be done that way—not by spawning an agent. Every agent call costs tokens; in multi-agent coordination, spawning agents for automatable work wastes tokens on coordination overhead instead of the actual work.

**Scripts, not agents, for:** running tests • linting • building • git operations • file transformations • grepping/searching • data processing • environment setup • batch edits • log inspection.

**Agents for:** reasoning (diagnosis, design, code review) • judgment calls (architecture, API design, naming) • writing from scratch when shape is unclear • complex cross-file consistency.

**Token economy:** In coordination, ask before every agent spawn: "Is this automatable?" If yes, script it. If no, spawn the agent. See CLAUDE.md for the full principle and examples.

## This repo

- Server: Python stdlib (`coordination/`): `coord-server` (+ A2A v1.0 binding in `a2a.py`), `coord-admin` (local CA), `coord-local` (local-mode bridge).
- Client: TypeScript (`clients/ts`): the `coord` CLI and `CoordClient`; the plugin `plugins/coord` ships its compiled copy (`npm run bundle-plugin`).
- Contract: `schema/ops.json` is generated from `coordination/service.py` (`uv run tools/gen_schema.py`); both sides are tested against it. No ACP, no MCP transport.
- Loopback only unless TLS + mTLS or Keycloak are configured (README `## Security and identities`).

## Using coord (agents)

- Run the `coord` command only (on PATH, or `node plugins/coord/client/cli.js`), from any directory.
- `coord --json whoami <family>`, then `COORD_SESSION=<id>` on every later command; `coord context` at start, `coord poll` when idle.
- Identity (mTLS bundle or Keycloak) comes from `~/.config/coord/env` or `COORD_IDENTITY=<name>`; the human sets it up (`coord-admin enroll <client-name>`, `coord login`). Renewal is automatic.
- Windows setup is `tools/setup-windows.ps1` (`-Codex` for Codex's sandbox, `-AutoStart` for the server); symptoms and fixes: README `## Troubleshooting`.
- Claim before editing (`coord claim <path>`, `dir/` = tree); keep it while asking for help: `coord ask --claim C12 --to <session> "..."`.
- Delegate with `coord task create "..." --assign <session>`; long analyses in `coord doc create`; debates in `coord discuss`/`propose`/`react`/`decide`.
- Never touch `pki/`, certificates, `~/.config/coord/`, `coord-admin` or `coord-server`.

## GitLab

- Internal projects live on GitLab: use `glab` (issues, MRs, CI) - it acts as the human, so no merge/approve/close without being asked.
- Reference `#issue` / `!mr` in `coord claim --note`, task titles and `--kind done` posts.

## Dev

- Python: `uv run tools/gen_schema.py --check`, `uv run tools/agent_plugins.py gen --check` (after editing `plugins/coord/claude-commands/`, run `gen`) and `uv run test_coord.py` (temp dirs; claim race, PKI, renewal, OIDC, cert sources, A2A).
- openssl is resolved by `coordination/sslbin.py` (`COORD_OPENSSL`, then Git for Windows' copy on Windows, then PATH): never call `"openssl"` directly.
- TS: `cd clients/ts && npm test` (against the real Python server, incl. @a2a-js/sdk interop); `npm run bundle-plugin` after client changes (CI checks it).
- Runtime state (`coord2.db*`, `.coord-session`, `pki/`, `clients/ts/node_modules`, `clients/ts/dist`) is gitignored; never commit it.
