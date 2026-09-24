import assert from "node:assert/strict";
import { join } from "node:path";
import { test } from "node:test";
import { CoordClient } from "../dist/client.js";
import { RemoteTransport } from "../dist/transport.js";
import { cleanEnv, startServer, tmp } from "./helpers.mjs";

test("follow: server-sent events from the real server, resumed after the last id", async () => {
  const srv = await startServer([], cleanEnv({ COORD_DB: join(tmp(), "sse.db") }));
  try {
    const t = new RemoteTransport(srv.url, { protocol: "call" });
    const c = new CoordClient(t);
    const me = await c.call("whoami", { family: "sse", project: "p" });
    await c.call("post", { session: me.session_id, body: "streamed" });
    const stop = new AbortController(), seen = [];
    const done = t.follow("p", 0, (e) => { seen.push(e); if (seen.some((x) => x.kind === "message.posted")) stop.abort(); }, stop.signal);
    await Promise.race([done, new Promise((_, no) => setTimeout(() => no(new Error("no event in 10 s")), 10_000))]);
    assert.deepEqual(seen.map((e) => e.kind).slice(0, 2), ["session.started", "message.posted"]);
    const again = new AbortController(), later = [];
    const first = seen[0].event;
    const resumed = t.follow("p", first, (e) => { later.push(e.event); again.abort(); }, again.signal);
    await Promise.race([resumed, new Promise((_, no) => setTimeout(() => no(new Error("no resume")), 10_000))]);
    assert.ok(later[0] > first, "resumes after the last id");
  } finally {
    await srv.stop();
  }
});
