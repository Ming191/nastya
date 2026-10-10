import { createHmac } from "node:crypto";
import { RoomError, type RoomConfig, type Role } from "./rooms";

/**
 * Production requires Redis REST for atomic shared quota + participant role claim.
 * Only loopback HTTP development may use the bounded process-local fallback.
 */
export interface SecurityGate {
  checkBudget(kind: "create" | "join"): Promise<void>;
  claimRole(roomId: string, role: Role, nonce: string, ttlSeconds: number): Promise<void>;
}

const BUDGET = {
  create: { limit: 60, ms: 60_000 },
  join: { limit: 300, ms: 60_000 },
} as const;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
export function requireSessionNonce(value: unknown): string {
  if (typeof value !== "string" || !UUID.test(value)) {
    throw new RoomError("INVALID_REQUEST", 400);
  }
  return value;
}

export function roleLeaseKey(config: RoomConfig, room: string, role: Role, nonce: string): {
  key: string; binding: string;
} {
  // Avoid storing session identifiers or bearer tokens as Redis keys/values.
  const key = "nastya:role:" + room + ":" + role;
  const binding = createHmac("sha256", config.inviteSecret)
    .update(room + ":" + role + ":" + nonce).digest("hex");
  return { key, binding };
}

type PostCommand = (command: Array<string | number>) => Promise<number>;

const QUOTA_SCRIPT = [
  "local n=redis.call('INCR',KEYS[1])",
  "if n==1 then redis.call('PEXPIRE',KEYS[1],ARGV[1]) end",
  "return n",
].join(";");

const CLAIM_SCRIPT = [
  "local old=redis.call('GET',KEYS[1])",
  "if not old then redis.call('SET',KEYS[1],ARGV[1],'PX',ARGV[2]);return 1 end",
  "if old==ARGV[1] then return 1 end",
  "return 0",
].join(";");

export function redisGate(config: RoomConfig, post: PostCommand): SecurityGate {
  return {
    async checkBudget(kind) {
      const { ms, limit } = BUDGET[kind];
      const count = await post(["EVAL", QUOTA_SCRIPT, 1, "nastya:budget:" + kind, ms]);
      if (count > limit) throw new RoomError("RATE_LIMITED", 429);
    },
    async claimRole(roomId, role, nonce, ttlSeconds) {
      const { key, binding } = roleLeaseKey(config, roomId, role, requireSessionNonce(nonce));
      const result = await post(["EVAL", CLAIM_SCRIPT, 1, key, binding, ttlSeconds * 1000]);
      if (result !== 1) throw new RoomError("ROLE_OCCUPIED", 409);
    },
  };
}

export function redisPost(
  endpoint: string, token: string, fetcher: typeof fetch = fetch,
): PostCommand {
  let url: URL;
  try { url = new URL(endpoint); } catch {
    throw new RoomError("ROOM_SERVICE_NOT_CONFIGURED", 503);
  }
  if (url.protocol !== "https:" || !!url.username || !!url.password ||
    !!url.hash || !!url.search || url.pathname !== "/" ||
    !token || token.length < 16) {
    throw new RoomError("ROOM_SERVICE_NOT_CONFIGURED", 503);
  }
  return async (command) => {
    try {
      const response = await fetcher(url, {
        method: "POST",
        headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
        body: JSON.stringify(command),
        signal: AbortSignal.timeout(2500),
        cache: "no-store",
      });
      if (!response.ok) throw new Error("redis unavailable");
      const body: unknown = await response.json();
      if (!body || typeof body !== "object" || !("result" in body) ||
        !Number.isSafeInteger((body as { result: unknown }).result) ||
        (body as { error?: unknown }).error) throw new Error("redis invalid response");
      return (body as { result: number }).result;
    } catch {
      // Never log bearer tokens, session fingerprints or Redis response bodies.
      throw new RoomError("ROOM_SERVICE_NOT_CONFIGURED", 503);
    }
  };
}

/** Strictly development-only, bounded and process-local. No public production use. */
export function memoryGate(config: RoomConfig): SecurityGate {
  const budgets = new Map<string, { count: number; deadline: number }>();
  const leases = new Map<string, { binding: string; deadline: number }>();
  const cap = 5000;
  return {
    async checkBudget(kind) {
      const now = Date.now();
      const spec = BUDGET[kind];
      const found = budgets.get(kind);
      const item = found && found.deadline > now
        ? { count: found.count + 1, deadline: found.deadline }
        : { count: 1, deadline: now + spec.ms };
      budgets.set(kind, item);
      if (item.count > spec.limit) throw new RoomError("RATE_LIMITED", 429);
    },
    async claimRole(room, role, nonce, ttl) {
      const { key, binding } = roleLeaseKey(config, room, role, requireSessionNonce(nonce));
      const now = Date.now();
      const current = leases.get(key);
      if (current && current.deadline > now && current.binding !== binding) {
        throw new RoomError("ROLE_OCCUPIED", 409);
      }
      if (leases.size >= cap && !leases.has(key)) {
        for (const [k, v] of leases) {
          if (v.deadline < now) leases.delete(k);
        }
        if (leases.size >= cap) throw new RoomError("RATE_LIMITED", 429);
      }
      if (!current || current.deadline <= now) {
        leases.set(key, { binding, deadline: now + ttl * 1000 });
      }
    },
  };
}

const devGates = new Map<string, SecurityGate>();
export function securityGate(
  config: RoomConfig, env: Record<string, string | undefined> = process.env,
): SecurityGate {
  const host = new URL(config.publicOrigin).hostname;
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(host);
  if (local && new URL(config.publicOrigin).protocol === "http:") {
    const key = config.publicOrigin;
    let gate = devGates.get(key);
    if (!gate) {
      gate = memoryGate(config);
      devGates.set(key, gate);
    }
    return gate;
  }
  // All non-loopback deployments fail CLOSED when durable coordination is missing.
  const endpoint = env.NASTYA_REDIS_REST_URL;
  const token = env.NASTYA_REDIS_REST_TOKEN;
  if (!endpoint || !token) throw new RoomError("ROOM_SERVICE_NOT_CONFIGURED", 503);
  return redisGate(config, redisPost(endpoint, token));
}
