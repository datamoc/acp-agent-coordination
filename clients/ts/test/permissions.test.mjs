// Slice 0 (project permissions) end to end: the CLI against a real Python server.
import assert from "node:assert";
import { join } from "node:path";
import { test } from "node:test";
import { cleanEnv, coord, startServer, tmp } from "./helpers.mjs";

const run = (args, env) => coord(["--json", ...args], { env });

test("project permissions end to end: roster, roles, refusal (slice 0)", async () => {
  // its own database: the default would be the repo's coord2.db, shared with every other run
  const srv = await startServer(["--db", join(tmp(), "permissions.db")]);
  const base = () => cleanEnv({ COORD_SERVER: srv.url, COORD_PROJECT: "perm" });
  try {
    const own = run(["whoami", "own", "--project", "perm"], base());
    const out = run(["whoami", "out", "--project", "perm"], base());
    assert.ok(own.json?.session_id && out.json?.session_id, "both sessions taken");
    const nOwn = own.json.name, nOut = out.json.name;              // own-01, out-01 in a fresh db
    const eOwn = cleanEnv({ COORD_SERVER: srv.url, COORD_PROJECT: "perm", COORD_SESSION: own.json.session_id });
    const eOut = cleanEnv({ COORD_SERVER: srv.url, COORD_PROJECT: "perm", COORD_SESSION: out.json.session_id });

    // open: an empty roster - and the first member of an open project can only be yourself
    assert.deepEqual(run(["members"], eOwn).json, { project: "perm", restricted: false, members: [] });
    const hijack = run(["member", "set", nOut, "--role", "admin"], eOwn);
    assert.equal(hijack.code, 1);
    assert.equal(hijack.json.error, "forbidden");
    assert.equal(run(["member", "set", nOwn, "--role", "admin"], eOwn).json.restricted, true);

    // a non-member: refused a write, a read, even the roster
    const denied = run(["post", "nope"], eOut);
    assert.equal(denied.code, 1);
    assert.equal(denied.json.error, "forbidden");
    assert.equal(denied.json.message.includes("not a member"), true, "the error says who to ask");
    assert.equal(run(["members"], eOut).json.error, "forbidden");

    // a viewer reads but does not write
    const v = run(["whoami", "view", "--project", "perm"], base());
    const eView = cleanEnv({ COORD_SERVER: srv.url, COORD_PROJECT: "perm", COORD_SESSION: v.json.session_id });
    assert.equal(run(["member", "set", v.json.name, "--role", "viewer"], eOwn).json.role, "viewer");
    assert.equal(run(["tasks"], eView).code, 0);
    assert.equal(run(["post", "cannot"], eView).json.error, "forbidden");

    const roster = run(["members"], eOwn).json;
    assert.equal(roster.restricted, true);
    assert.deepEqual(roster.members.map((m) => `${m.name}:${m.role}`).sort(),
                     [`${nOwn}:admin`, `${v.json.name}:viewer`].sort());

    // removing every member reopens the project
    run(["member", "remove", v.json.name], eOwn);
    assert.equal(run(["member", "remove", nOwn], eOwn).json.restricted, false);
    assert.equal(run(["post", "free again"], eOut).code, 0);
  } finally {
    await srv.stop();
  }
});
