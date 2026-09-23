/**
 * Keycloak/OIDC on the client: get and refresh access tokens so nothing expires mid-session.
 *
 *   client credentials  COORD_OIDC_CLIENT_SECRET(_FILE) set - an agent running as a service account
 *   device login        otherwise - a person runs `coord login` once (SSO in a browser), then the
 *                       refresh token is used (and rotated) from then on
 *
 * Settings: COORD_OIDC_ISSUER (endpoints discovered) or COORD_OIDC_TOKEN_URL, COORD_OIDC_CLIENT_ID,
 * COORD_OIDC_CLIENT_SECRET / COORD_OIDC_CLIENT_SECRET_FILE, COORD_OIDC_SCOPE (default "openid"),
 * COORD_OIDC_CA. State: one 0600 file per issuer+client under ~/.config/coord/oidc/ (same file as
 * coord 0.2.1), updated under a lock so parallel agents don't race a rotating refresh token.
 */
import { createHash } from "node:crypto";
import { chmodSync, closeSync, existsSync, mkdirSync, openSync, readFileSync, renameSync, statSync, unlinkSync, writeSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { CoordError } from "./errors.js";
import { expandUser } from "./config.js";
import { httpRequest } from "./transport.js";
const EARLY = 30; // seconds: treat a token as expired this long before it really is
const LOCK_STALE_MS = 60_000;
const stateDirDefault = (env) => join(env.XDG_CONFIG_HOME || join(homedir(), ".config"), "coord", "oidc");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export class TokenProvider {
    o;
    stateFile;
    endpoints;
    clock;
    ca;
    constructor(o) {
        this.o = o;
        if (!o.issuer && !o.tokenUrl)
            throw new CoordError("oidc_config", "set COORD_OIDC_ISSUER (or COORD_OIDC_TOKEN_URL)");
        if (!o.clientId)
            throw new CoordError("oidc_config", "set COORD_OIDC_CLIENT_ID");
        this.endpoints = o.tokenUrl ? { token_endpoint: o.tokenUrl } : null;
        this.clock = o.clock ?? (() => Date.now() / 1000);
        this.ca = o.ca ? readFileSync(o.ca, "utf8") : undefined;
        const base = o.issuer ?? o.tokenUrl;
        const key = createHash("sha256").update(`${base}|${o.clientId}`).digest("hex").slice(0, 16);
        this.stateFile = join(o.stateDir ?? stateDirDefault(o.env ?? process.env), `${key}.json`);
    }
    static fromEnv(env = process.env) {
        let secret = env.COORD_OIDC_CLIENT_SECRET;
        if (!secret && env.COORD_OIDC_CLIENT_SECRET_FILE)
            secret = readFileSync(expandUser(env.COORD_OIDC_CLIENT_SECRET_FILE), "utf8").trim();
        return new TokenProvider({ clientId: env.COORD_OIDC_CLIENT_ID ?? "", issuer: env.COORD_OIDC_ISSUER, tokenUrl: env.COORD_OIDC_TOKEN_URL,
            clientSecret: secret, scope: env.COORD_OIDC_SCOPE || "openid", ca: env.COORD_OIDC_CA, env });
    }
    // --- plumbing -----------------------------------------------------------
    async post(url, form) {
        let res;
        try {
            res = await httpRequest(new URL(url), "POST", new URLSearchParams(form).toString(), { "Content-Type": "application/x-www-form-urlencoded" }, { ca: this.ca, env: this.o.env });
        }
        catch (e) {
            throw new CoordError("oidc_unreachable", `cannot reach the identity provider: ${e.message}`);
        }
        try {
            return { status: res.status, body: JSON.parse(res.text || "{}") };
        }
        catch {
            return { status: res.status, body: { error: `http_${res.status}` } };
        }
    }
    async endpoint(name) {
        if (this.endpoints === null || (!(name in this.endpoints) && this.o.issuer)) {
            const url = this.o.issuer.replace(/\/+$/, "") + "/.well-known/openid-configuration";
            try {
                const res = await httpRequest(new URL(url), "GET", null, {}, { ca: this.ca, env: this.o.env });
                this.endpoints = JSON.parse(res.text);
            }
            catch (e) {
                throw new CoordError("oidc_unreachable", `OIDC discovery failed at ${url}: ${e.message}`);
            }
        }
        const url = this.endpoints?.[name];
        if (!url)
            throw new CoordError("oidc_config", `the identity provider has no ${name}`);
        return url;
    }
    auth() {
        return this.o.clientSecret ? { client_id: this.o.clientId, client_secret: this.o.clientSecret } : { client_id: this.o.clientId };
    }
    /** Read-modify-write the state file under an exclusive lock; saved even when fn throws. */
    async withState(fn) {
        const dir = join(this.stateFile, "..");
        mkdirSync(dir, { recursive: true });
        try {
            chmodSync(dir, 0o700);
        }
        catch { /* not POSIX */ }
        const lock = this.stateFile.replace(/\.json$/, ".lock");
        for (;;) {
            try {
                closeSync(openSync(lock, "wx"));
                break;
            }
            catch {
                try {
                    if (Date.now() - statSync(lock).mtimeMs > LOCK_STALE_MS)
                        unlinkSync(lock); // holder died
                }
                catch { /* gone meanwhile */ }
                await sleep(50);
            }
        }
        try {
            const state = existsSync(this.stateFile) ? JSON.parse(readFileSync(this.stateFile, "utf8")) : {};
            const before = JSON.stringify(state);
            try {
                return await fn(state);
            }
            finally { // a spent refresh token must not stay on disk
                if (JSON.stringify(state) !== before) {
                    const tmp = this.stateFile.replace(/\.json$/, ".tmp");
                    const fd = openSync(tmp, "w", 0o600);
                    writeSync(fd, JSON.stringify(state));
                    closeSync(fd);
                    renameSync(tmp, this.stateFile);
                }
            }
        }
        finally {
            unlinkSync(lock);
        }
    }
    store(s, tok) {
        s.access_token = tok.access_token;
        s.expires_at = this.clock() + Number(tok.expires_in ?? 60);
        if (tok.refresh_token)
            s.refresh_token = tok.refresh_token; // Keycloak may rotate it: keep the newest
        return s.access_token;
    }
    static clear(s) {
        for (const k of Object.keys(s))
            delete s[k];
    }
    // --- what the client uses -----------------------------------------------
    async token() {
        return this.withState(async (s) => {
            if (s.access_token && (s.expires_at ?? 0) - EARLY > this.clock())
                return s.access_token;
            const url = await this.endpoint("token_endpoint");
            if (this.o.clientSecret) {
                const { status, body } = await this.post(url, { ...this.auth(), grant_type: "client_credentials", scope: this.o.scope ?? "openid" });
                if (status !== 200 || !body.access_token) {
                    throw new CoordError("unauthenticated", `client-credentials login refused: ${body.error_description ?? body.error}`);
                }
                return this.store(s, body);
            }
            if (!s.refresh_token)
                throw new CoordError("unauthenticated", "not logged in to SSO: run `coord login`");
            const { status, body } = await this.post(url, { ...this.auth(), grant_type: "refresh_token", refresh_token: s.refresh_token });
            if (status !== 200 || !body.access_token) {
                TokenProvider.clear(s);
                throw new CoordError("unauthenticated", `SSO session expired or revoked (${body.error_description ?? body.error}): run \`coord login\``);
            }
            return this.store(s, body);
        });
    }
    /** The server refused the token (revoked, clock skew): drop the cached one. */
    async invalidate() {
        await this.withState(async (s) => {
            delete s.access_token;
            delete s.expires_at;
        });
    }
    /** OAuth 2.0 device authorization grant (RFC 8628): the person signs in via SSO. */
    async login(show = (m) => process.stderr.write(m + "\n"), wait = sleep) {
        const { status, body: dev } = await this.post(await this.endpoint("device_authorization_endpoint"), { ...this.auth(), scope: this.o.scope ?? "openid" });
        if (status !== 200 || !dev.device_code) {
            throw new CoordError("oidc_login", `device login refused: ${dev.error_description ?? dev.error} `
                + "(is 'OAuth 2.0 Device Authorization Grant' enabled on the client?)");
        }
        show(`Open ${dev.verification_uri_complete ?? dev.verification_uri} and confirm code ${dev.user_code}`);
        let interval = Number(dev.interval ?? 5);
        const deadline = this.clock() + Number(dev.expires_in ?? 600);
        const tokenUrl = await this.endpoint("token_endpoint");
        while (this.clock() < deadline) {
            await wait(interval * 1000);
            const { status: st, body: tok } = await this.post(tokenUrl, { ...this.auth(), grant_type: "urn:ietf:params:oauth:grant-type:device_code", device_code: dev.device_code });
            if (st === 200 && tok.access_token) {
                await this.withState(async (s) => {
                    TokenProvider.clear(s);
                    this.store(s, tok);
                });
                return { logged_in: true, refresh_token: Boolean(tok.refresh_token) };
            }
            if (tok.error === "slow_down")
                interval += 5;
            else if (tok.error !== "authorization_pending")
                throw new CoordError("oidc_login", `device login failed: ${tok.error_description ?? tok.error}`);
        }
        throw new CoordError("oidc_login", "device login timed out");
    }
    async logout() {
        return this.withState(async (s) => {
            const had = Object.keys(s).length > 0;
            TokenProvider.clear(s);
            return { logged_out: had };
        });
    }
}
