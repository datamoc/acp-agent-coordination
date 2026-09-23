import assert from "node:assert/strict";
import { readFileSync, statSync } from "node:fs";
import http from "node:http";
import { join } from "node:path";
import { test } from "node:test";
import { CoordClient } from "../dist/client.js";
import { TokenProvider } from "../dist/oidc.js";
import { RemoteTransport } from "../dist/transport.js";
import { cleanEnv, coordAsync as coord, startServer, tmp } from "./helpers.mjs";

/** Just enough of a Keycloak realm: discovery, token (client credentials, refresh with rotation,
 *  device code), device authorization, introspection, revocation. */
async function fakeKeycloak() {
  const kc = { active: new Set(), refresh: new Set(), requests: [], n: 0, pending: new Map() };
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (d) => (body += d));
    req.on("end", () => {
      const reply = (code, obj) => { res.writeHead(code, { "Content-Type": "application/json" }); res.end(JSON.stringify(obj)); };
      if (req.method === "GET") {
        return reply(200, { issuer: kc.realm, token_endpoint: kc.realm + "/token", device_authorization_endpoint: kc.realm + "/device",
                            introspection_endpoint: kc.realm + "/introspect" });
      }
      const f = Object.fromEntries(new URLSearchParams(body));
      const path = req.url.split("/").pop();
      if (path === "introspect") {
        const ok = kc.active.has(f.token);
        return reply(200, ok ? { active: true, sub: "agent", roles: ["coord:*:contributor"] } : { active: false });
      }
      if (path === "device") {
        kc.pending.set("dc1", 1);                                   // one "authorization_pending" first
        return reply(200, { device_code: "dc1", user_code: "WDJB-MJHT", interval: 0, expires_in: 60, verification_uri: kc.realm + "/device-ui" });
      }
      const g = f.grant_type;
      kc.requests.push(g);
      if (g === "client_credentials" && f.client_secret !== "s3cret") return reply(401, { error: "unauthorized_client", error_description: "bad secret" });
      if (g === "refresh_token") {
        if (!kc.refresh.has(f.refresh_token)) return reply(400, { error: "invalid_grant", error_description: "Session not active" });
        kc.refresh.delete(f.refresh_token);                         // rotation: the old one is spent
      }
      if (g.endsWith("device_code") && kc.pending.get(f.device_code) > 0) {
        kc.pending.set(f.device_code, kc.pending.get(f.device_code) - 1);
        return reply(400, { error: "authorization_pending" });
      }
      kc.n += 1;
      const tok = { access_token: `at${kc.n}`, expires_in: 60, token_type: "Bearer" };
      kc.active.add(tok.access_token);
      if (g !== "client_credentials") { tok.refresh_token = `rt${kc.n}`; kc.refresh.add(tok.refresh_token); }
      reply(200, tok);
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  kc.realm = `http://127.0.0.1:${server.address().port}/realms/corp`;
  kc.close = () => new Promise((r) => server.close(r));
  return kc;
}

test("Keycloak: tokens are fetched, cached, refreshed, recovered after revocation", async (t) => {
  const kc = await fakeKeycloak();
  t.after(kc.close);
  const srv = await startServer(["--oidc-introspect-url", kc.realm + "/introspect", "--oidc-client-id", "coord", "--oidc-cache-seconds", "0"],
                                cleanEnv({ COORD_DB: join(tmp(), "srv.db"), COORD_OIDC_SECRET: "srv" }));
  t.after(srv.stop);
  let clock = Date.now() / 1000;
  const state = tmp();
  const svc = (secret = "s3cret", dir = state) =>
    new TokenProvider({ clientId: "agent-svc", issuer: kc.realm, clientSecret: secret, stateDir: dir, clock: () => clock });
  const client = (tp) => new CoordClient(new RemoteTransport(srv.url, { token: tp }));

  // service account (client credentials): no human involved
  const tp = svc();
  const cl = client(tp);
  const sid = (await cl.call("whoami", { family: "svc", project: "kc" })).session_id;
  await cl.call("post", { session: sid, body: "hello from a service account" });
  assert.deepEqual(kc.requests, ["client_credentials"]);
  if (process.platform !== "win32") assert.equal(statSync(tp.stateFile).mode & 0o777, 0o600);
  await client(svc()).call("presence", {});                        // next process: cached token
  assert.deepEqual(kc.requests, ["client_credentials"]);
  clock += 61;                                                     // the fixed-token bug: expired
  await cl.call("heartbeat", { session: sid, status: "still here" });
  assert.equal(kc.requests.length, 2);
  kc.active.clear();                                               // revoked server-side
  await cl.call("heartbeat", { session: sid, status: "after revocation" });   // 401 -> new token -> retry
  assert.equal(kc.requests.length, 3);
  await assert.rejects(client(svc("nope", tmp())).call("presence", {}), /unauthenticated|refused/);

  // a person (device login): `coord login` once, then refresh tokens do the rest
  const tp2 = new TokenProvider({ clientId: "agent-cli", issuer: kc.realm, stateDir: tmp(), clock: () => clock });
  await assert.rejects(client(tp2).call("presence", {}), /coord login/);
  const shown = [];
  assert.equal((await tp2.login((m) => shown.push(m), async () => {})).logged_in, true);
  assert.match(shown[0], /WDJB-MJHT/);
  const cl2 = client(tp2);
  const sid2 = (await cl2.call("whoami", { family: "human", project: "kc" })).session_id;
  const rtBefore = JSON.parse(readFileSync(tp2.stateFile, "utf8")).refresh_token;
  clock += 61;
  await cl2.call("heartbeat", { session: sid2, status: "refreshed" });        // refresh grant, rotated
  assert.equal(kc.requests.at(-1), "refresh_token");
  assert.notEqual(JSON.parse(readFileSync(tp2.stateFile, "utf8")).refresh_token, rtBefore);
  kc.refresh.clear();                                              // SSO session ended
  clock += 61;
  await assert.rejects(cl2.call("presence", {}), /coord login/);
  assert.deepEqual(await tp2.logout(), { logged_out: false });     // state already cleared

  // the CLI, configured only through COORD_OIDC_* (e.g. in ~/.config/coord/env)
  const env = cleanEnv({ XDG_CONFIG_HOME: tmp(), COORD_SERVER: srv.url, COORD_OIDC_ISSUER: kc.realm,
                         COORD_OIDC_CLIENT_ID: "agent-svc", COORD_OIDC_CLIENT_SECRET: "s3cret", COORD_PROJECT: "kc" });
  for (const args of [["whoami", "cli"], ["presence"]]) {
    const r = await coord(["--json", ...args], { env });
    assert.equal(r.code, 0, r.err);
  }
  // and `coord login` / `logout` through the CLI (device flow; the fake approves after one poll)
  const denv = { ...env, COORD_OIDC_CLIENT_ID: "agent-cli", COORD_OIDC_CLIENT_SECRET: "" };
  assert.match((await coord(["presence"], { env: denv })).err, /run `coord login`/);
  const login = await coord(["login"], { env: denv });
  assert.equal(login.code, 0, login.err);
  assert.match(login.err, /confirm code WDJB-MJHT/);
  assert.equal((await coord(["presence"], { env: denv })).code, 0);
  assert.equal((await coord(["--json", "logout"], { env: denv })).json.logged_out, true);
});
