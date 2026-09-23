import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { admin, cleanEnv, coord, startServer, tmp } from "./helpers.mjs";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

test("mTLS: enroll -> TS client with only its bundle -> renewal -> live revocation", async (t) => {
  const pki = join(tmp(), "pki");
  const home = tmp();                                            // XDG_CONFIG_HOME: where bundles go
  const adminEnv = cleanEnv({ XDG_CONFIG_HOME: home });
  admin(pki, ["init"], adminEnv);
  admin(pki, ["server-cert"], adminEnv);
  const srv = await startServer(["--pki", pki, "--renew-after-days", "0.00002"], cleanEnv({ COORD_DB: join(tmp(), "srv.db") }));
  t.after(srv.stop);
  const alice = admin(pki, ["enroll", "alice", "--url", srv.url], adminEnv);
  assert.equal(alice.default, true);                             // first identity -> default
  admin(pki, ["enroll", "bob", "--url", srv.url], adminEnv);

  const env = cleanEnv({ XDG_CONFIG_HOME: home, COORD_PROJECT: "mtls" });
  delete env.COORD_CONFIG;                                       // use the enrolled default identity
  const run = (args, extra = {}) => coord(["--json", ...args], { env: { ...env, ...extra } });

  const who = run(["whoami", "agent"]);
  assert.equal(who.code, 0, who.err);
  const sid = who.json.session_id;
  const crt = join(home, "coord", "alice", "agent.crt");
  const before = readFileSync(crt, "utf8");
  await sleep(2000);                                             // > renew-after (1.7 s)
  const renewed = run(["heartbeat", "busy"], { COORD_SESSION: sid });
  assert.equal(renewed.code, 0, renewed.err);
  assert.match(renewed.err, /certificate renewed by the server/);
  assert.notEqual(readFileSync(crt, "utf8"), before);
  assert.equal(run(["presence"], { COORD_SESSION: sid }).code, 0);           // works on the new cert

  const hijack = run(["post", "I am alice"], { COORD_SESSION: sid, COORD_IDENTITY: "bob" });
  assert.equal(hijack.json.error, "forbidden");                  // session bound to alice's cert

  admin(pki, ["revoke", "alice"], adminEnv);                     // no server restart
  const refused = run(["presence"]);
  assert.equal(refused.json.error, "unauthenticated");

  const nocert = coord(["presence"], { env: cleanEnv({ COORD_SERVER: srv.url, COORD_CA: join(home, "coord", "bob", "ca.crt") }) });
  assert.equal(nocert.code, 1);
  assert.match(nocert.err, /unreachable/);                       // TLS handshake refused
});
