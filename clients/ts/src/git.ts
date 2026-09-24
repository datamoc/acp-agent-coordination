import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, realpathSync, statSync } from "node:fs";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";

export function git(...args: string[]): string {
  try {
    return execFileSync("git", args, { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
  } catch {
    return "";
  }
}

/** The checkout containing `from`, found without running git (git can refuse it: "dubious ownership"
 *  when an agent's sandbox account is not the repository's owner). */
export function findCheckout(from: string = process.cwd()): { root: string; gitDir: string } | null {
  for (let dir = resolve(from); ; dir = dirname(dir)) {
    const dotGit = join(dir, ".git");
    if (existsSync(dotGit)) {
      try {
        if (statSync(dotGit).isDirectory()) return { root: dir, gitDir: dotGit };
        const m = /^gitdir:\s*(.+)$/m.exec(readFileSync(dotGit, "utf8"));      // a worktree or submodule
        if (m) return { root: dir, gitDir: resolve(dir, m[1].trim()) };
      } catch { /* unreadable: keep looking up */ }
    }
    if (dirname(dir) === dir) return null;
  }
}

/** remote.origin.url read from the checkout's config file (a worktree's lives in its common dir). */
export function originFromConfig(from: string = process.cwd()): string {
  const c = findCheckout(from);
  if (!c) return "";
  const common = existsSync(join(c.gitDir, "commondir"))
    ? resolve(c.gitDir, readFileSync(join(c.gitDir, "commondir"), "utf8").trim()) : c.gitDir;
  try {
    const cfg = readFileSync(join(common, "config"), "utf8");
    const section = /\[remote\s+"origin"\]([\s\S]*?)(?=^\s*\[|$(?![\s\S]))/m.exec(cfg);
    const url = section && /^\s*url\s*=\s*(.+)$/m.exec(section[1]);
    return url ? url[1].trim() : "";
  } catch {
    return "";
  }
}

export function repoRoot(): string {
  return git("rev-parse", "--show-toplevel") || findCheckout()?.root || process.cwd();
}

/** git remote URL -> canonical key like github.com/org/repo (same rules as coordination/scopes.py). */
export function canonicalProject(remoteUrl: string): string {
  let u = (remoteUrl ?? "").trim();
  if (!u) return "default";
  u = u.replace(/^[a-z+]+:\/\//, "");
  u = u.replace(/^[^@/]+@/, "");
  u = u.replace(/^([^/:]+):(?!\d)/, "$1/");
  u = u.replace(/^([^/:]+):\d+\//, "$1/"); // ssh://host:2222/... == https://host/...
  u = u.replace(/\.git\/?$/, "").replace(/\/+$/, "");
  return u.toLowerCase();
}

export function detectProject(env: NodeJS.ProcessEnv = process.env): string {
  return env.COORD_PROJECT || canonicalProject(git("remote", "get-url", "origin") || originFromConfig());
}

const real = (p: string) => {
  try {
    return realpathSync(p);
  } catch {
    return resolve(p);
  }
};

/** A user-given path made repo-relative (absolute paths inside the repo are OK); a trailing / is kept. */
export function rel(path: string): string {
  if (!isAbsolute(path)) return path;
  const r = relative(real(repoRoot()), real(path));
  if (r.startsWith("..") || isAbsolute(r)) return path;
  return r.split(sep).join("/") + (/[/\\]$/.test(path) ? "/" : "");
}
