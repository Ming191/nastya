export type SpokenLanguage = "ru" | "vi";
export type ParticipantRole = "owner" | "guest";

export interface JoinResponse {
  version: 1;
  wsUrl: string;
  participantToken: string;
  participantRole: ParticipantRole;
  sourceLanguage: SpokenLanguage;
  expiresAt: string;
}

export interface BrowserSessionStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

const roomPattern = /^nastya_[0-9a-f]{32}$/;

export function isRoomId(value: string): boolean {
  return roomPattern.test(value);
}

export function readRoomInvite(
  roomId: string,
  hash: string,
  store: BrowserSessionStore,
): { token: string | null; clearFragment: boolean } {
  if (!isRoomId(roomId)) return { token: null, clearFragment: !!hash };
  const fragment = new URLSearchParams(hash.replace(/^#/, ""));
  const invitation = fragment.get("invite");
  if (invitation && invitation.length <= 1024) {
    store.setItem("nastya:guest:" + roomId, invitation);
    return { token: invitation, clearFragment: true };
  }
  return {
    token: store.getItem("nastya:guest:" + roomId) ??
      store.getItem("nastya:owner:" + roomId),
    clearFragment: !!hash,
  };
}

export function guestShareUrl(
  origin: string,
  roomId: string,
  store: BrowserSessionStore,
): string | null {
  if (!isRoomId(roomId)) return null;
  const ownerLink = store.getItem("nastya:share:" + roomId);
  if (ownerLink) {
    try {
      const url = new URL(ownerLink);
      if (url.origin === origin &&
        url.pathname === "/call/" + roomId &&
        url.hash.startsWith("#invite=")) return url.href;
    } catch {
      // Fall through to guest invite.
    }
  }
  const guestToken = store.getItem("nastya:guest:" + roomId);
  if (!guestToken) return null;
  return origin + "/call/" + roomId + "#invite=" + encodeURIComponent(guestToken);
}

export function isHumanIdentity(identity: string): boolean {
  return identity === "human:owner" || identity === "human:guest";
}

export function getMediaError(error: unknown, device: "microphone" | "camera"): string {
  const name = typeof error === "object" && error && "name" in error
    ? String(error.name) : "";
  if (name === "NotAllowedError" || name === "PermissionDeniedError" ||
    name === "SecurityError") {
    return "Access to the " + device + " was denied. Enable it in browser settings.";
  }
  if (name === "NotFoundError" || name === "DevicesNotFoundError" ||
    name === "OverconstrainedError") {
    return "No usable " + device + " was detected. Select another device.";
  }
  if (name === "NotReadableError" || name === "TrackStartError") {
    return "The " + device + " is busy or unavailable. Close other apps using it.";
  }
  return "Unable to use the " + device + ". Check the device and try again.";
}

export function languageLabel(language: SpokenLanguage): string {
  return language === "ru" ? "Russian" : "Vietnamese";
}
