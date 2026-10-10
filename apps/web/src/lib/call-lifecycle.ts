import { DisconnectReason } from "livekit-client";

export type CallConnection = "connected" | "reconnecting" | "disconnected";
export type ConnectionEvent = "reconnecting" | "reconnected" | "disconnected";

export function nextConnection(
  current: CallConnection,
  event: ConnectionEvent,
): CallConnection {
  // A terminal disconnect cannot be reversed by late SDK reconnect callbacks.
  if (current === "disconnected") return "disconnected";
  if (event === "disconnected") return "disconnected";
  return event === "reconnected" ? "connected" : "reconnecting";
}

export function disconnectMessage(reason?: DisconnectReason): string {
  switch (reason) {
    case DisconnectReason.ROOM_DELETED:
      return "This room has ended. Create a new room to call again.";
    case DisconnectReason.PARTICIPANT_REMOVED:
      return "You were removed from this room. Ask the host for a new invitation.";
    case DisconnectReason.DUPLICATE_IDENTITY:
      return "This role joined from another device or tab. Close the other call before retrying.";
    case DisconnectReason.SERVER_SHUTDOWN:
      return "The call server stopped or restarted. Try joining again.";
    case DisconnectReason.CLIENT_INITIATED:
      return "You left the call.";
    case DisconnectReason.JOIN_FAILURE:
      return "The call server rejected the connection. Verify your invitation and try again.";
    default:
      return "The connection was lost and could not recover. Check your network and try again.";
  }
}

export function joinFailureMessage(code: string): string {
  switch (code) {
    case "INVITE_EXPIRED":
    case "ROOM_EXPIRED":
      return "This invitation or room has expired. Ask the host to create a new room.";
    case "INVALID_INVITE":
    case "INVALID_REQUEST":
    case "INVALID_ORIGIN":
      return "This invitation is invalid. Open the original invite link again.";
    case "ROOM_FULL":
      return "This room already has two participants.";
    case "ROLE_OCCUPIED":
      return "This invitation is already in use in another tab or device.";
    case "RATE_LIMITED":
      return "Too many attempts. Try again shortly.";
    case "ROOM_SERVICE_NOT_CONFIGURED":
    case "RTC_UNAVAILABLE":
      return "The room server is unavailable. Please try again later.";
    default:
      return "Unable to join the room. Verify your invitation and try again.";
  }
}

export function networkFailureMessage(online: boolean): string {
  return online
    ? "Cannot reach the call server. Check your connection and try again."
    : "You appear to be offline. Reconnect to the internet and try again.";
}

export type PeerPresence = "waiting" | "present" | "left";

export function nextPeerPresence(
  current: PeerPresence,
  event: "joined" | "left" | "reset",
  identity: string,
): PeerPresence {
  // Ignore events for AI participants, never mistake model downtime for peer departure.
  if (identity !== "human:owner" && identity !== "human:guest") return current;
  if (event === "joined") return "present";
  if (event === "left") return "left";
  return "waiting";
}

/** Duck-typed so the lifecycle can be unit tested without a WebRTC connection. */
export interface DisconnectableRoom {
  disconnect(stopTracks?: boolean): Promise<void>;
}

const closing = new WeakMap<DisconnectableRoom, Promise<void>>();

/** Idempotent cleanup for leave, pagehide, unmount and late media acquisition races. */
export function disconnectRoomOnce(room: DisconnectableRoom): Promise<void> {
  const existing = closing.get(room);
  if (existing) return existing;
  const task = Promise.resolve()
    .then(() => room.disconnect(true))
    .catch(() => {
      // Connection already closed/failed: the cleanup must not block navigation.
    });
  closing.set(room, task);
  return task;
}
