import assert from "node:assert/strict";
import test from "node:test";
import {
  memoryGate, redisGate, redisPost, requireSessionNonce, roleLeaseKey, securityGate,
} from "./security-gate";
import { jsonBody, requireSameOrigin, roomFailure, roomJson } from "./room-http";
import { createPrivateRoom, redeemInvite, RoomError, type RoomAdmin, type RoomConfig } from "./rooms";

const config: RoomConfig = {
  publicOrigin: "https://nastya.example", wsUrl: "wss://livekit.example",
  apiHost: "https://livekit.example", apiKey: "test-key",
  apiSecret: "test-secret", inviteSecret: "32-characters-long-distinct-secret-for-tests",
};
const first = "b6c5c512-1419-4cd0-8e04-7db323e41fc2";
const second = "7ace4678-0211-482b-b79b-816c58011aaf";

test("nonce must be a real v4 UUID, never an arbitrary caller-controlled role", () => {
  assert.equal(requireSessionNonce(first), first);
  for (const value of ["x", "a".repeat(600), first.toUpperCase(), "11111111-1111-1111-1111-111111111111", 1]) {
    assert.throws(() => requireSessionNonce(value), { code: "INVALID_REQUEST" });
  }
  const a = roleLeaseKey(config, "nastya_" + "a".repeat(32), "guest", first);
  const b = roleLeaseKey(config, "nastya_" + "a".repeat(32), "guest", second);
  assert.equal(a.key, b.key);
  assert.notEqual(a.binding, b.binding);
  assert.ok(!a.binding.includes(first));
});

test("local dev role lease is first-device-wins, same tab can rejoin", async () => {
  const gate = memoryGate(config);
  const room = "nastya_" + "c".repeat(32);
  await gate.claimRole(room, "owner", first, 30);
  await gate.claimRole(room, "owner", first, 30);
  await assert.rejects(gate.claimRole(room, "owner", second, 30), { code: "ROLE_OCCUPIED" });
  await gate.claimRole(room, "guest", second, 30);
  await gate.claimRole("nastya_" + "d".repeat(32), "owner", second, 30);
});

test("production never falls back to in-memory when Redis credentials absent or malformed", async () => {
  assert.throws(() => securityGate(config, {}), { code: "ROOM_SERVICE_NOT_CONFIGURED" });
  assert.throws(() => securityGate(config, { NASTYA_REDIS_REST_URL: "http://redis.example", NASTYA_REDIS_REST_TOKEN: "short" }),
    { code: "ROOM_SERVICE_NOT_CONFIGURED" });
  const local = { ...config, publicOrigin: "http://127.0.0.1:3000" };
  assert.ok(securityGate(local, {}));
});

test("distributed Redis EVAL implements atomic quota + role lease across replicas", async () => {
  const hits: unknown[][] = [];
  const budgets = new Map<string, number>();
  const leases = new Map<string, string>();
  const evalFake = async (command: Array<string | number>): Promise<number> => {
    hits.push(command);
    assert.equal(command[0], "EVAL");
    const key = String(command[3]);
    if (key.startsWith("nastya:budget:")) {
      const total = (budgets.get(key) ?? 0) + 1;
      budgets.set(key, total);
      return total;
    }
    const binding = String(command[4]);
    const old = leases.get(key);
    if (old && old !== binding) return 0;
    leases.set(key, binding);
    return 1;
  };
  const gateA = redisGate(config, evalFake);
  const gateB = redisGate(config, evalFake);
  await Promise.all([gateA.claimRole("nastya_" + "a".repeat(32), "guest", first, 60),
    gateB.claimRole("nastya_" + "a".repeat(32), "guest", first, 60)]);
  await assert.rejects(gateB.claimRole("nastya_" + "a".repeat(32), "guest", second, 60),
    { code: "ROLE_OCCUPIED" });
  await Promise.all(Array.from({ length: 60 }, () => gateA.checkBudget("create")));
  await assert.rejects(gateB.checkBudget("create"), { code: "RATE_LIMITED" });
  assert.ok(hits.every((x) => x[0] === "EVAL"));
  assert.ok(hits.every((x) => !JSON.stringify(x).includes(first)));
});

test("Redis HTTP response errors and invalid JSON fail CLOSED without logging credentials", async () => {
  const url = "https://redis.example/";
  const secret = "private-test-redis-rest-token";
  const broken = redisPost(url, secret, async () => new Response("oops", { status: 503 }));
  await assert.rejects(broken(["GET", "test"]), { code: "ROOM_SERVICE_NOT_CONFIGURED" });
  const good = redisPost(url, secret, async (request, init) => {
    assert.equal(String(request), url);
    assert.equal((init?.headers as Record<string, string>).Authorization, "Bearer " + secret);
    assert.equal(init?.cache, "no-store");
    return Response.json({ result: 1 });
  });
  assert.equal(await good(["EVAL", "return 1", 0]), 1);
  const invalid = redisPost(url, secret, async () => Response.json({ error: "invalid Lua script" }));
  await assert.rejects(invalid(["EVAL", "bad", 0]), { code: "ROOM_SERVICE_NOT_CONFIGURED" });
});

test("origin and Fetch Metadata block cross-site browser requests before capability use", () => {
  const expected = "https://nastya.example";
  requireSameOrigin(new Request(expected + "/api/rooms", { method: "POST" }), expected);
  requireSameOrigin(new Request(expected + "/api/rooms", { method: "POST",
    headers: { origin: expected, "sec-fetch-site": "same-origin" } }), expected);
  for (const headers of [
    new Headers({ origin: "https://evil.example" }),
    new Headers({ "sec-fetch-site": "cross-site" }),
    new Headers({ "sec-fetch-site": "same-site" }),
    new Headers({ origin: "null" }),
  ]) {
    assert.throws(() => requireSameOrigin(new Request(expected, { method: "POST", headers }), expected),
      { code: "INVALID_ORIGIN" });
  }
});

test("request parser rejects streaming unbounded body, JSON pollution and malformed UTF-8", async () => {
  const good = (body: string, headers?: Record<string, string>) => new Request(
    "https://nastya.example/api/rooms/join",
    { method: "POST", headers: { "Content-Type": "application/json", ...headers }, body },
  );
  assert.deepEqual(await jsonBody(good('{"version":1}')), { version: 1 });
  await assert.rejects(jsonBody(good("x".repeat(4096))), { code: "INVALID_REQUEST" });
  await assert.rejects(jsonBody(good('{"ok":true}', { "content-type": "text/plain" })),
    { code: "INVALID_REQUEST" });
  const stream = new ReadableStream<Uint8Array>({
    start(c) { c.enqueue(new Uint8Array(1024)); c.enqueue(new Uint8Array(1030)); c.close(); },
  });
  await assert.rejects(jsonBody(new Request("https://nastya.example/api/rooms/join", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: stream, duplex: "half",
  } as RequestInit)), { code: "INVALID_REQUEST" });
  const failure = roomFailure(new Error("secret API key / transcript from user"));
  assert.equal(failure.status, 503);
  const payload = await failure.text();
  assert.ok(!payload.includes("secret") && !payload.includes("transcript"));
  const response = roomJson({ ok: true });
  assert.match(response.headers.get("cache-control") ?? "", /no-store/);
  assert.equal(response.headers.get("referrer-policy"), "no-referrer");
});

test("role reservation prevents concurrent replacement before JWT minting", async () => {
  const state = new Map<string, string>();
  const gate = redisGate(config, async (args) => {
    const key = String(args[3]);
    const token = String(args[4]);
    const current = state.get(key);
    if (current && current !== token) return 0;
    state.set(key, token);
    return 1;
  });
  const rooms = new Map<string, { name: string; metadata: string }>();
  const admin: RoomAdmin = {
    async createRoom(item) { rooms.set(item.name, item); },
    async listRooms(names) { return names.flatMap((key) => rooms.get(key) ?? []); },
    async listParticipants() { return []; },
  };
  const made = await createPrivateRoom(admin, config);
  const invite = new URL(made.guestUrl).hash.replace("#invite=", "");
  const join = (nonce: string) => redeemInvite(admin, config, {
    roomId: made.roomId, invite, preferredLanguage: "ru", sessionNonce: nonce,
  }, Math.floor(Date.now() / 1000), gate);
  const outcomes = await Promise.allSettled([join(first), join(second)]);
  assert.equal(outcomes.filter((x) => x.status === "fulfilled").length, 1);
  assert.equal(outcomes.filter((x) => x.status === "rejected" &&
    x.reason instanceof RoomError && x.reason.code === "ROLE_OCCUPIED").length, 1);
  const winner = outcomes[0].status === "fulfilled" ? first : second;
  const renewed = await join(winner);
  assert.equal(renewed.participantRole, "guest");
  const jwt = JSON.parse(Buffer.from(renewed.participantToken.split(".")[1], "base64url").toString());
  assert.deepEqual(jwt.video.canPublishSources, ["camera", "microphone"]);
  assert.equal(jwt.video.canPublishData, false);
  assert.equal(jwt.video.roomCreate, false);
});
