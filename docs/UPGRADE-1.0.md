# Upgrading from 0.x to 1.0

The steps, in order, and what is safe to assume once you are on 1.0. Written for **T39**; the
promise itself is [docs/COMPATIBILITY.md](COMPATIBILITY.md).

## 1. Back up first

The database is one file. Take it while the server runs - a JSON export reads every table inside
one connection, so it cannot catch a half-written state:

```sh
coord-db export --out before-1.0.json
```

**Downgrade is not promised.** Going back to 0.x after a 1.0 server has opened the database is
not something the project tests. The export is your rollback.

## 2. Upgrade the server

```sh
uv sync                    # a checkout
# or, for an installed copy:
uv tool install --force coord
```

Restart `coord-server`. **The migration is additive** - nullable columns and new tables, nothing
rewritten - and it is not a claim taken on trust: `older_databases_still_open` builds fixture
databases with the code of **0.2, 0.5, 0.9 and 0.10** on every CI run and opens each one with the
current version, then writes to it.

On start the server posts its `NEWS` entry to every project it reaches, so the agents learn what
changed without being told separately.

## 3. Upgrade the client

```sh
npm install -g @datamoc/coord-client   # or the release's .tgz
```

Client and server may differ: the client warns at `whoami` when they do, and an op the server
does not know comes back as `bad_op` rather than silently doing something else.

## 4. Refresh the plugin copies

The plugin ships the compiled client, so a copy of it goes stale - refresh whichever you use:

```sh
claude plugin update coord@coord                     # restart Claude Code afterwards
codex plugin marketplace add ~/dev/coord && codex plugin add coord@coord
muse plugins update coord
qwen extensions uninstall coord && qwen extensions install ~/dev/coord/plugins/coord
uv run tools/agent_plugins.py install opencode kilo crush deepcode
```

Gemini is *linked* to the checkout (`gemini extensions link plugins/coord`) and needs nothing.
On Windows, `powershell -ExecutionPolicy Bypass -File tools\setup-windows.ps1` refreshes the
`coord` command and reports if it points at another checkout.

## 5. Sessions after the restart

Restarting the server ends nothing in the database, but sessions lapse by TTL and the ones that
were alive get `dead_session`. That is not a failure:

```sh
coord whoami claude        # resumes the same session - same name, new generation
coord context              # where things stand, what waits for you
```

Claims held by sessions that are gone lapse on their own; a task whose creator and assignee have
both vanished is reclaimable by any live session (0.10.1) instead of blocking its dependents.

## 6. Coming from 0.2.x or earlier

The ACP layer (`ACP_server.py`, `ACP_client.py`, the `acp` plugin, `ACP_client.py` in an agent's
skill) was retired in **0.3.0**: `coord` does all of it now, and the old `coord.db` stays on disk
unused. The command-by-command mapping - `post`→`coord post`, `request`→`coord task create`, the
`acp` plugin→`coord` - is in the README under
[Migrating from 0.2.x](https://github.com/datamoc/coord#migrating-from-02x).

## 7. What 1.0 promises

From [docs/COMPATIBILITY.md](COMPATIBILITY.md):

- **Ops are not removed within 1.x**; a deprecation gets two minor releases and three
  announcements (NEWS, README, the error it starts returning) first.
- **An argument is never made required later and never repurposed**, and a new capability arrives
  as a new op - because the server refuses an argument it does not know.
- **Results grow, never shrink**: a client must ignore fields it does not recognise.
- **Error codes are part of the contract** (`errors` in `schema/ops.json`); the message is prose
  and may change, `error` and `data` are what to match on.
- **N-1 clients** keep working, and a newer client against an older server degrades rather than
  fails.
- **A 1.x server opens every older database**; downgrade is not promised.

## 8. Check it

```sh
coord server                # version, features, the client's version beside them
coord --json whoami claude  # resumed, same name
```

The wire contract is at `schema/ops.json`; `uv run tools/gen_schema.py --check` fails if the
server and the contract disagree, which is the same check CI runs.
