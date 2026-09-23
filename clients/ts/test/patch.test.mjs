import assert from "node:assert/strict";
import { join } from "node:path";
import { test } from "node:test";
import { unifiedDiff } from "../dist/diff.js";
import { coord, localEnv, py, tmp } from "./helpers.mjs";

// deterministic PRNG so failures reproduce
let seed = 42;
const rnd = () => ((seed = (seed * 1103515245 + 12345) % 2 ** 31) / 2 ** 31);
const pick = (xs) => xs[Math.floor(rnd() * xs.length)];

test("TS unifiedDiff output is applied exactly by the server's patch engine (1500 random pairs)", () => {
  const words = ["alpha", "beta", "", "## Title", "- item", "--- rule", "+ plus", "@@ x", "\\ back", "crlf\r"];
  const cases = [];
  for (let n = 0; n < 1500; n++) {
    const a = Array.from({ length: Math.floor(rnd() * 25) }, () => pick(words));
    const b = [...a];
    for (let k = 1 + Math.floor(rnd() * 4); k > 0; k--) {
      const r = rnd();
      if (r < 0.4 && b.length) b.splice(Math.floor(rnd() * b.length), 1);
      else if (r < 0.8) b.splice(Math.floor(rnd() * (b.length + 1)), 0, pick(words));
      else if (b.length) b[Math.floor(rnd() * b.length)] += "!";
    }
    const at = a.join("\n") + (a.length && rnd() < 0.7 ? "\n" : "");
    const bt = b.join("\n") + (b.length && rnd() < 0.7 ? "\n" : "");
    if (at !== bt) cases.push({ a: at, b: bt, patch: unifiedDiff(at, bt) });
  }
  const script = "import json,sys\nfrom coordination import textpatch as tp\n"
    + "print(json.dumps([tp.apply(c['a'], c['patch'])[0] == c['b'] for c in json.load(sys.stdin)]))";
  const p = py(["-c", script], undefined, JSON.stringify(cases));
  assert.equal(p.status, 0, p.stderr);
  const ok = JSON.parse(p.stdout);
  const bad = ok.findIndex((x) => !x);
  assert.equal(bad, -1, bad >= 0 ? JSON.stringify(cases[bad]) : "");
  assert.ok(cases.length > 1000);
  assert.equal(unifiedDiff("same\n", "same\n"), "");
});

test("doc patch --from: two agents edit one document at once, the edits merge", async () => {
  const dir = tmp();
  const env = localEnv(join(dir, "p.db"));
  const as = (sid) => ({ env: { ...env, COORD_SESSION: sid }, cwd: dir });
  const who = (f) => coord(["--json", "whoami", f], { env: { ...env, COORD_SESSION: "x" }, cwd: dir }).json.session_id;
  const [a, b] = [who("a"), who("b")];
  const body = [1, 2, 3, 4, 5].map((i) => `## Section ${i}\n` + [0, 1, 2, 3, 4, 5, 6].map((j) => `line ${i}.${j}`).join("\n") + "\n").join("\n");
  const doc = coord(["--json", "doc", "create", "Plan", "--content", body], as(a)).json.document;
  const { writeFileSync } = await import("node:fs");
  writeFileSync(join(dir, "a.md"), body.replace("line 1.3", "line 1.3 (a)"));        // both start from r1
  writeFileSync(join(dir, "b.md"), body.replace("line 5.2", "line 5.2 (b)"));
  const rb = coord(["--json", "doc", "patch", doc, "--base-revision", "1", "--from", "b.md"], as(b)).json;
  assert.equal(rb.merged, false);
  const ra = coord(["--json", "doc", "patch", doc, "--base-revision", "1", "--from", "a.md"], as(a)).json;
  assert.equal(ra.merged, true);
  assert.equal(ra.revision, 3);
  const final = coord(["--json", "doc", "show", doc], as(a)).json.content;
  assert.match(final, /line 1\.3 \(a\)/);
  assert.match(final, /line 5\.2 \(b\)/);
  writeFileSync(join(dir, "c.md"), body.replace("line 5.2", "line 5.2 (c)"));        // overlaps b's edit
  const clash = coord(["--json", "doc", "patch", doc, "--base-revision", "1", "--from", "c.md"], as(a));
  assert.equal(clash.code, 1);
  assert.equal(clash.json.error, "revision_conflict");
  assert.equal(clash.json.data.current_revision, 3);
  const diff = unifiedDiff(final, final.replace("line 3.0", "line 3.0 (stdin)"));    // a raw diff on stdin
  const viaStdin = coord(["--json", "doc", "patch", doc, "--base-revision", "3"], { ...as(a), input: diff });
  assert.equal(viaStdin.json.revision, 4, viaStdin.err);
  writeFileSync(join(dir, "same.md"), coord(["--json", "doc", "show", doc], as(a)).json.content);
  const same = coord(["doc", "patch", doc, "--base-revision", "4", "--from", "same.md"], as(a));
  assert.match(same.err, /no_change/);
});
