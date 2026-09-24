#!/usr/bin/env bash
# T26 (v2 section 18) / T27: CI checks what a user installs. Build the wheel and the npm
# tarball, install each into a fresh, isolated environment, then prove the installed
# binaries work end to end: coord-server listens, `coord whoami` registers a session
# against it over HTTP.
#
# Needs: uv, node/npm, git (the project comes from `git remote get-url origin`).
# Runs where CI runs it - Linux (GitHub ubuntu-latest, the GitLab uv image). The wheel is
# py3-none-any and the tarball is plain JS; the Windows runner covers the checkout path.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

# A clean env: nothing from the developer's own coord config (identity, COORD_SERVER,
# proxies) leaks into the smoke test - the rule the test suites follow too.
for v in $(env | sed -n 's/^\(COORD_[A-Z0-9_]*\)=.*/\1/p'); do unset "$v"; done
export COORD_CONFIG="$root/.coord-smoke-no-such-env" NO_PROXY="localhost,127.0.0.1"

# Under Git Bash (a local run of this script) native tools need Windows paths; on CI's
# Linux cygpath does not exist and this is the identity function.
wpath() { command -v cygpath >/dev/null 2>&1 && cygpath -w "$1" || echo "$1"; }

work="$(mktemp -d)"
server=""
port=""
cleanup() {
  status=$?                       # the trap must not mask the result of the run itself
  if [ -n "$server" ]; then
    kill "$server" 2>/dev/null || true
    # Windows (a local run): the bin script spawns coord-server.exe as a child that kill()
    # does not reach - stop the listener by port too. On CI's Linux the bin script execs, so
    # the kill above is the server itself.
    if [ -n "$port" ] && command -v cygpath >/dev/null 2>&1; then
      powershell.exe -NoProfile -NonInteractive -Command \
        "(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue).OwningProcess | ForEach-Object { Stop-Process -Id \$_ -Force -ErrorAction SilentlyContinue }" \
        >/dev/null 2>&1 || true
    fi
    wait "$server" 2>/dev/null || true
  fi
  rm -rf "$work" 2>/dev/null || true
  exit "$status"
}
trap cleanup EXIT

echo "== 1/4  build the wheel =="
rm -rf dist
uv build --out-dir dist
wheel=(dist/*.whl)
echo "        ${wheel[0]}"

echo "== 2/4  uv tool install <wheel> (a fresh venv with its own bin dir) =="
export UV_TOOL_DIR="$(wpath "$work/uv-tools")" UV_TOOL_BIN_DIR="$(wpath "$work/uv-bin")"
uv tool install --force "${wheel[0]}"
export PATH="$work/uv-bin:$PATH"
coord-server --help >/dev/null            # the installed console script runs

echo "== 3/4  npm pack + npm install -g <tgz> (a fresh prefix) =="
( cd clients/ts && npm ci --silent && npm run build --silent && npm pack --pack-destination "$work" >/dev/null )
tarball=("$work"/coord-client-*.tgz)
npm install -g --prefix "$(wpath "$work/npm-global")" "${tarball[0]}"
# Linux puts the bins in <prefix>/bin, npm on Windows in <prefix> itself: cover both.
export PATH="$work/npm-global/bin:$work/npm-global:$PATH"
coord --help >/dev/null                   # the installed bin runs

echo "== 4/4  coord-server + coord whoami, end to end =="
port="$(node -e 'const s=require("net").createServer();s.listen(0,"127.0.0.1",()=>{process.stdout.write(String(s.address().port));s.close()})')"
coord-server --port "$port" --db "$work/coord2.db" >"$work/server.log" 2>&1 &
server=$!
for _ in $(seq 1 60); do
  grep -q "coord-server on" "$work/server.log" && break
  kill -0 "$server" 2>/dev/null || { echo "coord-server exited early:"; cat "$work/server.log"; exit 1; }
  sleep 0.5
done
grep -q "coord-server on" "$work/server.log" || { echo "coord-server never listened:"; cat "$work/server.log"; exit 1; }

whoami="$(COORD_SERVER="http://127.0.0.1:$port" coord --json whoami opencode --model ci)"
case "$whoami" in
  *'"session_id"'*) echo "        $(echo "$whoami" | tr -d '\n' | cut -c1-160)" ;;
  *) echo "whoami failed against http://127.0.0.1:$port:"; echo "$whoami"; exit 1 ;;
esac

echo "OK: a clean install of ${wheel[0]##*/} and ${tarball[0]##*/} works end to end"
