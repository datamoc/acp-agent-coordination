// Human-readable output of the `coord` CLI - same text as coord 0.2.1 (agents' prompts rely on it).
/* eslint-disable @typescript-eslint/no-explicit-any */
export function fmtMsg(m) {
    const to = m.to ? ` -> ${m.to}` : "";
    const re = m.reply_to ? ` re #${m.reply_to}` : "";
    const cl = m.claim ? ` [${m.claim}]` : "";
    const done = m.resolved_at ? `  (resolved by ${m.resolved_by}: ${m.resolution})` : "";
    return `#${m.id} [${m.at}] ${m.from}${to} ${m.kind}${re}${cl}: ${m.body}${done}`;
}
const every = (s) => (s % 86400 === 0 ? `${s / 86400}d` : s % 3600 === 0 ? `${s / 3600}h` : `${Math.round(s / 60)}m`);
/** One event-log line: "#1203 2026-09-24T10:00:00+00:00 claim.acquired claim 42 {scope}". */
export function fmtEvent(e) {
    const p = e.payload && Object.keys(e.payload).length ? " " + JSON.stringify(e.payload) : "";
    return `#${e.event} ${e.at} ${e.kind}${e.entity ? ` ${e.entity} ${e.id}` : ""}${p}`;
}
/** When to look again: "wake: now - R2 due: ..." or "wake: in 12 min (10:42Z) - renew or release C12". */
export function fmtWake(w) {
    const s = w.in_seconds;
    const when = s <= 0 ? "now" : s < 90 ? `in ${s} s` : s < 5400 ? `in ${Math.round(s / 60)} min` : `in ${(s / 3600).toFixed(1)} h`;
    return `wake: ${when}${s > 0 ? ` (${w.next_at})` : ""} - ${w.reason}`;
}
/** Tasks as a forest: each one under its prerequisites (a task with several appears under each, marked ↑). */
export function taskGraph(tasks) {
    const byId = new Map(tasks.map((t) => [t.task, t]));
    const children = new Map();
    for (const t of tasks)
        for (const p of t.after ?? [])
            if (byId.has(p))
                children.set(p, [...(children.get(p) ?? []), t.task]);
    const mark = (t) => (t.status === "done" ? "✓" : t.status === "cancelled" ? "✗" : t.blocked_by?.length ? "⏸" : "•");
    const lines = [], shown = new Set();
    const walk = (id, prefix, last, root) => {
        const t = byId.get(id);
        const again = shown.has(id);
        lines.push(`${prefix}${root ? "" : last ? "└─ " : "├─ "}${mark(t)} ${t.task} ${t.title} [${t.status}]${t.assigned ? ` (${t.assigned})` : ""}`
            + (again ? " ↑" : ""));
        if (again)
            return;
        shown.add(id);
        const kids = children.get(id) ?? [];
        kids.forEach((k, i) => walk(k, root ? "" : prefix + (last ? "   " : "│  "), i === kids.length - 1, false));
    };
    const roots = tasks.filter((t) => !(t.after ?? []).some((p) => byId.has(p)));
    roots.forEach((t) => walk(t.task, "", true, true));
    return lines.join("\n") || "(no tasks)";
}
export function fmtRoutine(x) {
    const when = [x.every ? `every ${every(x.every)}` : "",
        x.on_commit ? `on commit${x.paths.length ? ` (${x.paths.join(", ")})` : ""}` : ""].filter(Boolean).join(" + ");
    const state = x.status !== "active" ? `[${x.status}]` : x.running ? `[running: ${x.running}]` : x.due ? `[due: ${x.why}]`
        : `[next ${x.next_due ?? "on commit"}]`;
    const last = x.last_run_at ? ` | last ${x.last_outcome ?? "?"} by ${x.last_run_by} ${x.last_run_at}${x.last_result ? `: ${x.last_result}` : ""}` : "";
    return `${x.routine} ${state} ${x.title} (${when})${last}`;
}
export function fmtClaim(c) {
    return `${c.claim} ${c.scope} (${c.scope_type}) ${c.owner} fence=${c.fence} until ${c.expires_at}`
        + (c.note ? ` - ${c.note}` : "") + (c.released ? " [released]" : "");
}
/** JSON with Python json.dumps separators (", " and ": "), as the 0.2.1 CLI printed it. */
export function py(v) {
    if (Array.isArray(v))
        return `[${v.map(py).join(", ")}]`;
    if (v !== null && typeof v === "object") {
        return `{${Object.entries(v).filter(([, x]) => x !== undefined).map(([k, x]) => `${JSON.stringify(k)}: ${py(x)}`).join(", ")}}`;
    }
    return JSON.stringify(v) ?? "null";
}
/** "consensus: yes|no - a-01 support (implied), b-01 object; why ..." */
export function consensusLine(c) {
    const stances = (c.participants ?? []).map((x) => `${x.name} ${x.stance ?? "no stance"}${x.implied ? " (implied)" : ""}`).join(", ");
    return `consensus: ${c.met ? "yes" : "no"} - ${stances}` + (c.met || !c.why?.length ? "" : `; ${c.why.join("; ")}`);
}
const list = (xs) => (xs?.length ? xs.join(", ") : "-");
/** One agent: "qwen-01 [paused: quota] ready T12 | messages #40 | wake W3 requested". */
export function fmtAgent(a) {
    const p = a.pending ?? {};
    const work = [p.ready_tasks?.length ? `ready ${list(p.ready_tasks)}` : "", p.blocked_tasks?.length ? `blocked ${list(p.blocked_tasks)}` : "",
        p.messages?.length ? `messages ${list(p.messages)}` : "", p.discussions?.length ? `discussions ${list(p.discussions)}` : ""]
        .filter(Boolean).join(" | ");
    const wakes = (a.wake_requests ?? []).map((w) => `${w.wake} ${w.status}`).join(", ");
    return `${a.name}${a.kind === "human" ? " (human)" : ""} [${a.state}${a.paused ? `: ${a.paused}` : ""}]`
        + (work ? ` ${work}` : " no pending work") + (wakes ? ` | wake ${wakes}` : "") + (a.asleep_with_work ? "  <- asleep with work" : "");
}
export function fmtMilestone(m) {
    const crit = (m.criteria ?? []).map((c) => `  [${c.met ? "x" : " "}] ${c.n}. ${c.text}${c.met ? ` (${c.met_by})` : ""}`);
    const moved = (m.target_history ?? []).length > 1
        ? `  target history: ${m.target_history.map((t) => `${t.target ?? "none"} (${t.reason})`).join(" -> ")}` : "";
    const left = (m.remaining ?? []).length ? `  remaining: ${m.remaining.map((t) => `${t.task} [${t.status}] ${t.title}`).join("; ")}` : "";
    return [`${m.milestone} [${m.status}] ${m.title}` + (m.reached_at ? ` reached ${m.reached_at}` : m.target ? ` target ${m.target}` : " (no target)")
            + ` - ${m.criteria_met}/${(m.criteria ?? []).length} criteria met${m.owner ? `, owner ${m.owner}` : ""}`,
        ...crit, moved, left].filter(Boolean).join("\n");
}
export function human(cmd, r) {
    if (cmd === "inbox" || cmd === "thread")
        return r.map(fmtMsg).join("\n") || "(no messages)";
    if (cmd === "agents")
        return r.map(fmtAgent).join("\n") || "(nobody)";
    if (cmd === "activity")
        return r.map((e) => `${e.at.slice(0, 16).replace("T", " ")} ${e.text}`).join("\n") || "(no activity)";
    if (cmd === "receipts") {
        return [`#${r.id} from ${r.from} (${r.kind}, ${r.priority})`,
            ...r.recipients.map((x) => `  ${x.name}: ${x.state}${x.note ? ` - ${x.note}` : ""}`)].join("\n");
    }
    if (cmd === "milestones")
        return r.map(fmtMilestone).join("\n\n") || "(no milestones)";
    if (cmd === "candidates") {
        return r.map((x) => `${x.suggestion} [${x.status}] ${x.target}: ${x.title} <- ${x.source} (${x.nature}) \u00ab${x.quote}\u00bb`
            + (x.result ? ` -> ${x.result}` : "") + (x.review_note ? ` (${x.reviewed_by}: ${x.review_note})` : "")).join("\n") || "(no candidates)";
    }
    if (cmd === "wake-list") {
        return r.map((w) => `${w.wake} ${w.agent} [${w.status}, ${w.mechanism}] ${w.reason} ${w.ref ?? ""} by ${w.requested_by}`
            + (w.response || w.diagnostic ? ` - ${w.response ?? w.diagnostic}` : "")).join("\n") || "(no wake-up requests)";
    }
    if (cmd === "wake" && r.resume)
        return `${r.wake} to ${r.agent}: ${r.status} (${r.mechanism})${r.manual ? `\n${r.manual}` : ""}\n\n${r.resume}`;
    if (cmd === "mandates") {
        return r.map((m) => `${m.mandate} [${m.status}] ${m.holder} until ${m.expires_at} - ${m.powers.join(", ")}; ${m.reason}`
            + ` (granted by ${m.granted_by.join(", ")})` + (m.acts.length ? `\n  acts: ${m.acts.map((x) => x.discussion ?? x.entity).join(", ")}` : "")
            + (m.review ? `\n  review: ${m.review}` : "")).join("\n") || "(no mandates)";
    }
    if (cmd === "doc-comments")
        return r.map((k) => `${k.comment} ${k.by} r${k.revision}${k.quote ? ` on \u00ab${k.quote}\u00bb` : ""}: ${k.body}`).join("\n") || "(no comments)";
    if (cmd === "dashboard") {
        const out = [];
        for (const p of r.projects) {
            const c = p.counts;
            out.push(`${p.project}: ${c.agents} agents (${c.active} active, ${c.paused} paused) · ${c.tasks} tasks (${c.ready} ready, `
                + `${c.blocked} blocked, ${c.unowned} unowned) · ${c.claims} claims · ${c.attention ? `! ${c.attention}` : "nothing waiting"}`);
            for (const a of p.attention)
                out.push(`  ! ${a.text}`);
            for (const u of p.unblock_points)
                out.push(`  unblock point: ${u.task} ${u.title} -> ${u.unblocks.join(", ")}${u.milestones.length ? ` (milestones ${u.milestones.join(", ")})` : ""}`);
            for (const m of p.milestones)
                out.push(`  next milestone: ${m.milestone} ${m.title} - ${m.criteria_met}/${m.criteria.length} criteria, ${m.remaining.length} task(s) left${m.target ? `, target ${m.target}` : ""}`);
        }
        if (r.activity.length)
            out.push("", ...r.activity.slice(-10).map((e) => `${e.at.slice(11, 16)} ${e.text}`));
        return out.join("\n");
    }
    if (cmd === "locks")
        return r.map(fmtClaim).join("\n") || "(no active claims)";
    if (cmd === "claim" || cmd === "renew")
        return fmtClaim(r);
    if (cmd === "poll") {
        const out = r.messages.length ? r.messages.map(fmtMsg) : ["(no new messages)"];
        out.push("claims: " + (r.my_claims.map((c) => `${c.claim} ${c.scope}`).join(", ") || "-"));
        out.push("tasks: " + (r.tasks.map((t) => `${t.task} ${t.title}`).join(", ") || "-"));
        out.push("discussions: " + (r.discussions.map((d) => `${d.discussion} ${d.topic}`).join(", ") || "-"));
        if (r.routines?.length)
            out.push("routines due: " + r.routines.map((x) => `${x.routine} ${x.title} (${x.why})`).join(", "));
        if (r.awaiting?.length)
            out.push("waiting for you: " + r.awaiting.map((m) => `#${m.id} from ${m.from}${m.priority ? ` (${m.priority})` : ""}`).join(", ")
                + " - coord ack <id> taken|done, or reply");
        for (const w of r.wake_requests ?? [])
            out.push(`${w.wake} asks you to resume (${w.reason} ${w.ref ?? ""}) - coord wake answer ${w.wake} accept|refuse "why"`);
        if (r.wake)
            out.push(fmtWake(r.wake));
        return out.join("\n");
    }
    if (cmd === "doc-show") {
        const prov = r.origin === "import" ? `\nimported: by ${r.deposited_by}, author ${r.author}, ${r.context}${r.written_at ? `, written ${r.written_at}` : ""}`
            + `, AI-assisted: ${r.ai_assisted === null ? "not stated" : r.ai_assisted ? "yes" : "no"}, ${r.fingerprint}, visibility ${r.visibility}`
            + (r.derived?.length ? `\nderived: ${r.derived.map((x) => `${x.suggestion} ${x.target} ${x.status}${x.result ? ` -> ${x.result}` : ""}`).join(", ")}` : "") : "";
        return `${r.document} r${r.revision}/${r.latest_revision} [${r.kind}/${r.status}] ${r.title}${prov}${r.comments ? `\n${r.comments} comment(s): coord doc comments ${r.document}` : ""}\n\n${r.content}`;
    }
    if (cmd === "tasks") {
        return r.map((t) => `${t.task} [${t.kind === "milestone" ? "milestone " : ""}${t.status}${t.ready ? ", ready" : ""}] p${t.priority} ${t.title}` + (t.assigned ? ` (${t.assigned})` : "")
            + (t.blocked_by?.length ? `  blocked by ${t.blocked_by.join(", ")}` : t.after?.length ? `  after ${t.after.join(", ")}` : "")).join("\n") || "(no tasks)";
    }
    if (cmd === "tasks-graph")
        return taskGraph(r);
    if (cmd === "discussion") {
        const who = r.participants ? r.participants.join(", ") : "open (opener + whoever reacts)";
        const lines = [`${r.discussion} [${r.status}] ${r.topic} (by ${r.created_by})`,
            `  rule: ${r.rule ?? "unanimous"}, quorum ${r.quorum ?? 2}, participants: ${who}`];
        for (const p of r.proposals) {
            const t = Object.entries(p.tally).filter(([, v]) => v).map(([k, v]) => `${k}=${v}`).join(" ");
            lines.push(`  ${p.proposal} [${p.status}] ${p.author}${p.supersedes ? ` (replaces ${p.supersedes})` : ""}: ${p.body}  ${t}`);
            for (const x of p.consensus?.reservations ?? [])
                lines.push(`    reservation from ${x.name}: ${x.comment || "-"}`);
            if (p.consensus && r.status === "open")
                lines.push(`    ${consensusLine(p.consensus)}`);
        }
        if (r.decision) {
            lines.push(`  decided by ${r.decided_by} (consensus=${r.consensus ? "yes" : "no"}): ${r.decision} -> ${r.decision_document}`);
            if (r.consensus_detail)
                lines.push(`    ${consensusLine(r.consensus_detail)}`);
            if (r.decision_reason)
                lines.push(`    reason: ${r.decision_reason}`);
        }
        return lines.join("\n");
    }
    if (cmd === "decide") {
        return `${r.discussion} decided -> ${r.document}, consensus: ${r.consensus ? "yes" : "no"}`
            + (r.why?.length ? `\n  why not: ${r.why.join("; ")}` : "");
    }
    if (cmd === "memory") {
        return r.map((m) => `${m.memory} [${m.kind}] ${m.title} (r${m.revision}, ${m.updated_by})\n${m.content}`).join("\n\n") || "(no memory)";
    }
    if (cmd === "server") {
        return [`coord server ${r.version} (this client ${r.client_version})`, r.news ? `new in ${r.version}: ${r.news}` : "",
            `features: ${r.features.join(", ")}`,
            `limits: session ${r.limits.session_ttl / 60} min without poll, claim ${r.limits.claim_ttl / 3600} h, `
                + `message ${r.limits.message_recommended} chars (max ${r.limits.message_max}), routine run ${r.limits.routine_lease / 60} min`,
            `ops: ${r.ops.read.length} read, ${r.ops.write.length} write`].filter(Boolean).join("\n");
    }
    if (cmd === "routines")
        return r.map(fmtRoutine).join("\n") || "(no routines)";
    if (cmd === "routine") {
        const runs = (r.runs ?? []).map((x) => `  run ${x.run} ${x.by} (${x.trigger}) ${x.outcome ?? "running or abandoned"}${x.result ? `: ${x.result}` : ""}`);
        return [fmtRoutine(r), ...(r.instructions ? ["", r.instructions] : []), ...(runs.length ? ["", ...runs] : [])].join("\n");
    }
    if (cmd === "routine-new")
        return `${r.routine} created - due now (first run)`;
    if (cmd === "routine-start") {
        return `${r.routine} run ${r.run} (${r.trigger}) is yours until ${r.until}: ${r.title}`
            + (r.instructions ? `\n\n${r.instructions}` : "") + (r.last_result ? `\n\nlast result: ${r.last_result}` : "")
            + `\n\nwhen finished: coord routine done ${r.routine} "result" [--outcome issues|failed]`;
    }
    if (cmd === "routine-done")
        return `${r.routine} run ${r.run}: ${r.outcome}`;
    if (cmd === "context") {
        const lines = [`you: ${r.me.name} gen ${r.me.generation} project ${r.me.project} | unread ${r.unread}`];
        lines.push(...(r.strategy ?? []).map((m) => `strategy: ${m.title}: ${m.content}`));
        lines.push(...(r.policies ?? []).map((m) => `policy (not put to a vote): ${m.title}: ${m.content}`));
        lines.push(...(r.awaiting ?? []).map((m) => `waiting for you: #${m.id} from ${m.from}${m.priority ? ` (${m.priority})` : ""}: ${m.body.slice(0, 100)}`));
        lines.push(...(r.wake_requests ?? []).map((w) => `wake-up: ${w.wake} (${w.reason} ${w.ref ?? ""}) - coord wake answer ${w.wake} accept|refuse`));
        lines.push(...r.overview.map((m) => `overview: ${m.title}: ${m.content}`));
        lines.push(...(r.routines ?? []).map((x) => `routine due: ${x.routine} ${x.title} (${x.why}) - coord routine start ${x.routine}`));
        if (r.wake)
            lines.push(fmtWake(r.wake));
        lines.push(...r.memory.map((m) => `memory: ${m.memory} [${m.kind}] ${m.title}`));
        lines.push(...r.my_claims.map((c) => `claim: ${c.claim} ${c.scope}`));
        lines.push(...[...r.my_tasks, ...r.open_tasks].map((t) => `task: ${t.task} [${t.status}] ${t.title}`));
        lines.push(...r.discussions.map((d) => `discussion: ${d.discussion} ${d.topic}`));
        return lines.join("\n");
    }
    if (cmd === "check") {
        if (r.ok)
            return "ok: no staged file is claimed by another session";
        const lines = r.conflicts.map((c) => `CLAIMED ${c.file} by ${c.owner} (${c.claim} ${c.scope})`);
        if (r.mode === "warn")
            lines.push("(warn mode: commit allowed)");
        return lines.join("\n");
    }
    if (Array.isArray(r))
        return r.map(py).join("\n") || "(none)";
    return typeof r === "string" ? r : py(r);
}
