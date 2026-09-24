// Test helpers: the real Python server/admin/local bridge from this checkout, temp dirs, free ports.
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdtempSync } from "node:fs";
import net from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

export const REPO = fileURLToPath(new URL("../../../", import.meta.url));
export const CLI = fileURLToPath(new URL("../dist/cli.js", import.meta.url));
const venvPy = join(REPO, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
export const PYTHON = process.env.COORD_TEST_PYTHON || (existsSync(venvPy) ? venvPy : "python3");

export const tmp = (name = "coordts-") => mkdtempSync(join(tmpdir(), name));

/** A clean env: nothing from the developer's own coord config or proxies leaks into tests. */
export function cleanEnv(extra = {}) {
  const env = Object.fromEntries(Object.entries(process.env).filter(([k]) => !k.startsWith("COORD_")));
  return { ...env, COORD_CONFIG: join(tmpdir(), "coord-no-such-config"), PYTHONPATH: REPO, NO_PROXY: "localhost,127.0.0.1",
           COORD_USER: "", ...extra };      // COORD_USER="": the family-NN names these tests expect
}

export const py = (args, env = cleanEnv(), input) =>
  spawnSync(PYTHON, args, { cwd: REPO, env, encoding: "utf8", input });

export function admin(pkiDir, args, env = cleanEnv()) {
  const p = py(["-m", "coordination.pki", "--dir", pkiDir, ...args], env);
  if (p.status !== 0) throw new Error(`coord-admin ${args.join(" ")}: ${p.stderr}`);
  return JSON.parse(p.stdout);
}

export async function freePort() {
  return new Promise((ok) => {
    const s = net.createServer().listen(0, "127.0.0.1", () => { const { port } = s.address(); s.close(() => ok(port)); });
  });
}

/** Start `coord-server` and wait until it listens. Returns { url, stop }. */
export async function startServer(args = [], env = cleanEnv()) {
  const port = await freePort();
  const proc = spawn(PYTHON, ["-m", "coordination.server", "--port", String(port), ...args], { cwd: REPO, env });
  let out = "";
  await new Promise((ok, fail) => {
    const t = setTimeout(() => fail(new Error(`server did not start: ${out}`)), 20_000);
    const on = (d) => { out += d; if (out.includes("coord-server on")) { clearTimeout(t); ok(); } };
    proc.stdout.on("data", on);
    proc.stderr.on("data", (d) => { out += d; });
    proc.once("exit", (c) => { clearTimeout(t); fail(new Error(`server exited ${c}: ${out}`)); });
  });
  const scheme = args.includes("--pki") || args.includes("--tls-cert") ? "https" : "http";
  return { url: `${scheme}://localhost:${port}`, port, log: () => out, stop: () => new Promise((r) => { proc.once("exit", r); proc.kill(); }) };
}

/** Run the built `coord` CLI. */
export function coord(args, { env = cleanEnv(), cwd = tmp(), input } = {}) {
  const p = spawnSync(process.execPath, [CLI, ...args], { env, cwd, encoding: "utf8", input });
  let json = null;
  try { json = JSON.parse(p.stdout); } catch { /* human output */ }
  return { code: p.status, out: p.stdout, err: p.stderr, json };
}

export const localEnv = (db, extra = {}) => cleanEnv({ COORD_DB: db, COORD_LOCAL: `${PYTHON} -m coordination.local`, COORD_PROJECT: "ts-proj", ...extra });

/** Like coord(), without blocking this process (needed when a fake service runs in the test itself). */
export function coordAsync(args, { env = cleanEnv(), cwd = tmp(), input } = {}) {
  return new Promise((ok) => {
    const p = spawn(process.execPath, [CLI, ...args], { env, cwd });
    let out = "", err = "";
    p.stdout.on("data", (d) => (out += d));
    p.stderr.on("data", (d) => (err += d));
    if (input !== undefined) p.stdin.end(input); else p.stdin.end();
    p.on("close", (code) => {
      let json = null;
      try { json = JSON.parse(out); } catch { /* human output */ }
      ok({ code, out, err, json });
    });
  });
}
