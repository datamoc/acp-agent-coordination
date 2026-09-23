import { spawnSync } from "node:child_process";
import { X509Certificate, createPrivateKey, randomUUID } from "node:crypto";
import { existsSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import http from "node:http";
import https from "node:https";
import net from "node:net";
import { basename, dirname, join } from "node:path";
import tls from "node:tls";
import { CoordError } from "./errors.js";
export function isLoopback(host) {
    const h = host.replace(/^\[|\]$/g, "");
    if (h === "" || h === "localhost")
        return true;
    if (net.isIPv4(h))
        return h.startsWith("127.");
    if (net.isIPv6(h))
        return h === "::1";
    return false;
}
/** NO_PROXY / no_proxy: comma list of hosts, .domain suffixes, or *. */
export function bypassProxy(host, env = process.env) {
    if (isLoopback(host))
        return true;
    const list = (env.NO_PROXY ?? env.no_proxy ?? "").split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);
    const h = host.toLowerCase();
    return list.some((e) => e === "*" || h === e.replace(/^\./, "") || h.endsWith(e.startsWith(".") ? e : "." + e));
}
export function proxyFor(url, env = process.env) {
    if (bypassProxy(url.hostname, env))
        return null;
    const p = url.protocol === "https:" ? env.HTTPS_PROXY ?? env.https_proxy : env.HTTP_PROXY ?? env.http_proxy;
    return p ? new URL(p) : null;
}
/** Open a TCP tunnel to host:port through an HTTP proxy (CONNECT). */
function tunnel(proxy, host, port) {
    return new Promise((ok, fail) => {
        const req = http.request({ host: proxy.hostname, port: Number(proxy.port || 80), method: "CONNECT", path: `${host}:${port}` });
        req.once("connect", (res, socket) => (res.statusCode === 200 ? ok(socket) : fail(new Error(`proxy CONNECT ${res.statusCode}`))));
        req.once("error", fail);
        req.end();
    });
}
/** POST a body and return (status, text), honouring TLS options and proxies. Shared with oidc.ts. */
export async function httpRequest(url, method, body, headers, opts = {}) {
    const secure = url.protocol === "https:";
    const port = Number(url.port || (secure ? 443 : 80));
    const proxy = proxyFor(url, opts.env);
    const tlsOpts = {
        ca: opts.ca, cert: opts.cert, key: opts.key, servername: net.isIP(url.hostname) ? undefined : url.hostname,
        rejectUnauthorized: !opts.insecure,
    };
    let socket;
    if (proxy && secure)
        socket = await tunnel(proxy, url.hostname, port);
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
            const chunks = [];
            res.on("data", (c) => chunks.push(c));
            res.on("end", () => ok({ status: res.statusCode ?? 0, text: Buffer.concat(chunks).toString("utf8"), headers: res.headers }));
            res.on("error", fail);
        });
        req.on("error", fail);
        req.end(body ?? undefined);
    });
}
export class A2AError extends CoordError {
}
/** coord-server over HTTP(S), speaking A2A v1.0 JSON-RPC (SendMessage with a data part {op, args});
 *  mTLS bundle, bearer tokens, automatic certificate renewal. */
export class RemoteTransport {
    o;
    url;
    a2aUrl;
    protocol;
    rpcId = 0;
    certPem;
    keyPem;
    caPem;
    constructor(url, o = {}) {
        this.o = o;
        this.url = new URL(url.replace(/\/+$/, "") + "/call");
        this.a2aUrl = new URL(url.replace(/\/+$/, "") + "/a2a");
        this.protocol = o.protocol ?? (o.env?.COORD_PROTOCOL === "call" ? "call" : "a2a");
        this.caPem = o.ca ? readFileSync(o.ca, "utf8") : undefined;
        if (o.cert)
            this.certPem = readFileSync(o.cert, "utf8");
        if (o.key)
            this.keyPem = readFileSync(o.key, "utf8");
    }
    async bearer() {
        const t = this.o.token;
        if (!t)
            return null;
        return typeof t === "string" ? t : t.token();
    }
    /** POST JSON with our credentials; retries once with a fresh token on 401; installs renewals. */
    async post(url, payload) {
        const attempt = async () => {
            const headers = { "Content-Type": "application/json" };
            const b = await this.bearer();
            if (b)
                headers.Authorization = `Bearer ${b}`;
            let res;
            try {
                res = await httpRequest(url, "POST", JSON.stringify(payload), headers, { ca: this.caPem, cert: this.certPem, key: this.keyPem, insecure: this.o.insecure, env: this.o.env });
            }
            catch (e) {
                throw new CoordError("unreachable", `cannot reach ${url.origin}: ${e.message}`);
            }
            let body;
            try {
                body = JSON.parse(res.text || "{}");
            }
            catch {
                body = { ok: false, error: `http_${res.status}`, message: res.text.slice(0, 300) };
            }
            const cert = res.headers["coord-certificate"];
            if (typeof cert === "string")
                this.installRenewal(Buffer.from(cert, "base64").toString("utf8"));
            if (body && typeof body.certificate === "string")
                this.installRenewal(body.certificate);
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
    async agentCard() {
        const url = new URL("/.well-known/agent-card.json", this.url);
        let res;
        try {
            res = await httpRequest(url, "GET", null, {}, { ca: this.caPem, cert: this.certPem, key: this.keyPem, insecure: this.o.insecure, env: this.o.env });
        }
        catch (e) {
            throw new CoordError("unreachable", `cannot reach ${url.origin}: ${e.message}`);
        }
        if (res.status !== 200)
            throw new CoordError("a2a_unsupported", `${url} answered ${res.status}`);
        return JSON.parse(res.text);
    }
    /** One A2A JSON-RPC call; A2A errors become CoordError (coord's own code when it has one). */
    async a2a(method, params) {
        const { status, body } = await this.post(this.a2aUrl, { jsonrpc: "2.0", id: ++this.rpcId, method, params });
        if (status === 404 && !body?.jsonrpc)
            throw new A2AError("a2a_unsupported", `${this.a2aUrl.origin} has no A2A endpoint (coord < 0.3)`);
        if (body?.error) {
            const e = body.error;
            throw new CoordError(e.data?.error ?? `a2a_${e.code}`, e.message ?? "", e.data?.data ?? null);
        }
        return body?.result;
    }
    async send(op, args) {
        if (this.protocol === "a2a") {
            try {
                const res = await this.a2a("SendMessage", {
                    message: { messageId: randomUUID(), role: "ROLE_USER", parts: [{ data: { op, args }, mediaType: "application/json" }] },
                });
                const part = res?.message?.parts?.find((p) => p && "data" in p);
                return { ok: true, result: part ? part.data : null };
            }
            catch (e) {
                if (e instanceof A2AError)
                    this.protocol = "call"; // older server: plain /call from now on
                else if (e instanceof CoordError)
                    return { ok: false, error: e.code, message: e.message, data: e.data };
                else
                    throw e;
            }
        }
        return (await this.post(this.url, { op, args })).body;
    }
    /** Replace our cert atomically with the renewed one - only if it fits our private key. */
    installRenewal(pem) {
        const log = this.o.log ?? ((m) => process.stderr.write(m + "\n"));
        const path = this.o.cert;
        if (!path || !this.keyPem)
            return;
        if (existsSync(path) && readFileSync(path, "utf8").trim() === pem.trim())
            return;
        try {
            if (!new X509Certificate(pem).checkPrivateKey(createPrivateKey(this.keyPem)))
                throw new Error("does not match our key");
            const tmp = join(dirname(path), `.renew-${process.pid}-${basename(path)}`);
            writeFileSync(tmp, pem);
            try {
                renameSync(tmp, path);
            }
            catch (e) {
                unlinkSync(tmp);
                throw e;
            }
            this.certPem = pem;
            log(`coord: certificate renewed by the server (${path})`);
        }
        catch (e) {
            log(`coord: could not install the renewed certificate: ${e.message}`);
        }
    }
}
/** Local mode: no server - one op per `coord-local` run against a SQLite db (Python package). */
export class LocalTransport {
    db;
    command;
    constructor(db, command = process.env.COORD_LOCAL || "coord-local") {
        this.db = db;
        this.command = command;
    }
    async send(op, args) {
        const [cmd, ...rest] = this.command.split(/\s+/).filter(Boolean);
        const p = spawnSync(cmd, rest, { input: JSON.stringify({ db: this.db, op, args }), encoding: "utf8", maxBuffer: 64 << 20 });
        if (p.error) {
            throw new CoordError("local_unavailable", `local mode runs \`${this.command}\` (the coord server package): ${p.error.message}. `
                + "Install it (uv tool install acp-agent-coordination) or set COORD_SERVER");
        }
        try {
            return JSON.parse(p.stdout);
        }
        catch {
            throw new CoordError("local_failed", `${this.command} failed: ${(p.stderr || p.stdout).trim().slice(0, 500)}`);
        }
    }
}
