"use client";

import { useParams } from "next/navigation";
import { useState } from "react";

export default function RoomEntryPage() {
  const { roomId } = useParams<{ roomId: string }>();
  const [language, setLanguage] = useState<"ru" | "vi">("ru");
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);

  async function authorize() {
    setBusy(true);
    setStatus("");
    try {
      const url = new URL(window.location.href);
      const fragment = new URLSearchParams(url.hash.slice(1));
      const inviteFromLink = fragment.get("invite");
      if (inviteFromLink) {
        window.history.replaceState(null, "", url.pathname + url.search);
      }
      const invite = inviteFromLink ??
        window.sessionStorage.getItem("nastya:owner:" + roomId);
      if (!invite) throw new Error("Missing room invitation");
      const response = await fetch("/api/rooms/join", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ version: 1, roomId, invite, preferredLanguage: language }),
        cache: "no-store",
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error?.code ?? "Unable to join");
      // Keep participant JWT out of the URL, DOM, and persistent browser storage.
      // RTC connection consumes this response in the video-call integration.
      setStatus("Room authorized as " + result.participantRole + ". Media controls are not connected yet.");
    } catch (cause) {
      setStatus(cause instanceof Error ? cause.message : "Unable to authorize");
    } finally {
      setBusy(false);
    }
  }
  return <main className="shell">
    <div className="eyebrow">PRIVATE ROOM</div>
    <h1>Join your call</h1>
    <p>Choose your spoken language before joining.</p>
    <label htmlFor="language">My language</label>{" "}
    <select id="language" value={language}
      onChange={(event) => setLanguage(event.target.value as "vi" | "ru")}>
      <option value="ru">Russian</option>
      <option value="vi">Vietnamese</option>
    </select>
    <p><button onClick={authorize} disabled={busy}>{busy ? "Connecting..." : "Authorize room access"}</button></p>
    {status && <p role="status">{status}</p>}
    <p><a href="/">Back to home</a></p>
  </main>;
}
