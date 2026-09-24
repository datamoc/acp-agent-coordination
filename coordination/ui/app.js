// coord UI - served by `coord-server --ui`. Everything agents write is untrusted: it only ever
// reaches the page as text (el(), textContent), never as HTML.
"use strict";

const $ = (id) => document.getElementById(id);
let state = null, project = null, me = null, replyTo = null, stream = null, pending = null;

function el(tag, props = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) n.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) n.append(k instanceof Node ? k : String(k));
  return n;
}
const fill = (node, items, empty = "—") => node.replaceChildren(...(items.length ? items : [el("li", { class: "muted" }, empty)]));
const ago = (iso) => {
  if (!iso) return "";
  const s = (Date.now() - Date.parse(iso)) / 1000;
  return s < 60 ? "now" : s < 3600 ? `${Math.round(s / 60)} min` : s < 86400 ? `${Math.round(s / 3600)} h` : `${Math.round(s / 86400)} d`;
};

function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.add("hidden"), 4000);
}

async function call(op, args = {}) {
  const r = await fetch("/api/call", { method: "POST", headers: { "Content-Type": "application/json" },
                                       body: JSON.stringify({ op, args, project }) });
  const env = await r.json();
  if (!env.ok) throw new Error(`${env.error}: ${env.message ?? ""}`);
  return env.result;
}
const act = (fn) => async (ev) => {
  ev?.preventDefault?.();
  try { await fn(ev); await refresh(); } catch (e) { toast(e.message); }
};

// --- panels -------------------------------------------------------------
function renderSessions(list) {
  fill($("sessions"), list.map((s) => el("li", {}, el("b", {}, s.name), " ", el("span", { class: "muted small" }, s.status || ""),
    el("span", { class: "tag" }, ago(s.seen)))), "nobody live");
  const to = $("to"), assign = $("assign"), keep = [to.value, assign.value];
  to.replaceChildren(el("option", { value: "" }, "everyone"), ...list.map((s) => el("option", { value: s.name }, s.name)));
  assign.replaceChildren(el("option", { value: "" }, "open"), ...list.map((s) => el("option", { value: s.name }, s.name)));
  [to.value, assign.value] = keep;
}

function renderClaims(list) {
  fill($("claims"), list.map((c) => el("li", {}, el("span", { class: "id" }, c.claim), el("code", {}, c.scope), " ",
    el("span", { class: "muted small" }, `${c.owner} · until ${c.expires_at.slice(11, 16)}`), c.note ? el("div", { class: "small muted clamp", title: c.note }, c.note) : null)),
  "no active claim");
}

function renderStrategy(list) {
  $("strategy").replaceChildren(...(list.length ? list.map((m) => el("div", { class: "item" }, el("b", {}, m.title), m.content))
    : [el("p", { class: "muted small" }, "No strategy yet - agents add it with coord memory add strategy.")]));
}

function renderMessages(list) {
  const feed = $("messages");
  const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 40;
  fill(feed, list.map((m) => {
    const open = !m.resolved_at && (m.kind === "question" || m.kind === "warning");
    return el("li", { class: `kind-${m.kind}${m.from === me ? " mine" : ""}` },
      el("div", { class: "head" }, el("span", { class: "id" }, `#${m.id}`), el("span", { class: "from" }, m.from),
        m.to ? ` → ${m.to}` : "", " · ", el("span", { class: "kind" }, m.kind), m.claim ? ` · ${m.claim}` : "",
        m.reply_to ? ` · re #${m.reply_to}` : "", " · ", ago(m.at)),
      el("div", { class: "body" }, m.body),
      m.resolved_at ? el("div", { class: "small muted" }, `resolved by ${m.resolved_by}: ${m.resolution || ""}`) : null,
      el("div", { class: "actions" },
        el("button", { class: "ghost small", onclick: () => startReply(m) }, "Reply"),
        open ? el("button", { class: "ghost small", onclick: act(() => {
          const note = prompt(`Resolve #${m.id} - note:`, "");
          if (note !== null) return call("resolve", { message: m.id, resolution: note });
        }) }, "Resolve") : null));
  }), "no message yet");
  if (atBottom) feed.scrollTop = feed.scrollHeight;
}

const taskState = (t) => (t.status === "done" || t.status === "cancelled" ? "done" : t.blocked_by?.length ? "blocked" : t.status);

function renderTasks(list) {
  const live = list.filter((t) => t.status !== "done" && t.status !== "cancelled");
  fill($("tasks"), live.map((t) => el("li", { title: t.title }, el("div", { class: "clamp" }, el("span", { class: "id" }, t.task), t.title),
    el("span", { class: `tag${t.blocked_by?.length ? " due" : ""}` }, t.blocked_by?.length ? `blocked by ${t.blocked_by.join(", ")}` : t.status),
    t.assigned ? el("span", { class: "tag" }, t.assigned) : null,
    !t.blocked_by?.length && t.after?.length ? el("span", { class: "tag" }, `after ${t.after.join(", ")}`) : null)), "no open task");
  renderGraph(list.filter((t) => t.status !== "cancelled"));
}

// --- the task graph: columns by depth (longest chain of prerequisites), edges prerequisite -> task --
const SVG = "http://www.w3.org/2000/svg";
function svg(tag, attrs = {}, ...kids) {
  const n = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  for (const k of kids) n.append(k instanceof Node ? k : document.createTextNode(String(k)));
  return n;
}
function renderGraph(tasks) {
  const box = $("graph");
  const byId = new Map(tasks.map((t) => [t.task, t]));
  if (!tasks.some((t) => (t.after || []).some((a) => byId.has(a)))) {
    box.replaceChildren(el("p", { class: "muted small" }, tasks.length
      ? "No dependencies yet - link tasks with coord task link T3 --after T1 (or \"after\" below)." : "No task yet."));
    return;
  }
  const depth = new Map();
  const d = (id, seen = new Set()) => {
    if (depth.has(id)) return depth.get(id);
    if (seen.has(id)) return 0;
    seen.add(id);
    const v = Math.max(0, ...(byId.get(id).after || []).filter((a) => byId.has(a)).map((a) => d(a, seen) + 1));
    depth.set(id, v);
    return v;
  };
  tasks.forEach((t) => d(t.task));
  const cols = [];
  for (const t of tasks) (cols[depth.get(t.task)] ??= []).push(t);
  const row = new Map();                     // each column ordered by its prerequisites' mean row: fewer crossings
  const hasNext = new Set(tasks.flatMap((t) => (t.after || []).filter((a) => byId.has(a))));
  cols[0]?.sort((a, b) => Number(hasNext.has(b.task)) - Number(hasNext.has(a.task)));   // roots that lead somewhere first
  cols.forEach((col, c) => {
    if (c) {
      const mean = (t) => { const r = (t.after || []).filter((a) => row.has(a)).map((a) => row.get(a));
                            return r.length ? r.reduce((x, y) => x + y, 0) / r.length : Infinity; };
      col.sort((a, b) => mean(a) - mean(b));
    }
    col.forEach((t, r) => row.set(t.task, r));
  });
  const W = 230, H = 46, GX = 70, GY = 18, P = 12;
  const pos = new Map();
  cols.forEach((col, c) => col.forEach((t, r) => pos.set(t.task, { x: P + c * (W + GX), y: P + r * (H + GY) })));
  const width = P * 2 + cols.length * W + (cols.length - 1) * GX;
  const height = P * 2 + Math.max(...cols.map((c) => c.length)) * (H + GY) - GY;
  const root = svg("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "task dependency graph" });
  root.append(svg("defs", {}, svg("marker", { id: "arrow", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7",
                                               markerHeight: "7", orient: "auto-start-reverse" },
                                  svg("path", { d: "M0,0 L10,5 L0,10 z", fill: "currentColor", class: "edge-head" }))));
  for (const t of tasks) for (const a of t.after || []) {
    if (!byId.has(a)) continue;
    const s = pos.get(a), e = pos.get(t.task);
    const x1 = s.x + W, y1 = s.y + H / 2, x2 = e.x - 2, y2 = e.y + H / 2, mx = (x1 + x2) / 2;
    root.append(svg("path", { class: "edge", d: `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`, "marker-end": "url(#arrow)" }));
  }
  for (const t of tasks) {
    const { x, y } = pos.get(t.task);
    const title = t.title.length > 30 ? t.title.slice(0, 29) + "…" : t.title;
    const sub = t.blocked_by?.length ? `waits for ${t.blocked_by.join(", ")}` : t.assigned ? `${t.status} · ${t.assigned}` : t.status;
    root.append(svg("g", { class: `node ${taskState(t)}`, transform: `translate(${x},${y})` },
      svg("title", {}, `${t.task} ${t.title} (${t.status})`),
      svg("rect", { width: W, height: H, rx: 8 }),
      svg("text", { x: 10, y: 19 }, svg("tspan", { class: "tid" }, t.task + " "), title),
      svg("text", { x: 10, y: 36, class: "muted-text", opacity: ".75", "font-size": "11" }, sub)));
  }
  box.replaceChildren(root);
}

function renderAttention(msgs, tasks) {
  const open = msgs.filter((m) => !m.resolved_at && (m.kind === "question" || m.kind === "warning")).slice(-8).reverse();
  const offered = tasks.filter((t) => t.status === "offered");
  fill($("attention"), [
    ...open.map((m) => el("li", {}, el("span", { class: "id" }, `#${m.id}`), el("b", {}, m.from), ` ${m.kind}: `,
      el("span", { class: "small" }, m.body.length > 110 ? m.body.slice(0, 109) + "…" : m.body))),
    ...offered.map((t) => el("li", {}, el("span", { class: "id" }, t.task), `offered to ${t.assigned}: `, t.title)),
  ], "nothing waiting");
  return open.length + offered.length;
}

function renderDue(routines) {
  const due = routines.filter((r) => r.due);
  fill($("due"), due.map((r) => el("li", {}, el("span", { class: "id" }, r.routine), r.title, el("span", { class: "tag due" }, r.why))),
    "nothing due");
  return due.length;
}

const badge = (id, n) => { $(id).textContent = n ? String(n) : ""; };

// --- tabs -----------------------------------------------------------------
function showTab(name) {
  for (const b of document.querySelectorAll("#tabs [role=tab]")) b.setAttribute("aria-selected", String(b.dataset.tab === name));
  for (const v of document.querySelectorAll(".view")) v.classList.toggle("hidden", v.id !== `view-${name}`);
  try { localStorage.setItem("coord.tab", name); } catch { /* private window */ }
}
for (const b of document.querySelectorAll("#tabs [role=tab]")) b.addEventListener("click", () => showTab(b.dataset.tab));
showTab((() => { try { return localStorage.getItem("coord.tab") || "now"; } catch { return "now"; } })());

function renderRoutines(list) {
  fill($("routines"), list.map((r) => el("li", {}, el("span", { class: "id" }, r.routine), r.title,
    r.running ? el("span", { class: "tag" }, `running: ${r.running}`) : r.due ? el("span", { class: "tag due" }, `due: ${r.why}`)
      : el("span", { class: "tag" }, r.status !== "active" ? r.status : r.next_due ? `next ${r.next_due.slice(5, 16).replace("T", " ")}` : "on commit"),
    r.last_outcome && r.last_outcome !== "ok" ? el("span", { class: "tag bad" }, r.last_outcome) : null,
    r.last_result ? el("div", { class: "small muted" }, r.last_result) : null)), "no routine");
}

function renderDocs(list) {
  fill($("docs"), list.slice(-15).reverse().map((d) => el("li", {},
    el("a", { class: "link", onclick: act(async () => {
      const doc = await call("doc_show", { document: d.document });
      $("vtitle").textContent = `${doc.document} r${doc.revision} · ${doc.title}`;
      $("vbody").textContent = doc.content;
      $("viewer").showModal();
    }) }, el("span", { class: "id" }, d.document), d.title), el("span", { class: "tag" }, d.kind))), "no document");
}

async function renderDiscussions(list) {
  const details = await Promise.all(list.map((d) => call("discussion", { discussion: d.discussion })));
  const box = $("discussions");
  box.replaceChildren(...(details.length ? details.map((d) => el("div", { class: "disc" },
    el("div", {}, el("span", { class: "id" }, d.discussion), el("b", {}, d.topic)),
    el("div", { class: "small muted" }, `${d.rule}, by ${d.created_by}`),
    ...d.proposals.filter((p) => p.status !== "superseded").map((p) => {
      const c = p.consensus || {};
      const stance = (s) => act(() => {
        const why = s === "object" || s === "support-with-reservation" ? prompt(`${s} - why?`, "") : "";
        if (why !== null) return call("react", { proposal: p.proposal, stance: s, comment: why });
      });
      return el("div", { class: "prop" },
        el("div", {}, el("span", { class: "id" }, p.proposal), p.body, " ", el("span", { class: "muted small" }, `- ${p.author}`)),
        c.met ? el("div", { class: "met" }, "consensus reached") : el("div", { class: "why" }, (c.why || []).join("; ")),
        ...(c.reservations || []).map((x) => el("div", { class: "small muted" }, `reservation from ${x.name}: ${x.comment || "-"}`)),
        el("div", { class: "row" },
          el("button", { class: "ghost small", onclick: stance("support") }, "Support"),
          el("button", { class: "ghost small", onclick: stance("support-with-reservation") }, "Reservation"),
          el("button", { class: "ghost small", onclick: stance("object") }, "Object"),
          c.met ? el("button", { class: "small", onclick: act(() => {
            const text = prompt("Decision:", p.body);
            if (text) return call("decide", { discussion: d.discussion, decision: text, proposal: p.proposal });
          }) }, "Decide") : null));
    }))) : [el("p", { class: "muted small" }, "no open discussion")]));
}

// --- loading --------------------------------------------------------------
async function refresh() {
  if (!project) return;
  try {
    const [pres, locks, strat, msgs, tasks, discs, routines, docs] = await Promise.all([
      call("presence", { project }), call("locks", { project }), call("memory", { project, kind: "strategy" }),
      call("inbox", { project, limit: 80 }), call("tasks", { project }), call("discussions", { project }),
      call("routines", { project }), call("docs", { project })]);
    renderSessions(pres); renderClaims(locks); renderStrategy(strat); renderMessages(msgs);
    renderTasks(tasks); renderRoutines(routines); renderDocs(docs);
    await renderDiscussions(discs);
    badge("b-now", renderAttention(msgs, tasks));
    badge("b-tasks", tasks.filter((t) => t.status === "offered" || (t.blocked_by?.length && t.status !== "done")).length);
    badge("b-discussions", discs.length);
    badge("b-routines", renderDue(routines));
  } catch (e) { toast(e.message); }
}

function listen() {
  stream?.close();
  stream = new EventSource(`/api/events?project=${encodeURIComponent(project)}&after=${state.lastEvent ?? 0}`);
  stream.onopen = () => $("live").classList.add("on");
  stream.onerror = () => $("live").classList.remove("on");
  stream.onmessage = (ev) => {
    const e = JSON.parse(ev.data);
    state.lastEvent = e.event;
    if (e.kind === "message.posted" && document.hidden && Notification?.permission === "granted") {
      new Notification("coord", { body: `new ${e.payload?.kind ?? "message"} in ${project}` });
    }
    clearTimeout(pending);
    pending = setTimeout(refresh, 250);        // bursts of events: one refresh
  };
}

function startReply(m) {
  replyTo = m.id;
  $("replying").textContent = `Replying to #${m.id} (${m.from}) - press Esc to cancel`;
  $("replying").classList.remove("hidden");
  $("body").focus();
}

$("compose").addEventListener("submit", act(async () => {
  const body = $("body").value.trim();
  if (!body) return;
  if (replyTo) await call("reply", { message: replyTo, body, kind: $("kind").value });
  else await call("post", { body, kind: $("kind").value, to: $("to").value || null });
  $("body").value = ""; replyTo = null; $("replying").classList.add("hidden");
}));
$("body").addEventListener("keydown", (e) => { if (e.key === "Escape") { replyTo = null; $("replying").classList.add("hidden"); } });
$("newtask").addEventListener("submit", act(async () => {
  const title = $("tasktitle").value.trim();
  if (!title) return;
  const after = $("taskafter").value.split(/[\s,]+/).filter(Boolean);
  await call("task_create", { title, assign: $("assign").value || null, after: after.length ? after : null });
  $("tasktitle").value = ""; $("taskafter").value = "";
}));
$("refresh").addEventListener("click", () => refresh());
async function enter(name) {
  project = name;
  try { localStorage.setItem("coord.project", project); } catch { /* private window */ }
  me = (await call("context").catch(() => ({ me: {} }))).me.name;     // takes this project's session
  $("me").textContent = `you: ${me ?? state.me.name}`;
  await refresh();
  listen();
}
$("project").addEventListener("change", () => enter($("project").value));

(async () => {
  const r = await (await fetch("/api/state")).json();
  state = r.result;
  $("me").textContent = `you: ${state.me.name}`;
  $("version").textContent = `coord ${state.server.version}`;
  const busiest = [...state.projects].sort((x, y) => (y.live_sessions - x.live_sessions) || (y.messages - x.messages));
  const names = busiest.map((p) => p.project).filter((p) => p !== "(all)");
  const saved = (() => { try { return localStorage.getItem("coord.project"); } catch { return null; } })();
  const first = names.includes(saved) ? saved : names[0] || "default";
  $("project").replaceChildren(...names.map((n) => el("option", { value: n }, n)));
  $("project").value = first;
  if (window.Notification && Notification.permission === "default") Notification.requestPermission().catch(() => {});
  state.lastEvent = state.last_event;
  await enter(first);
})();
