import { execFileSync } from "node:child_process";
import { realpathSync } from "node:fs";
import { isAbsolute, relative, resolve, sep } from "node:path";
export function git(...args) {
    try {
        return execFileSync("git", args, { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
    }
    catch {
        return "";
    }
}
export function repoRoot() {
    return git("rev-parse", "--show-toplevel") || process.cwd();
}
/** git remote URL -> canonical key like github.com/org/repo (same rules as coordination/scopes.py). */
export function canonicalProject(remoteUrl) {
    let u = (remoteUrl ?? "").trim();
    if (!u)
        return "default";
    u = u.replace(/^[a-z+]+:\/\//, "");
    u = u.replace(/^[^@/]+@/, "");
    u = u.replace(/^([^/:]+):(?!\d)/, "$1/");
    u = u.replace(/^([^/:]+):\d+\//, "$1/"); // ssh://host:2222/... == https://host/...
    u = u.replace(/\.git\/?$/, "").replace(/\/+$/, "");
    return u.toLowerCase();
}
export function detectProject(env = process.env) {
    return env.COORD_PROJECT || canonicalProject(git("remote", "get-url", "origin"));
}
const real = (p) => {
    try {
        return realpathSync(p);
    }
    catch {
        return resolve(p);
    }
};
/** A user-given path made repo-relative (absolute paths inside the repo are OK); a trailing / is kept. */
export function rel(path) {
    if (!isAbsolute(path))
        return path;
    const r = relative(real(repoRoot()), real(path));
    if (r.startsWith("..") || isAbsolute(r))
        return path;
    return r.split(sep).join("/") + (/[/\\]$/.test(path) ? "/" : "");
}
