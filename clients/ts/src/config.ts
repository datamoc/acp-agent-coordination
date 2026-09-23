import { readFileSync, realpathSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";

const CONFIG_PATHS = new Set(["COORD_CA", "COORD_CERT", "COORD_KEY", "COORD_DB"]);

export const expandUser = (p: string) => (p === "~" || p.startsWith("~/") || p.startsWith("~\\") ? join(homedir(), p.slice(1)) : p);

export function configHome(env: NodeJS.ProcessEnv = process.env): string {
  return join(env.XDG_CONFIG_HOME || join(homedir(), ".config"), "coord");
}

/** Agent CLIs recognized by a variable they set for the commands they run: [variable, identity]. */
const HOSTS: [string, string][] = [["MUSE_SESSION_ID", "muse"], ["OPENCODE", "opencode"]];

/** The identity named after the agent CLI running us, when one is enrolled (~/.config/coord/<cli>/env). */
export function hostIdentity(env: NodeJS.ProcessEnv = process.env): string | null {
  for (const [variable, identity] of HOSTS) {
    if (env[variable] && probe(join(configHome(env), identity, "env")) !== "missing") return identity;
  }
  return null;
}

export function configFile(env: NodeJS.ProcessEnv = process.env): string {
  if (env.COORD_CONFIG) return expandUser(env.COORD_CONFIG);
  const identity = env.COORD_IDENTITY || hostIdentity(env);
  if (identity) return join(configHome(env), identity, "env");
  return join(configHome(env), "env");
}

/**
 * Fill unset COORD_* variables from the identity's config file (written by `coord-admin enroll`):
 * $COORD_CONFIG, else ~/.config/coord/$COORD_IDENTITY/env, else the enrolled identity named after the
 * agent CLI running us (hostIdentity: muse, opencode), else ~/.config/coord/env - a symlink to
 * an identity, or a `COORD_IDENTITY=<name>` pointer where symlinks are not allowed. KEY=value lines,
 * # comments; the environment always wins; relative paths resolve against the file's real directory.
 */
export function loadConfig(env: NodeJS.ProcessEnv = process.env): string | null {
  let f = configFile(env);
  const state = probe(f);
  if (state === "denied") {
    throw new ConfigError(`cannot read ${f} (permission denied). An agent sandbox that runs commands as another `
      + "account (Codex on Windows) needs read access to the identity folder - see the README, `Codex`");
  }
  if (state === "missing") {
    if (env.COORD_IDENTITY && !env.COORD_CONFIG) {
      throw new ConfigError(`no identity '${env.COORD_IDENTITY}' (${f} missing) - ask the administrator for \`coord-admin enroll <client-name>\``);
    }
    return null;
  }
  try {
    f = realpathSync(f);
  } catch {
    f = resolve(f);   // a sandbox may read the file but not stat its parents: keep the path as given
  }
  const lines = readFileSync(f, "utf8").split(/\r?\n/);
  const pointer = lines.filter((l) => l.startsWith("COORD_IDENTITY=")).map((l) => l.slice("COORD_IDENTITY=".length).trim());
  if (pointer.length && !env.COORD_IDENTITY && !env.COORD_CONFIG) {
    env.COORD_IDENTITY = pointer[0];
    return loadConfig(env);
  }
  for (const raw of lines) {
    const line = raw.trim().replace(/^export\s+/, "");
    const eq = line.indexOf("=");
    if (eq < 0) continue;
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim().replace(/^['"]|['"]$/g, "");
    if (!key.startsWith("COORD_") || key in env) continue;
    if (CONFIG_PATHS.has(key) && value) value = resolve(dirname(f), expandUser(value));
    env[key] = value;
  }
  return f;
}

function probe(f: string): "ok" | "missing" | "denied" {
  try {
    statSync(f);
    return "ok";
  } catch (e) {
    const code = (e as NodeJS.ErrnoException).code;
    return code === "EPERM" || code === "EACCES" ? "denied" : "missing";
  }
}

export class ConfigError extends Error {}
