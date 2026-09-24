import { spawnSync } from "node:child_process";
import { X509Certificate, createPrivateKey, randomUUID } from "node:crypto";
import { existsSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import http from "node:http";
import https from "node:https";
import net from "node:net";
import { basename, dirname, join } from "node:path";
import tls from "node:tls";
import { configFile } from "./config.js";
import { CoordError } from "./errors.js";

/** The /call envelope, identical over HTTP and in local mode. */
export interface Envelope {
  ok: boolean;
  result?: unknown;
  error?: string;
  message?: string;
  data?: unknown;
  certificate?: string; // a renewed client certificate handed out by the server
}

/** One entry of the server's event log (the `events` op, or a server-sent event). */
export interface CoordEvent { event: number; kind: string; entity: string | null; id: string | null; payload: unknown; at: string }

export interface Transport {
  send(op: string, args: Record<string, unknown>): Promise<Envelope>;
  /** Follow the event log from `after`: calls onEvent for each one, resumes after drops, until `signal` aborts.
   *  `session` (when the caller has one) proves membership of a restricted project. */
  follow?(project: string | null, after: number, onEvent: (e: CoordEvent) => void, signal: AbortSignal,
          session?: string | null): Promise<void>;
  /** A raw A2A JSON-RPC call (GetTask, CreateTaskPushNotificationConfig, ...), where supported. */
  a2a?(method: string, params: Record<string, unknown>): Promise<unknown>;
}

/** Anything that can produce a bearer token (a fixed string, or oidc.TokenProvider). */
export interface TokenSource {
  token(): Promise<string>;
  invalidate(): Promise<void>;
}

export function isLoopback(host: string): boolean {
  const h = host.replace(/^\[|\]$/g, "");
  if (h === "" || h === "localhost") return true;
  if (net.isIPv4(h)) return h.startsWith("127.");
  if (net.isIPv6(h)) return h === "::1";
  return false;
}

/** NO_PROXY / no_proxy: comma list of hosts, .domain suffixes, or *. */
export function bypassProxy(host: string, env: NodeJS.ProcessEnv = process.env): boolean {
  if (isLoopback(host)) return true;
  const list = (env.NO_PROXY ?? env.no_proxy ?? "").split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);
  const h = host.toLowerCase();
  return list.some((e) => e === "*" || h === e.replace(/^\./, "") || h.endsWith(e.startsWith(".") ? e : "." + e));
}

export function proxyFor(url: URL, env: NodeJS.ProcessEnv = process.env): URL | null {
  if (bypassProxy(url.hostname, env)) return null;
  const p = url.protocol === "https:" ? env.HTTPS_PROXY ?? env.https_proxy : env.HTTP_PROXY ?? env.http_proxy;
  return p ? new URL(p) : null;
}

/** Open a TCP tunnel to host:port through an HTTP proxy (CONNECT). */
function tunnel(proxy: URL, host: string, port: number): Promise<net.Socket> {
  return new Promise((ok, fail) => {
    const req = http.request({ host: proxy.hostname, port: Number(proxy.port || 80), method: "CONNECT", path: `${host}:${port}` });
    req.once("connect", (res, socket) => (res.statusCode === 200 ? ok(socket) : fail(new Error(`proxy CONNECT ${res.statusCode}`))));
    req.once("error", fail);
    req.end();
  });
}

export interface HttpOptions {
  ca?: string | Buffer;
  cert?: string;
  key?: string;
  insecure?: boolean;
  timeoutMs?: number;
  env?: NodeJS.ProcessEnv;
}

/** POST a body and return (status, text), honouring TLS options and proxies. Shared with oidc.ts. */
export async function httpRequest(url: URL, method: string, body: string | null, headers: Record<string, string>,
                                  opts: HttpOptions = {}): Promise<{ status: number; text: string; headers: http.IncomingHttpHeaders }> {
  const secure = url.protocol === "https:";
  const port = Number(url.port || (secure ? 443 : 80));
  const proxy = proxyFor(url, opts.env);
  const tlsOpts: tls.ConnectionOptions = {
    ca: opts.ca, cert: opts.cert, key: opts.key, servername: net.isIP(url.hostname) ? undefined : url.hostname,
    rejectUnauthorized: !opts.insecure,
  };
  let socket: net.Socket | undefined;
  if (proxy && secure) socket = await tunnel(proxy, url.hostname, port);
  return new Promise((ok, fail) => {
    const common = { method, headers: { ...headers, ...(body !== null ? { "Content-Length": Buffer.byteLength(body).toString() } : {}) } };
    const req = secure
      ? https.request({ ...common, host: url.hostname, port, path: url.pathname + url.search, agent: false, ...tlsOpts,
                        ...(socket ? { createConnection: () => tls.connect({ ...tlsOpts, socket }) } : {}) })
      : http.request(proxy
          ? { ...common, host: proxy.hostname, port: Number(proxy.port || 80), path: url.toString(), agent: false }
          : { ...common, host: url.hostname, port, path: url.pathname + url.search, agent: false });
    req.setTimeout(opts.timeoutMs ?? 30_000, () => req.destroy(new Error(`timeout after ${opts.timeoutMs ?? 30_000} ms`)));
    req.on("response", (res) => {
      const chunks: Buffer[] = [];
      res.on("data", (c: Buffer) => chunks.push(c));
      res.on("end", () => ok({ status: res.statusCode ?? 0, text: Buffer.concat(chunks).toString("utf8"), headers: res.headers }));
      res.on("error", fail);
    });
    req.on("error", fail);
    req.end(body ?? undefined);
  });
}

export interface RemoteOptions {
  ca?: string;        // path to the CA bundle
  cert?: string;      // path to the client cert (replaced in place on renewal)
  key?: string;       // path to the client key
  token?: string | TokenSource | null;
  insecure?: boolean;
  env?: NodeJS.ProcessEnv;
  log?: (msg: string) => void;
  /** "a2a" (default: A2A JSON-RPC at /a2a, falling back to /call on a 404) or "call". */
  protocol?: "a2a" | "call";
}

export class A2AError extends CoordError {}

/** coord-server over HTTP(S), speaking A2A v1.0 JSON-RPC (SendMessage with a data part {op, args});
 *  mTLS bundle, bearer tokens, automatic certificate renewal. */
export class RemoteTransport implements Transport {
  private readonly url: URL;
  private readonly a2aUrl: URL;
  private protocol: "a2a" | "call";
  private rpcId = 0;
  private certPem?: string;
  private keyPem?: string;
  private readonly caPem?: string;

  constructor(url: string, private readonly o: RemoteOptions = {}) {
    this.url = new URL(url.replace(/\/+$/, "") + "/call");
    this.a2aUrl = new URL(url.replace(/\/+$/, "") + "/a2a");
    this.protocol = o.protocol ?? (o.env?.COORD_PROTOCOL === "call" ? "call" : "a2a");
    this.caPem = o.ca ? readFileSync(o.ca, "utf8") : undefined;
    if (o.cert) this.certPem = readFileSync(o.cert, "utf8");
    if (o.key) this.keyPem = readFileSync(o.key, "utf8");
  }

  private async bearer(): Promise<string | null> {
    const t = this.o.token;
    if (!t) return null;
    return typeof t === "string" ? t : t.token();
  }

  /** POST JSON with our credentials; retries once with a fresh token on 401; installs renewals. */
  private async post(url: URL, payload: unknown): Promise<{ status: number; body: any }> {
    const attempt = async () => {
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      const b = await this.bearer();
      if (b) headers.Authorization = `Bearer ${b}`;
      let res;
      try {
        res = await httpRequest(url, "POST", JSON.stringify(payload), headers,
          { ca: this.caPem, cert: this.certPem, key: this.keyPem, insecure: this.o.insecure, env: this.o.env });
      } catch (e) {
        throw new CoordError("unreachable", `cannot reach ${url.origin}: ${(e as Error).message}`);
      }
      let body: any;
      try {
        body = JSON.parse(res.text || "{}");
      } catch {
        body = { ok: false, error: `http_${res.status}`, message: res.text.slice(0, 300) };
      }
      const cert = res.headers["coord-certificate"];
      if (typeof cert === "string") this.installRenewal(Buffer.from(cert, "base64").toString("utf8"));
      if (body && typeof body.certificate === "string") this.installRenewal(body.certificate);
      return { status: res.status, body };
    };
    let r = await attempt();
    if (r.status === 401 && this.o.token && typeof this.o.token !== "string") {
      await this.o.token.invalidate(); // revoked or skewed: get a fresh one, retry once
      r = await attempt();
    }
    return r;
  }

  /** The server's A2A Agent Card (/.well-known/agent-card.json). */
  async agentCard(): Promise<unknown> {
    const url = new URL("/.well-known/agent-card.json", this.url);
    let res;
    try {
      res = await httpRequest(url, "GET", null, {}, { ca: this.caPem, cert: this.certPem, key: this.keyPem, insecure: this.o.insecure, env: this.o.env });
    } catch (e) {
      throw new CoordError("unreachable", `cannot reach ${url.origin}: ${(e as Error).message}`);
    }
    if (res.status !== 200) throw new CoordError("a2a_unsupported", `${url} answered ${res.status}`);
    return JSON.parse(res.text);
  }

  /** One A2A JSON-RPC call; A2A errors become CoordError (coord's own code when it has one). */
  async a2a(method: string, params: Record<string, unknown>): Promise<unknown> {
    const { status, body } = await this.post(this.a2aUrl, { jsonrpc: "2.0", id: ++this.rpcId, method, params });
    if (status === 404 && !body?.jsonrpc) throw new A2AError("a2a_unsupported", `${this.a2aUrl.origin} has no A2A endpoint (coord < 0.3)`);
    if (body?.error) {
      const e = body.error;
      throw new CoordError(e.data?.error ?? `a2a_${e.code}`, e.message ?? "", e.data?.data ?? null);
    }
    return body?.result;
  }

  async send(op: string, args: Record<string, unknown>): Promise<Envelope> {
    if (this.protocol === "a2a") {
      try {
        const res: any = await this.a2a("SendMessage", {
          message: { messageId: randomUUID(), role: "ROLE_USER", parts: [{ data: { op, args }, mediaType: "application/json" }] },
        });
        const part = res?.message?.parts?.find((p: any) => p && "data" in p);
        return { ok: true, result: part ? part.data : null };
      } catch (e) {
        if (e instanceof A2AError) this.protocol = "call";   // older server: plain /call from now on
        else if (e instanceof CoordError) return { ok: false, error: e.code, message: e.message, data: e.data };
        else throw e;
      }
    }
    return (await this.post(this.url, { op, args })).body as Envelope;
  }

  /** GET /events/stream (server-sent events), reconnecting with Last-Event-ID; a server older than
   *  0.8 (404) is followed by polling the `events` op instead. */
  async follow(project: string | null, after: number, onEvent: (e: CoordEvent) => void, signal: AbortSignal,
               session?: string | null): Promise<void> {
    let last = after, backoff = 1000;
    while (!signal.aborted) {
      const qs = [project ? `project=${encodeURIComponent(project)}` : "",
                  session ? `session=${encodeURIComponent(session)}` : ""].filter(Boolean).join("&");
      const url = new URL(`/events/stream${qs ? `?${qs}` : ""}`, this.url);
      const headers: Record<string, string> = { Accept: "text/event-stream", "Last-Event-ID": String(last) };
      const b = await this.bearer();
      if (b) headers.Authorization = `Bearer ${b}`;
      const status = await sseRequest(url, headers, { ca: this.caPem, cert: this.certPem, key: this.keyPem,
                                                      insecure: this.o.insecure, env: this.o.env }, signal, (e) => {
        last = e.event; backoff = 1000; onEvent(e);
      }).catch(() => 0);
      if (status === 404) return pollEvents(this, project, last, onEvent, signal, 2000, session);
      if (status === 401 && this.o.token && typeof this.o.token !== "string") await this.o.token.invalidate();
      await sleep(backoff, signal);
      backoff = Math.min(backoff * 2, 30_000);
    }
  }

  /** Replace our cert atomically with the renewed one - only if it fits our private key. */
  private installRenewal(pem: string): void {
    const log = this.o.log ?? ((m: string) => process.stderr.write(m + "\n"));
    const path = this.o.cert;
    if (!path || !this.keyPem) return;
    if (existsSync(path) && readFileSync(path, "utf8").trim() === pem.trim()) return;
    try {
      if (!new X509Certificate(pem).checkPrivateKey(createPrivateKey(this.keyPem))) throw new Error("does not match our key");
      const tmp = join(dirname(path), `.renew-${process.pid}-${basename(path)}`);
      writeFileSync(tmp, pem);
      try {
        renameSync(tmp, path);
      } catch (e) {
        unlinkSync(tmp);
        throw e;
      }
      this.certPem = pem;
      log(`coord: certificate renewed by the server (${path})`);
    } catch (e) {
      log(`coord: could not install the renewed certificate: ${(e as Error).message}`);
    }
  }
}

const sleep = (ms: number, signal: AbortSignal) => new Promise<void>((ok) => {
  const t = setTimeout(ok, ms);
  signal.addEventListener("abort", () => { clearTimeout(t); ok(); }, { once: true });
});

/** Poll the `events` op every `everyMs` - for local mode and servers without /events/stream. */
export async function pollEvents(t: Transport, project: string | null, after: number, onEvent: (e: CoordEvent) => void,
                                 signal: AbortSignal, everyMs = 2000, session: string | null = null): Promise<void> {
  let last = after;
  while (!signal.aborted) {
    const r = await t.send("events", { after: last, project, limit: 200, session }).catch(() => null);
    for (const e of (r?.ok ? (r.result as CoordEvent[]) : [])) { last = e.event; onEvent(e); }
    await sleep(everyMs, signal);
  }
}

/** One server-sent-events GET: parses `id:`/`data:` blocks until the stream ends; resolves the HTTP status. */
export async function sseRequest(url: URL, headers: Record<string, string>, opts: HttpOptions, signal: AbortSignal,
                                 onEvent: (e: CoordEvent) => void): Promise<number> {
  const secure = url.protocol === "https:";
  const port = Number(url.port || (secure ? 443 : 80));
  const tlsOpts: tls.ConnectionOptions = {
    ca: opts.ca, cert: opts.cert, key: opts.key, servername: net.isIP(url.hostname) ? undefined : url.hostname,
    rejectUnauthorized: !opts.insecure,
  };
  const proxy = proxyFor(url, opts.env);
  let socket: net.Socket | undefined;
  if (proxy && secure) socket = await tunnel(proxy, url.hostname, port);
  return new Promise((ok, fail) => {
    const common = { method: "GET", headers, signal };
    const req = secure
      ? https.request({ ...common, host: url.hostname, port, path: url.pathname + url.search, agent: false, ...tlsOpts,
                        ...(socket ? { createConnection: () => tls.connect({ ...tlsOpts, socket }) } : {}) })
      : http.request({ ...common, host: url.hostname, port, path: url.pathname + url.search, agent: false });
    req.on("response", (res) => {
      if (res.statusCode !== 200) { res.resume(); return ok(res.statusCode ?? 0); }
      let buf = "";
      res.setEncoding("utf8");
      res.on("data", (chunk: string) => {
        buf += chunk;
        let cut;
        while ((cut = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, cut); buf = buf.slice(cut + 2);
          const data = block.split("\n").filter((l) => l.startsWith("data: ")).map((l) => l.slice(6)).join("\n");
          if (data) { try { onEvent(JSON.parse(data)); } catch { /* not an event */ } }
        }
      });
      res.on("end", () => ok(200));
      res.on("error", () => ok(200));
    });
    req.on("error", (e) => (signal.aborted ? ok(0) : fail(e)));
    req.end();
  });
}

/** Local mode: no server - one op per `coord-local` run against a SQLite db (Python package). */
export class LocalTransport implements Transport {
  constructor(private readonly db: string, private readonly command = process.env.COORD_LOCAL || "coord-local") {}

  async send(op: string, args: Record<string, unknown>): Promise<Envelope> {
    const [cmd, ...rest] = this.command.split(/\s+/).filter(Boolean);
    const p = spawnSync(cmd, rest, { input: JSON.stringify({ db: this.db, op, args }), encoding: "utf8", maxBuffer: 64 << 20 });
    if (p.error) {
      throw new CoordError("local_unavailable", `no coord server configured (no COORD_SERVER, no identity at ${configFile()}), `
        + `so coord tried local mode, which runs \`${this.command}\` (the coord server package): ${p.error.message}. `
        + "The human enrolls an identity (`coord-admin enroll <client-name>`) for a running coord-server, "
        + "or installs the package for local mode (`uv tool install <path to the coord checkout>`)");
    }
    try {
      return JSON.parse(p.stdout);
    } catch {
      throw new CoordError("local_failed", `${this.command} failed: ${(p.stderr || p.stdout).trim().slice(0, 500)}`);
    }
  }

  follow(project: string | null, after: number, onEvent: (e: CoordEvent) => void, signal: AbortSignal,
         session?: string | null): Promise<void> {
    return pollEvents(this, project, after, onEvent, signal, 2000, session ?? null);
  }
}
