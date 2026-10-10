"use client";

import Link from "next/link";
import { useState } from "react";

type CreatedRoom = { roomId: string; ownerInvite: string; guestUrl: string };
export default function HomePage() {
  const [room, setRoom] = useState<CreatedRoom | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);

  async function create() {
    setBusy(true);
    setError("");
    setCopied(false);
    try {
      const response = await fetch("/api/rooms", { method: "POST", cache: "no-store" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error?.code ?? "Unable to create room");
      const created = payload as CreatedRoom;
      window.sessionStorage.setItem("nastya:owner:" + created.roomId, created.ownerInvite);
      window.sessionStorage.setItem("nastya:share:" + created.roomId, created.guestUrl);
      setRoom(created);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Unable to create room");
    } finally {
      setBusy(false);
    }
  }

  async function copyInvite() {
    if (!room) return;
    try {
      await navigator.clipboard.writeText(room.guestUrl);
      setCopied(true);
    } catch {
      setError("Clipboard unavailable. Select and copy the invitation link instead.");
    }
  }

  return <main className="shell">
    <div className="eyebrow">NASTYA</div>
    <h1>Speak your language. Stay connected.</h1>
    <p>Private video calls for Russian and Vietnamese speakers.</p>
    <button type="button" onClick={() => void create()} disabled={busy}>
      {busy ? "Creating…" : "Create a private room"}
    </button>
    {error && <p role="alert">{error}</p>}
    {room && <section aria-label="Room invitation">
      <p>Share this invitation with the other participant:</p>
      <p><a href={room.guestUrl}>{room.guestUrl}</a></p>
      <button type="button" onClick={() => void copyInvite()}>
        {copied ? "Copied" : "Copy invite link"}
      </button>
      <p><Link href={"/call/" + room.roomId}>Continue as room owner</Link></p>
    </section>}
    <p>Invitations expire after one hour. Room access uses temporary tokens.</p>
  </main>;
}
