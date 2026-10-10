import { RoomError } from "./rooms";

const MAX_JSON_BYTES = 2048;

export function roomJson(body: object, status = 200): Response {
  return Response.json(body, {
    status,
    headers: {
      "Cache-Control": "no-store, max-age=0",
      "Pragma": "no-cache",
      "X-Content-Type-Options": "nosniff",
      "Referrer-Policy": "no-referrer",
      "Vary": "Origin",
    },
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
  // No exception messages or request bodies in logs, responses or telemetry.
  return roomJson({ version: 1, error: {
    code: "RTC_UNAVAILABLE", message: "room service unavailable", retryable: true,
  } }, 503);
}

/** Reject oversized streamed bodies BEFORE buffering a full user-supplied payload. */
export async function jsonBody(req: Request): Promise<unknown> {
  if (!(req.headers.get("content-type") ?? "").match(/^application\/json(?:\s*;|$)/i)) {
    throw new RoomError("INVALID_REQUEST", 400);
  }
  const length = Number(req.headers.get("content-length") ?? "0");
  if (!Number.isSafeInteger(length) || length < 0 || length > MAX_JSON_BYTES) {
    throw new RoomError("INVALID_REQUEST", 400);
  }
  if (!req.body) throw new RoomError("INVALID_REQUEST", 400);
  const reader = req.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      size += next.value.byteLength;
      if (size > MAX_JSON_BYTES) {
        await reader.cancel();
        throw new RoomError("INVALID_REQUEST", 400);
      }
      chunks.push(next.value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const part of chunks) { bytes.set(part, offset); offset += part.byteLength; }
    const raw = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    return JSON.parse(raw);
  } catch {
    throw new RoomError("INVALID_REQUEST", 400);
  } finally {
    reader.releaseLock();
  }
}

export function requireSameOrigin(req: Request, expected: string): void {
  const supplied = req.headers.get("origin");
  const site = req.headers.get("sec-fetch-site");
  // Reject browser cross-site requests even if the Origin header is suppressed.
  if (site && !["same-origin", "none"].includes(site)) {
    throw new RoomError("INVALID_ORIGIN", 403);
  }
  if (supplied && supplied !== expected) throw new RoomError("INVALID_ORIGIN", 403);
  // Requests without Origin and Fetch Metadata are accepted for non-browser
  // CLI clients, but still go through distributed abuse limits.
}
