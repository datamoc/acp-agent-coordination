import assert from "node:assert/strict";
import { readFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { coord, localEnv, tmp } from "./helpers.mjs";

test("local mode: the full CLI round trip on a SQLite db, no server", () => {
  const dir = tmp();
  const env = localEnv(join(dir, "cli.db"));
  const run = (args, session, ok = true) => {
    const r = coord(["--json", ...args], { env: session ? { ...env, COORD_SESSION: session } : env, cwd: dir });
    if (ok) assert.equal(r.code, 0, r.out + r.err);
    return r.json;
  };
  const s = run(["whoami", "cli"]).session_id;
  const c = run(["claim", "src/"], s);
  assert.equal(c.scope_type, "tree");
  run(["post", "--kind", "info", "starting"], s);
  assert.equal(run(["inbox"], s)[0].body, "starting");
  const d = run(["doc", "create", "--kind", "plan", "--content", "step 1", "Plan"], s);
  run(["doc", "edit", d.document, "--base-revision", "1", "--content", "step 2"], s);
  assert.equal(run(["doc", "show", d.document]).revision, 2);
  const bad = run(["doc", "edit", d.document, "--base-revision", "1", "--content", "x"], s, false);
  assert.equal(bad.error, "revision_conflict");
  assert.deepEqual(run(["release", "--all"], s).released, [c.claim]);
  assert.equal(run(["context"], s).me.project, "ts-proj");
  const human = coord(["inbox"], { env: { ...env, COORD_SESSION: s }, cwd: dir });
  assert.match(human.out, /^#1 \[.+\] cli-01 info: starting$/m);             // same text as 0.2.1
});

test("a second whoami in the same checkout does not hijack a live session's file", () => {
  const dir = tmp();
  const env = localEnv(join(dir, "h.db"));
  const who = () => coord(["--json", "whoami", "cli"], { env, cwd: dir });
  const file = join(dir, ".coord-session");
  rmSync(file, { force: true });
  const first = who().json.session_id;
  assert.equal(readFileSync(file, "utf8").trim(), first);
  const second = who();
  assert.ok(second.json.warning, "expected a warning");
  assert.equal(readFileSync(file, "utf8").trim(), first);
  coord(["end"], { env: { ...env, COORD_SESSION: first }, cwd: dir });
  const third = who().json.session_id;
  assert.equal(readFileSync(file, "utf8").trim(), third);
});

test("local mode without the bridge says how to fix it", () => {
  const r = coord(["presence"], { env: localEnv(join(tmp(), "x.db"), { COORD_LOCAL: "coord-local-does-not-exist" }) });
  assert.equal(r.code, 1);
  assert.match(r.err, /local_unavailable.*COORD_SERVER/s);
});

test("consensus through the CLI: participants, rule, computed result, visible reasons", () => {
  const dir = tmp();
  const env = localEnv(join(dir, "c.db"));
  const as = (sid) => ({ env: { ...env, COORD_SESSION: sid }, cwd: dir });
  const who = (f) => coord(["--json", "whoami", f], { env: { ...env, COORD_SESSION: "x" }, cwd: dir }).json.session_id;
  const [lead, rev, ops] = [who("lead"), who("rev"), who("ops")];
  const d = coord(["--json", "discuss", "Which lock strategy?", "--with", "rev-01,ops-01"], as(lead)).json;
  assert.deepEqual(d.participants, ["lead-01", "rev-01", "ops-01"]);
  assert.equal(d.rule, "unanimous");
  const p = coord(["--json", "propose", d.discussion, "BEGIN IMMEDIATE everywhere"], as(lead)).json.proposal;
  coord(["react", p, "support"], as(rev));
  coord(["react", p, "object", "hurts read latency"], as(ops));
  const view = coord(["discussion", d.discussion], as(lead)).out;
  assert.match(view, /rule: unanimous, quorum 2, participants: lead-01, rev-01, ops-01/);
  assert.match(view, /consensus: no - lead-01 support \(implied\), rev-01 support, ops-01 object; not unanimous: ops-01 did not agree/);
  const refused = coord(["decide", d.discussion, "Use BEGIN IMMEDIATE", "--proposal", p], as(lead));
  assert.equal(refused.code, 1);                                                          // objections block it
  assert.match(refused.err, /no_consensus.*ops-01 did not agree.*--no-consensus/s);
  const dec = coord(["decide", d.discussion, "Use BEGIN IMMEDIATE", "--proposal", p, "--no-consensus", "release blocker"], as(lead));
  assert.equal(dec.code, 0, dec.err);
  assert.match(dec.out, /consensus: no\n  why not: not unanimous: ops-01 did not agree/);   // not "yes" as in 0.2.1
  const doc = coord(["--json", "discussion", d.discussion], as(lead)).json;
  assert.equal(doc.consensus, false);
  const content = coord(["--json", "doc", "show", doc.decision_document], as(lead)).json.content;
  assert.match(content, /consensus: no \(rule: unanimous, quorum: 2\)/);
  assert.match(content, /ops-01: object/);
  assert.match(content, /reason: release blocker/);
  assert.equal(coord(["discuss", "x", "--rule", "dictator"], as(lead)).code, 2);          // usage error
});

test("strategy and routines through the CLI", () => {
  const dir = tmp();
  const env = localEnv(join(dir, "r.db"));
  const run = (args, session, ok = true) => {
    const r = coord(["--json", ...args], { env: session ? { ...env, COORD_SESSION: session } : env, cwd: dir });
    if (ok) assert.equal(r.code, 0, r.out + r.err);
    return r.json;
  };
  const s = run(["whoami", "cli"]).session_id;
  run(["memory", "add", "strategy", "Fidelity", "--content", "v3.3.8 first"], s);
  assert.equal(run(["context"], s).strategy[0].content, "v3.3.8 first");
  assert.equal(run(["strategy"])[0].title, "Fidelity");
  const r = run(["routine", "create", "Security review", "--every", "1d", "--on-commit", "--path", "src/",
                 "--instructions", "npm audit; grep for secrets"], s).routine;
  assert.deepEqual(run(["poll"], s).routines.map((x) => x.routine), [r]);
  const start = coord(["routine", "start", r], { env: { ...env, COORD_SESSION: s }, cwd: dir });
  assert.match(start.out, /is yours until .+: Security review\n\nnpm audit; grep for secrets/);
  assert.equal(run(["routine", "done", r, "2 advisories", "--outcome", "issues"], s).outcome, "issues");
  assert.deepEqual(run(["routines", "--due"]), []);
  const list = coord(["routines"], { env, cwd: dir });
  assert.match(list.out, new RegExp(`^${r} \[next .+\] Security review \(every 1d \+ on commit \(src\)\) \| last issues by cli-01 .+: 2 advisories$`, "m"));
  run(["routine", "pause", r], s);
  assert.equal(run(["routine", "show", r]).status, "paused");
});
