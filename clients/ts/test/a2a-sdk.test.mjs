// Interop: the official A2A JS SDK (@a2a-js/sdk, A2A v1.0) as a client of the coord server.
import assert from "node:assert/strict";
import http from "node:http";
import { join } from "node:path";
import { test } from "node:test";
import { SendMessageRequest, TaskState } from "@a2a-js/sdk";
import { ClientFactory } from "@a2a-js/sdk/client";
import { cleanEnv, coordAsync, startServer, tmp } from "./helpers.mjs";

const send = (json) => SendMessageRequest.fromJSON(json);
const dataOf = (msg) => msg.parts.find((p) => p.content?.$case === "data").content.value;

async function webhook() {
  const got = [];
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (d) => (body += d));
    req.on("end", () => { got.push({ headers: req.headers, body: JSON.parse(body) }); res.writeHead(200).end(); });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  return { url: `http://127.0.0.1:${server.address().port}/hook`, got, close: () => new Promise((r) => server.close(r)) };
}

const waitFor = async (fn, ms = 5000) => {
  for (const end = Date.now() + ms; Date.now() < end; await new Promise((r) => setTimeout(r, 50))) if (fn()) return;
  throw new Error("timed out");
};

test("A2A SDK client: agent card, coord ops, delegation as A2A tasks, push notifications", async (t) => {
  const srv = await startServer([], cleanEnv({ COORD_DB: join(tmp(), "a2a.db") }));
  t.after(srv.stop);
  const hook = await webhook();
  t.after(hook.close);

  const client = await new ClientFactory().createFromUrl(srv.url);            // discovery via the card
  const whoami = async (family) => dataOf(await client.sendMessage(send({
    message: { messageId: `m-${family}`, role: "ROLE_USER", parts: [{ data: { op: "whoami", args: { family, project: "a2a-proj" } } }] },
  })));
  const lead = await whoami("lead");
  const worker = await whoami("worker");
  assert.equal(worker.name, "worker-01");

  // any coord op as a data part - and coord refusals come back as JSON-RPC errors
  const op = async (o, args) => dataOf(await client.sendMessage(send({ message: { messageId: `m-${o}-${Math.random()}`, role: "ROLE_USER",
                                                                                   parts: [{ data: { op: o, args } }] } })));
  const claim = await op("claim", { session: lead.session_id, scope: "src/parser/", tree: true });
  assert.match(claim.claim, /^C\d+$/);
  await assert.rejects(op("claim", { session: worker.session_id, scope: "src/parser/lexer.py" }), /overlaps/);

  // delegation in plain A2A: text + who should do it -> an A2A Task
  const task = await client.sendMessage(send({ message: {
    messageId: "m-delegate", role: "ROLE_USER", parts: [{ text: "Review the parser changes before merge" }],
    metadata: { coord: { session: lead.session_id, assign: "worker-01" } } } }));
  assert.equal(task.id, "T1");
  assert.equal(task.contextId, "a2a-proj");
  assert.equal(task.status.state, TaskState.TASK_STATE_SUBMITTED);           // assigned = offered, not yet accepted

  // the lead subscribes to it with a webhook instead of polling
  const cfg = await client.createTaskPushNotificationConfig({ tenant: "", id: "", taskId: "T1", url: hook.url, token: "tok-123", authentication: undefined });
  assert.equal(cfg.taskId, "T1");
  await assert.rejects(client.createTaskPushNotificationConfig({ tenant: "", id: "", taskId: "T1", url: "https://evil.example/x", token: "", authentication: undefined }),
                       /not allowed/);

  // the worker finishes it with the TS coord CLI (A2A under the hood) -> push to the webhook
  const env = cleanEnv({ COORD_SERVER: srv.url, COORD_PROJECT: "a2a-proj", COORD_SESSION: worker.session_id });
  const accepted = await coordAsync(["task", "accept", "T1"], { env });              // the worker agrees
  assert.equal(accepted.code, 0, accepted.err);
  assert.equal((await client.getTask({ tenant: "", id: "T1" })).status.state, TaskState.TASK_STATE_WORKING);
  const done = await coordAsync(["task", "done", "T1", "reviewed, looks good"], { env });
  assert.equal(done.code, 0, done.err);
  await waitFor(() => hook.got.some((g) => g.body.statusUpdate.status.state === "TASK_STATE_COMPLETED"));
  assert.equal(hook.got[0].body.statusUpdate.status.state, "TASK_STATE_WORKING");        // accept was pushed too
  const push = hook.got.find((g) => g.body.statusUpdate.status.state === "TASK_STATE_COMPLETED");
  assert.equal(push.headers["x-a2a-notification-token"], "tok-123");
  assert.equal(push.headers["content-type"], "application/a2a+json");
  assert.equal(push.body.statusUpdate.taskId, "T1");
  assert.equal(push.body.statusUpdate.status.state, "TASK_STATE_COMPLETED");

  const got = await client.getTask({ tenant: "", id: "T1" });
  assert.equal(got.status.state, TaskState.TASK_STATE_COMPLETED);
  assert.equal(got.status.message.parts[0].content.value, "reviewed, looks good");
  const listed = await client.listTasks({ tenant: "", contextId: "a2a-proj", status: TaskState.TASK_STATE_UNSPECIFIED, pageToken: "" });
  assert.deepEqual(listed.tasks.map((x) => x.id), ["T1"]);

  // an unassigned task is SUBMITTED; its creator cancels it through A2A
  const open = await client.sendMessage(send({ message: { messageId: "m-open", role: "ROLE_USER", parts: [{ text: "Update the changelog" }],
                                                          metadata: { coord: { session: lead.session_id } } } }));
  assert.equal(open.status.state, TaskState.TASK_STATE_SUBMITTED);
  const cancelled = await client.cancelTask({ tenant: "", id: open.id, metadata: { coord: { session: lead.session_id } } });
  assert.equal(cancelled.status.state, TaskState.TASK_STATE_CANCELED);
});
