"use client";

import { Room } from "livekit-client";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { LiveCall } from "../../../components/live-call";
import {
  guestShareUrl, isRoomId, languageLabel, readRoomInvite,
  type JoinResponse, type SpokenLanguage,
} from "../../../lib/call";

export default function RoomEntryPage() {
  const { roomId } = useParams<{ roomId: string }>();
  const router = useRouter();
  const [language, setLanguage] = useState<SpokenLanguage>("ru");
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const [joined, setJoined] = useState<{ room: Room; participant: JoinResponse } | null>(null);
  const [canShare, setCanShare] = useState(false);
  const roomRef = useRef<Room | null>(null);
  const mounted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    if (!isRoomId(roomId)) {
      return () => { mounted.current = false; };
    }
    try {
      const current = new URL(window.location.href);
      const invite = readRoomInvite(roomId, current.hash, window.sessionStorage);
      if (invite.clearFragment) {
        window.history.replaceState(null, "", current.pathname + current.search);
      }
    } catch {
      // Session storage may be disabled by browser privacy settings.
      // Joining will show a useful error if session storage is not permitted.
    }
    return () => {
      mounted.current = false;
      const current = roomRef.current;
      roomRef.current = null;
      if (current) void current.disconnect(true);
    };
  }, [roomId]);

  async function join() {
    if (!isRoomId(roomId) || busy || roomRef.current) return;
    setBusy(true);
    setStatus("");
    let client: Room | null = null;
    try {
      const url = new URL(window.location.href);
      const capability = readRoomInvite(roomId, url.hash, window.sessionStorage);
      if (capability.clearFragment) {
        window.history.replaceState(null, "", url.pathname + url.search);
      }
      if (!capability.token) {
        setStatus("Room invitation missing or expired. Ask the host for a new link.");
        return;
      }
      const response = await fetch("/api/rooms/join", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          version: 1, roomId, invite: capability.token, preferredLanguage: language,
        }),
        cache: "no-store",
      });
      const payload: unknown = await response.json();
      if (!response.ok) {
        const code = typeof payload === "object" && payload && "error" in payload &&
          typeof payload.error === "object" && payload.error && "code" in payload.error
          ? String(payload.error.code) : "UNABLE_TO_JOIN";
        setStatus("Unable to join room: " + code.replaceAll("_", " ").toLowerCase());
        return;
      }
      const result = payload as JoinResponse;
      if (!result.wsUrl || !result.participantToken ||
        (result.participantRole !== "owner" && result.participantRole !== "guest")) {
        throw new Error("invalid server response");
      }
      // Keep the JWT only in this in-memory SDK instance; never store in URL/storage/DOM.
      client = new Room({
        adaptiveStream: true,
        dynacast: true,
        audioCaptureDefaults: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      roomRef.current = client;
      await client.connect(result.wsUrl, result.participantToken);
      if (!mounted.current) {
        await client.disconnect(true);
        return;
      }
      setCanShare(!!guestShareUrl(url.origin, roomId, window.sessionStorage));
      setJoined({ room: client, participant: result });
    } catch {
      if (client) {
        await client.disconnect(true);
        if (roomRef.current === client) roomRef.current = null;
      }
      if (mounted.current) {
        setStatus("Could not connect to the call. Check your network, invitation and LiveKit settings.");
      }
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  async function leave() {
    const current = roomRef.current;
    roomRef.current = null;
    if (current) await current.disconnect(true);
    setJoined(null);
    router.push("/");
  }

  async function copyInvite() {
    const link = guestShareUrl(window.location.origin, roomId, window.sessionStorage);
    if (!link) {
      setStatus("No guest invitation is available in this browser session.");
      return;
    }
    try {
      await navigator.clipboard.writeText(link);
      setStatus("Guest invitation copied.");
    } catch {
      setStatus("Clipboard unavailable. Copy the invitation from the home page.");
    }
  }

  if (joined) {
    return <LiveCall
      room={joined.room} role={joined.participant.participantRole}
      language={joined.participant.sourceLanguage} canShare={canShare}
      onLeave={leave} onCopyInvite={copyInvite}
    />;
  }
  return <main className="shell room-entry">
    <div className="eyebrow">NASTYA / PRIVATE ROOM</div>
    <h1>Join your video call</h1>
    <p>Select the language you speak. Camera and microphone permissions will be requested after joining.</p>
    <label className="device-field" htmlFor="language">Spoken language
      <select id="language" value={language}
        onChange={(event) => setLanguage(event.target.value as SpokenLanguage)}
        disabled={busy}>
        <option value="vi">Vietnamese</option>
        <option value="ru">Russian</option>
      </select>
    </label>
    <p>You will speak {languageLabel(language)}. Translation is configured separately.</p>
    <button type="button" onClick={() => void join()} disabled={busy || !isRoomId(roomId)}>
      {busy ? "Connecting…" : "Join video call"}
    </button>
    {status && <p role="alert">{status}</p>}
    <p><Link href="/">Back to home</Link></p>
  </main>;
}
