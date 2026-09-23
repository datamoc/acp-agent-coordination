import { existsSync, readFileSync, realpathSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
const CONFIG_PATHS = new Set(["COORD_CA", "COORD_CERT", "COORD_KEY", "COORD_DB"]);
export const expandUser = (p) => (p === "~" || p.startsWith("~/") || p.startsWith("~\\") ? join(homedir(), p.slice(1)) : p);
export function configHome(env = process.env) {
    return join(env.XDG_CONFIG_HOME || join(homedir(), ".config"), "coord");
}
export function configFile(env = process.env) {
    if (env.COORD_CONFIG)
        return expandUser(env.COORD_CONFIG);
    if (env.COORD_IDENTITY)
        return join(configHome(env), env.COORD_IDENTITY, "env");
    return join(configHome(env), "env");
}
/**
 * Fill unset COORD_* variables from the identity's config file (written by `coord-admin enroll`):
 * $COORD_CONFIG, else ~/.config/coord/$COORD_IDENTITY/env, else ~/.config/coord/env - a symlink to
 * an identity, or a `COORD_IDENTITY=<name>` pointer where symlinks are not allowed. KEY=value lines,
 * # comments; the environment always wins; relative paths resolve against the file's real directory.
 */
export function loadConfig(env = process.env) {
    let f = configFile(env);
    if (!existsSync(f)) {
        if (env.COORD_IDENTITY && !env.COORD_CONFIG) {
            throw new ConfigError(`no identity '${env.COORD_IDENTITY}' (${f} missing) - ask the administrator for \`coord-admin enroll <client-name>\``);
        }
        return null;
    }
    f = realpathSync(f);
    const lines = readFileSync(f, "utf8").split(/\r?\n/);
    const pointer = lines.filter((l) => l.startsWith("COORD_IDENTITY=")).map((l) => l.slice("COORD_IDENTITY=".length).trim());
    if (pointer.length && !env.COORD_IDENTITY && !env.COORD_CONFIG) {
        env.COORD_IDENTITY = pointer[0];
        return loadConfig(env);
    }
    for (const raw of lines) {
        const line = raw.trim().replace(/^export\s+/, "");
        const eq = line.indexOf("=");
        if (eq < 0)
            continue;
        const key = line.slice(0, eq).trim();
        let value = line.slice(eq + 1).trim().replace(/^['"]|['"]$/g, "");
        if (!key.startsWith("COORD_") || key in env)
            continue;
        if (CONFIG_PATHS.has(key) && value)
            value = resolve(dirname(f), expandUser(value));
        env[key] = value;
    }
    return f;
}
export class ConfigError extends Error {
}
