import { createWindowLimiter, createPrivateRoom, loadRoomConfig, makeRoomAdmin, RoomError } from "../../../lib/rooms";
import { securityGate } from "../../../lib/security-gate";
import { requireSameOrigin, roomFailure, roomJson } from "../../../lib/room-http";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// Per-process abuse backstop. Production also needs edge rate limiting.
const allow = createWindowLimiter(8, 60_000);

export async function POST(req: Request): Promise<Response> {
  try {
    const config = loadRoomConfig();
    requireSameOrigin(req, config.publicOrigin);
    if (!allow()) throw new RoomError("RATE_LIMITED", 429);
    await securityGate(config).checkBudget("create");
    return roomJson(await createPrivateRoom(makeRoomAdmin(config), config), 201);
  } catch (error) {
    return roomFailure(error);
  }
}
