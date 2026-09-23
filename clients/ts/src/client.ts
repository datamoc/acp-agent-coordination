import { join } from "node:path";
import { loadConfig } from "./config.js";
import { CoordError } from "./errors.js";
import { repoRoot } from "./git.js";
import { OPS, type OpArgs, type OpName } from "./ops.generated.js";
import { TokenProvider } from "./oidc.js";
import { LocalTransport, RemoteTransport, type TokenSource, type Transport } from "./transport.js";

/** A fixed COORD_TOKEN wins; with COORD_OIDC_* settings, a self-refreshing Keycloak token. */
export function tokenSource(env: NodeJS.ProcessEnv = process.env): string | TokenSource | null {
  if (env.COORD_TOKEN) return env.COORD_TOKEN;
  if (env.COORD_OIDC_ISSUER || env.COORD_OIDC_TOKEN_URL) return TokenProvider.fromEnv(env);
  return null;
}

/** COORD_SERVER -> remote (mTLS bundle / Keycloak); otherwise local mode on the repo's coord2.db. */
export function transportFromEnv(env: NodeJS.ProcessEnv = process.env): Transport {
  if (env.COORD_SERVER) {
    return new RemoteTransport(env.COORD_SERVER, { ca: env.COORD_CA, cert: env.COORD_CERT, key: env.COORD_KEY,
                                                   token: tokenSource(env), insecure: env.COORD_INSECURE === "1", env });
  }
  return new LocalTransport(env.COORD_DB || join(repoRoot(), "coord2.db"), env.COORD_LOCAL || "coord-local");
}

/**
 * The coord API for TypeScript agents. Every op of schema/ops.json, typed:
 *
 *   const c = CoordClient.fromEnv();              // same config as the `coord` CLI
 *   const me = await c.call("whoami", { family: "my-agent", project: "github.com/org/repo" });
 *   await c.call("claim", { session: me.session_id, scope: "src/auth/", tree: true });
 */
export class CoordClient {
  constructor(readonly transport: Transport) {}

  /** Loads ~/.config/coord/env (or COORD_IDENTITY / COORD_CONFIG) into `env` first, like the CLI. */
  static fromEnv(env: NodeJS.ProcessEnv = process.env): CoordClient {
    loadConfig(env);
    return new CoordClient(transportFromEnv(env));
  }

  /** A standard A2A JSON-RPC method on the coord agent (GetTask, ListTasks, CancelTask,
   *  CreateTaskPushNotificationConfig, ...). Remote only. */
  async a2a(method: string, params: Record<string, unknown>): Promise<any> {
    if (!this.transport.a2a) throw new CoordError("a2a_unsupported", "A2A needs a coord server (COORD_SERVER), not local mode");
    return this.transport.a2a(method, params);
  }

  /** The coord server's A2A Agent Card. Remote only. */
  async agentCard(): Promise<any> {
    const t = this.transport as { agentCard?: () => Promise<unknown> };
    if (!t.agentCard) throw new CoordError("a2a_unsupported", "the agent card needs a coord server (COORD_SERVER)");
    return t.agentCard();
  }

  async call<O extends OpName>(op: O, args: OpArgs[O]): Promise<any> {
    if (!(op in OPS)) throw new CoordError("bad_op", `unknown op ${op}`);
    const clean = Object.fromEntries(Object.entries(args as Record<string, unknown>).filter(([, v]) => v !== undefined));
    const env = await this.transport.send(op, clean);
    if (!env.ok) throw new CoordError(env.error ?? "error", env.message ?? "", env.data ?? null);
    return env.result;
  }
}
