import { RoomError } from "./rooms";

export function roomJson(body: object, status = 200): Response {
  return Response.json(body, {
    status,
    headers: { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" },
  });
}

export function roomFailure(error: unknown): Response {
  if (error instanceof RoomError) {
    return roomJson({ version: 1, error: {
      code: error.code,
      message: error.code.replaceAll("_", " ").toLowerCase(),
      retryable: error.status === 429 || error.status >= 500,
    } }, error.status);
  }
  return roomJson({ version: 1, error: {
    code: "RTC_UNAVAILABLE", message: "room service unavailable", retryable: true,
  } }, 503);
}

export async function jsonBody(req: Request): Promise<unknown> {
  if (!(req.headers.get("content-type") ?? "").startsWith("application/json") ||
    Number(req.headers.get("content-length") ?? "0") > 2048) {
    throw new RoomError("INVALID_REQUEST", 400);
  }
  const raw = await req.text();
  if (raw.length > 2048) throw new RoomError("INVALID_REQUEST", 400);
  try { return JSON.parse(raw); }
  catch { throw new RoomError("INVALID_REQUEST", 400); }
}

export function requireSameOrigin(req: Request, expected: string): void {
  const supplied = req.headers.get("origin");
  if (supplied && supplied !== expected) throw new RoomError("INVALID_ORIGIN", 403);
}
