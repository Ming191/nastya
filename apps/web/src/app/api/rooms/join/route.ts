import { createWindowLimiter, loadRoomConfig, makeRoomAdmin, redeemInvite, RoomError } from "../../../../lib/rooms";
import { jsonBody, requireSameOrigin, roomFailure, roomJson } from "../../../../lib/room-http";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const allow = createWindowLimiter(40, 60_000);

export async function POST(req: Request): Promise<Response> {
  try {
    const config = loadRoomConfig();
    requireSameOrigin(req, config.publicOrigin);
    if (!allow()) throw new RoomError("RATE_LIMITED", 429);
    const input = await jsonBody(req);
    if (!input || typeof input !== "object" || Array.isArray(input)) {
      throw new RoomError("INVALID_REQUEST", 400);
    }
    const data = input as Record<string, unknown>;
    if (data.version !== 1 || typeof data.roomId !== "string" ||
      typeof data.invite !== "string" ||
      (data.preferredLanguage !== "vi" && data.preferredLanguage !== "ru")) {
      throw new RoomError("INVALID_REQUEST", 400);
    }
    return roomJson(await redeemInvite(makeRoomAdmin(config), config, {
      roomId: data.roomId, invite: data.invite, preferredLanguage: data.preferredLanguage,
    }));
  } catch (error) {
    return roomFailure(error);
  }
}
