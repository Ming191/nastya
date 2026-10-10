import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { AccessToken, RoomServiceClient, TrackSource } from "livekit-server-sdk";
import type { SecurityGate } from "./security-gate";

export type Role = "owner" | "guest";
export type Language = "vi" | "ru";
type Invite = { v: 1; roomId: string; role: Role; exp: number };
export const INVITE_TTL = 3600;
export const TOKEN_TTL = 300;
const ROOM_PATTERN = /^nastya_[a-f0-9]{32}$/;

export class RoomError extends Error {
  constructor(public readonly code: string, public readonly status: number) {
    super(code);
  }
}
export interface RoomConfig {
  publicOrigin: string;
  wsUrl: string;
  apiHost: string;
  apiKey: string;
  apiSecret: string;
  inviteSecret: string;
}
export interface RoomAdmin {
  createRoom(input: { name: string; maxParticipants: number; emptyTimeout: number; departureTimeout: number; metadata: string }): Promise<unknown>;
  listRooms(names: string[]): Promise<Array<{ name: string; metadata: string }>>;
  listParticipants(room: string): Promise<Array<{ identity: string }>>;
}
function required(env: Record<string, string | undefined>, name: string): string {
  const value = env[name];
  if (!value || value.trim() !== value) throw new RoomError("ROOM_SERVICE_NOT_CONFIGURED", 503);
  return value;
}
export function loadRoomConfig(env: Record<string, string | undefined> = process.env): RoomConfig {
  const publicUrl = required(env, "NASTYA_PUBLIC_ORIGIN");
  const wsUrl = required(env, "LIVEKIT_URL");
  let front: URL, rtc: URL;
  try { front = new URL(publicUrl); rtc = new URL(wsUrl); }
  catch { throw new RoomError("ROOM_SERVICE_NOT_CONFIGURED", 503); }
  const local = (u: URL) => ["localhost", "127.0.0.1", "[::1]"].includes(u.hostname);
  const clean = (u: URL) => !u.username && !u.password && !u.search && !u.hash && u.pathname === "/";
  if (!clean(front) || !clean(rtc) ||
    !(front.protocol === "https:" || (front.protocol === "http:" && local(front))) ||
    !(rtc.protocol === "wss:" || (rtc.protocol === "ws:" && local(rtc)))) {
    throw new RoomError("ROOM_SERVICE_NOT_CONFIGURED", 503);
  }
  const inviteSecret = required(env, "NASTYA_INVITE_SECRET");
  if (Buffer.byteLength(inviteSecret) < 32) throw new RoomError("ROOM_SERVICE_NOT_CONFIGURED", 503);
  return {
    publicOrigin: front.origin, wsUrl, inviteSecret,
    apiHost: wsUrl.replace(/^wss:/, "https:").replace(/^ws:/, "http:"),
    apiKey: required(env, "LIVEKIT_API_KEY"),
    apiSecret: required(env, "LIVEKIT_API_SECRET"),
  };
}
export function signInvite(invite: Invite, key: string): string {
  const payload = Buffer.from(JSON.stringify(invite)).toString("base64url");
  return payload + "." + createHmac("sha256", key).update(payload).digest("base64url");
}
export function readInvite(token: string, roomId: string, key: string, now: number): Invite {
  if (!ROOM_PATTERN.test(roomId) || token.length > 1024) throw new RoomError("INVALID_INVITE", 403);
  const parts = token.split(".");
  if (parts.length !== 2 || !/^[A-Za-z0-9_-]+$/.test(parts[0]) ||
      !/^[A-Za-z0-9_-]+$/.test(parts[1])) throw new RoomError("INVALID_INVITE", 403);
  const expected = createHmac("sha256", key).update(parts[0]).digest();
  const received = Buffer.from(parts[1], "base64url");
  if (received.length !== expected.length || !timingSafeEqual(received, expected)) {
    throw new RoomError("INVALID_INVITE", 403);
  }
  let value: unknown;
  try { value = JSON.parse(Buffer.from(parts[0], "base64url").toString("utf8")); }
  catch { throw new RoomError("INVALID_INVITE", 403); }
  if (!value || typeof value !== "object") throw new RoomError("INVALID_INVITE", 403);
  const p = value as Partial<Invite>;
  if (p.v !== 1 || p.roomId !== roomId || (p.role !== "owner" && p.role !== "guest") ||
    typeof p.exp !== "number" || !Number.isSafeInteger(p.exp)) {
    throw new RoomError("INVALID_INVITE", 403);
  }
  if (now >= p.exp) throw new RoomError("INVITE_EXPIRED", 410);
  return p as Invite;
}
export function makeRoomAdmin(config: RoomConfig): RoomAdmin {
  return new RoomServiceClient(config.apiHost, config.apiKey, config.apiSecret);
}
export async function createPrivateRoom(admin: RoomAdmin, config: RoomConfig, now = Math.floor(Date.now()/1000)) {
  const roomId = "nastya_" + randomBytes(16).toString("hex");
  const exp = now + INVITE_TTL;
  await admin.createRoom({
    name: roomId, maxParticipants: 3, emptyTimeout: INVITE_TTL,
    departureTimeout: 60, metadata: JSON.stringify({ app: "nastya", v: 1, exp }),
  });
  const guest = signInvite({v:1,roomId,role:"guest",exp},config.inviteSecret);
  return {
    version: 1 as const,
    roomId,
    ownerInvite: signInvite({v:1,roomId,role:"owner",exp},config.inviteSecret),
    guestUrl: config.publicOrigin + "/call/" + roomId + "#invite=" + guest,
    expiresAt: new Date(exp*1000).toISOString(),
  };
}
export async function redeemInvite(
  admin: RoomAdmin,
  config: RoomConfig,
  request: { roomId: string; invite: string; preferredLanguage: Language; sessionNonce?: string },
  now = Math.floor(Date.now()/1000),
  gate?: SecurityGate,
) {
  if (request.preferredLanguage !== "ru" && request.preferredLanguage !== "vi") {
    throw new RoomError("INVALID_LANGUAGE", 400);
  }
  const invitation = readInvite(request.invite, request.roomId, config.inviteSecret, now);
  const rooms = await admin.listRooms([request.roomId]);
  const room = rooms.find((r) => r.name === request.roomId);
  if (!room) throw new RoomError("ROOM_EXPIRED", 410);
  let meta: {app?:string;v?:number;exp?:number};
  try { meta = JSON.parse(room.metadata); }
  catch { throw new RoomError("ROOM_EXPIRED", 410); }
  if (meta.app !== "nastya" || meta.v !== 1 || meta.exp !== invitation.exp ||
      now >= invitation.exp) throw new RoomError("ROOM_EXPIRED", 410);
  const people = await admin.listParticipants(request.roomId);
  const identity = "human:" + invitation.role;
  if (people.some((p) => p.identity === identity)) throw new RoomError("ROLE_OCCUPIED",409);
  if (people.filter((p) => p.identity.startsWith("human:")).length >= 2) {
    throw new RoomError("ROOM_FULL",409);
  }
  // Atomic first-device-wins reservation prevents two distinct browsers
  // redeeming the same room/role concurrently across deployment replicas.
  // A returning tab with the SAME locally retained nonce may refresh its JWT.
  if (gate) {
    await gate.claimRole(request.roomId, invitation.role, request.sessionNonce ?? "",
      invitation.exp - now);
  }
  const ttl = Math.min(TOKEN_TTL, invitation.exp-now);
  const token = new AccessToken(config.apiKey,config.apiSecret, {
    identity, ttl, attributes:{ role:invitation.role, sourceLanguage:request.preferredLanguage },
  });
  token.addGrant({
    roomJoin:true,room:request.roomId,canPublish:true,canSubscribe:true,
    canPublishSources:[TrackSource.CAMERA, TrackSource.MICROPHONE],
    canPublishData:false,canUpdateOwnMetadata:false,roomAdmin:false,roomCreate:false,
  });
  return {
    version:1 as const, wsUrl:config.wsUrl,participantToken:await token.toJwt(),
    participantRole:invitation.role,sourceLanguage:request.preferredLanguage,
    expiresAt:new Date((now+ttl)*1000).toISOString(),
  };
}
export function createWindowLimiter(limit:number, windowMs:number) {
  let start = 0, count=0;
  return (now=Date.now()):boolean => {
    if (!start || now-start>=windowMs) { start=now; count=0; }
    count++;
    return count<=limit;
  };
}
