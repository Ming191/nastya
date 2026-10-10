import assert from "node:assert/strict";
import test from "node:test";
import {
  createPrivateRoom, createWindowLimiter, loadRoomConfig, readInvite,
  redeemInvite, RoomError, type RoomAdmin, type RoomConfig,
} from "./rooms";

const config: RoomConfig = {
  publicOrigin: "https://nastya.example",
  wsUrl: "wss://test.livekit.cloud",
  apiHost: "https://test.livekit.cloud",
  apiKey: "test-key",
  apiSecret: "super-secret-for-livekit-testing",
  inviteSecret: "32-chars-minimum-secret-for-nastya-auth",
};

class FakeAdmin implements RoomAdmin {
  rooms = new Map<string, { name: string; metadata: string }>();
  participants: Array<{ identity: string }> = [];
  options: Record<string, unknown> | undefined;
  async createRoom(data: {
    name: string; maxParticipants: number; emptyTimeout: number;
    departureTimeout: number; metadata: string;
  }) {
    this.options = data;
    this.rooms.set(data.name, { name: data.name, metadata: data.metadata });
  }
  async listRooms(names: string[]) {
    return names.flatMap((id) => {
      const room = this.rooms.get(id);
      return room ? [room] : [];
    });
  }
  async listParticipants() { return this.participants; }
}

async function make() {
  const admin = new FakeAdmin();
  const room = await createPrivateRoom(admin, config, 1_000_000);
  const guest = new URL(room.guestUrl).hash.slice("#invite=".length);
  return { admin, room, guest };
}
function code(fn: () => unknown, expected: string) {
  assert.throws(fn, (error: unknown) => error instanceof RoomError && error.code === expected);
}

test("requires a dedicated invite secret, valid URLs, and server credentials", () => {
  const env = {
    NASTYA_PUBLIC_ORIGIN: "http://localhost:3000",
    LIVEKIT_URL: "wss://demo.livekit.cloud",
    LIVEKIT_API_KEY: "key", LIVEKIT_API_SECRET: "secret",
    NASTYA_INVITE_SECRET: config.inviteSecret,
  };
  assert.equal(loadRoomConfig(env).apiHost, "https://demo.livekit.cloud");
  code(() => loadRoomConfig({ ...env, NASTYA_INVITE_SECRET: "short" }), "ROOM_SERVICE_NOT_CONFIGURED");
  code(() => loadRoomConfig({ ...env, LIVEKIT_URL: "ws://other.example" }), "ROOM_SERVICE_NOT_CONFIGURED");
  code(() => loadRoomConfig({ ...env, NASTYA_PUBLIC_ORIGIN: "https://someone.example/path" }), "ROOM_SERVICE_NOT_CONFIGURED");
});

test("creates unpredictable room, separate signed roles and exactly three total slots", async () => {
  const { admin, room, guest } = await make();
  assert.match(room.roomId, /^nastya_[0-9a-f]{32}$/);
  assert.equal(admin.options?.maxParticipants, 3);
  assert.ok(room.guestUrl.startsWith("https://nastya.example/call/"));
  assert.ok(!room.guestUrl.includes(config.apiSecret));
  assert.ok(!room.guestUrl.includes(room.ownerInvite));
  assert.equal(readInvite(room.ownerInvite, room.roomId, config.inviteSecret, 1_000_100).role, "owner");
  assert.equal(readInvite(guest, room.roomId, config.inviteSecret, 1_000_100).role, "guest");
  assert.notEqual(guest, room.ownerInvite);
});

test("rejects tampering, replay to another room, wrong key, and expired invite", async () => {
  const { room, guest } = await make();
  code(() => readInvite(guest + "a", room.roomId, config.inviteSecret, 1_000_001), "INVALID_INVITE");
  code(() => readInvite(guest, "nastya_" + "0".repeat(32), config.inviteSecret, 1_000_001), "INVALID_INVITE");
  code(() => readInvite(guest, room.roomId, "some-other-secret", 1_000_001), "INVALID_INVITE");
  code(() => readInvite(guest, room.roomId, config.inviteSecret, 1_003_600), "INVITE_EXPIRED");
});

test("redeems role-scoped short-lived token for both VI and RU", async () => {
  const { admin, room, guest } = await make();
  const owner = await redeemInvite(admin, config, {
    roomId: room.roomId, invite: room.ownerInvite, preferredLanguage: "vi",
  }, 1_000_010);
  const visitor = await redeemInvite(admin, config, {
    roomId: room.roomId, invite: guest, preferredLanguage: "ru",
  }, 1_000_020);
  assert.equal(owner.participantRole, "owner");
  assert.equal(visitor.participantRole, "guest");
  assert.equal(owner.wsUrl, config.wsUrl);
  const jwt = JSON.parse(Buffer.from(visitor.participantToken.split(".")[1], "base64url").toString());
  assert.equal(jwt.sub, "human:guest");
  assert.equal(jwt.video.room, room.roomId);
  assert.equal(jwt.video.roomJoin, true);
  assert.equal(jwt.video.canPublishData, false);
  assert.equal(jwt.video.roomAdmin, false);
  const currentTime = Math.floor(Date.now() / 1000);
  assert.ok(jwt.exp > currentTime && jwt.exp <= currentTime + 300);
  assert.equal(jwt.attributes.sourceLanguage, "ru");
});

test("fails closed if room is deleted or replaced", async () => {
  const { admin, room, guest } = await make();
  admin.rooms.delete(room.roomId);
  await assert.rejects(() => redeemInvite(admin, config, {
    roomId: room.roomId, invite: guest, preferredLanguage: "vi",
  }, 1_000_001), { code: "ROOM_EXPIRED" });
  admin.rooms.set(room.roomId, { name:room.roomId, metadata:"{}" });
  await assert.rejects(() => redeemInvite(admin, config, {
    roomId: room.roomId, invite: guest, preferredLanguage: "vi",
  }, 1_000_001), { code: "ROOM_EXPIRED" });
});

test("denies occupied role and room overflow without affecting interpreter capacity", async () => {
  const { admin, room, guest } = await make();
  admin.participants = [{ identity: "human:guest" }, { identity: "interpreter" }];
  await assert.rejects(() => redeemInvite(admin, config, {
    roomId: room.roomId, invite: guest, preferredLanguage: "ru",
  }, 1_000_100), { code: "ROLE_OCCUPIED" });
  admin.participants = [{ identity: "human:owner" }, { identity: "human:unrecognized" }];
  await assert.rejects(() => redeemInvite(admin, config, {
    roomId: room.roomId, invite: guest, preferredLanguage: "ru",
  }, 1_000_100), { code: "ROOM_FULL" });
});

test("rejects unsupported source language and expired room", async () => {
  const { admin, room, guest } = await make();
  await assert.rejects(() => redeemInvite(admin, config, {
    roomId: room.roomId, invite: guest, preferredLanguage: "en" as "ru",
  }, 1_000_100), { code:"INVALID_LANGUAGE" });
  await assert.rejects(() => redeemInvite(admin, config, {
    roomId: room.roomId, invite: guest, preferredLanguage: "ru",
  }, 1_003_600), { code:"INVITE_EXPIRED" });
});

test("single-process rate limiter has a bounded window", () => {
  const allow = createWindowLimiter(2, 1_000);
  assert.equal(allow(10_000), true);
  assert.equal(allow(10_000), true);
  assert.equal(allow(10_000), false);
  assert.equal(allow(11_000), true);
});
