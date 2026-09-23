// Human-readable output of the `coord` CLI - same text as coord 0.2.1 (agents' prompts rely on it).
/* eslint-disable @typescript-eslint/no-explicit-any */

export function fmtMsg(m: any): string {
  const to = m.to ? ` -> ${m.to}` : "";
  const re = m.reply_to ? ` re #${m.reply_to}` : "";
  const cl = m.claim ? ` [${m.claim}]` : "";
  const done = m.resolved_at ? `  (resolved by ${m.resolved_by}: ${m.resolution})` : "";
  return `#${m.id} [${m.at}] ${m.from}${to} ${m.kind}${re}${cl}: ${m.body}${done}`;
}

export function fmtClaim(c: any): string {
  return `${c.claim} ${c.scope} (${c.scope_type}) ${c.owner} fence=${c.fence} until ${c.expires_at}`
    + (c.note ? ` - ${c.note}` : "") + (c.released ? " [released]" : "");
}

/** JSON with Python json.dumps separators (", " and ": "), as the 0.2.1 CLI printed it. */
export function py(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(py).join(", ")}]`;
  if (v !== null && typeof v === "object") {
    return `{${Object.entries(v).filter(([, x]) => x !== undefined).map(([k, x]) => `${JSON.stringify(k)}: ${py(x)}`).join(", ")}}`;
  }
  return JSON.stringify(v) ?? "null";
}

/** "consensus: yes|no - a-01 support (implied), b-01 object; why ..." */
export function consensusLine(c: any): string {
  const stances = (c.participants ?? []).map((x: any) => `${x.name} ${x.stance ?? "no stance"}${x.implied ? " (implied)" : ""}`).join(", ");
  return `consensus: ${c.met ? "yes" : "no"} - ${stances}` + (c.met || !c.why?.length ? "" : `; ${c.why.join("; ")}`);
}

export function human(cmd: string, r: any): string {
  if (cmd === "inbox" || cmd === "thread") return r.map(fmtMsg).join("\n") || "(no messages)";
  if (cmd === "locks") return r.map(fmtClaim).join("\n") || "(no active claims)";
  if (cmd === "claim" || cmd === "renew") return fmtClaim(r);
  if (cmd === "poll") {
    const out = r.messages.length ? r.messages.map(fmtMsg) : ["(no new messages)"];
    out.push("claims: " + (r.my_claims.map((c: any) => `${c.claim} ${c.scope}`).join(", ") || "-"));
    out.push("tasks: " + (r.tasks.map((t: any) => `${t.task} ${t.title}`).join(", ") || "-"));
    out.push("discussions: " + (r.discussions.map((d: any) => `${d.discussion} ${d.topic}`).join(", ") || "-"));
    return out.join("\n");
  }
  if (cmd === "doc-show") return `${r.document} r${r.revision}/${r.latest_revision} [${r.kind}/${r.status}] ${r.title}\n\n${r.content}`;
  if (cmd === "tasks") {
    return r.map((t: any) => `${t.task} [${t.status}] p${t.priority} ${t.title}` + (t.assigned ? ` (${t.assigned})` : "")).join("\n") || "(no tasks)";
  }
  if (cmd === "discussion") {
    const who = r.participants ? r.participants.join(", ") : "open (opener + whoever reacts)";
    const lines = [`${r.discussion} [${r.status}] ${r.topic} (by ${r.created_by})`,
                   `  rule: ${r.rule ?? "unanimous"}, quorum ${r.quorum ?? 2}, participants: ${who}`];
    for (const p of r.proposals) {
      const t = Object.entries(p.tally).filter(([, v]) => v).map(([k, v]) => `${k}=${v}`).join(" ");
      lines.push(`  ${p.proposal} [${p.status}] ${p.author}: ${p.body}  ${t}`);
      if (p.consensus && r.status === "open") lines.push(`    ${consensusLine(p.consensus)}`);
    }
    if (r.decision) {
      lines.push(`  decided by ${r.decided_by} (consensus=${r.consensus ? "yes" : "no"}): ${r.decision} -> ${r.decision_document}`);
      if (r.consensus_detail) lines.push(`    ${consensusLine(r.consensus_detail)}`);
      if (r.decision_reason) lines.push(`    reason: ${r.decision_reason}`);
    }
    return lines.join("\n");
  }
  if (cmd === "decide") {
    return `${r.discussion} decided -> ${r.document}, consensus: ${r.consensus ? "yes" : "no"}`
      + (r.why?.length ? `\n  why not: ${r.why.join("; ")}` : "");
  }
  if (cmd === "memory") {
    return r.map((m: any) => `${m.memory} [${m.kind}] ${m.title} (r${m.revision}, ${m.updated_by})\n${m.content}`).join("\n\n") || "(no memory)";
  }
  if (cmd === "context") {
    const lines = [`you: ${r.me.name} gen ${r.me.generation} project ${r.me.project} | unread ${r.unread}`];
    lines.push(...r.overview.map((m: any) => `overview: ${m.title}: ${m.content}`));
    lines.push(...r.memory.map((m: any) => `memory: ${m.memory} [${m.kind}] ${m.title}`));
    lines.push(...r.my_claims.map((c: any) => `claim: ${c.claim} ${c.scope}`));
    lines.push(...[...r.my_tasks, ...r.open_tasks].map((t: any) => `task: ${t.task} [${t.status}] ${t.title}`));
    lines.push(...r.discussions.map((d: any) => `discussion: ${d.discussion} ${d.topic}`));
    return lines.join("\n");
  }
  if (cmd === "check") {
    if (r.ok) return "ok: no staged file is claimed by another session";
    return r.conflicts.map((c: any) => `CLAIMED ${c.file} by ${c.owner} (${c.claim} ${c.scope})`).join("\n");
  }
  if (Array.isArray(r)) return r.map(py).join("\n") || "(none)";
  return typeof r === "string" ? r : py(r);
}
