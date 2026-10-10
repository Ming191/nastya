"use client";

import { Room, RoomEvent, type RemoteParticipant } from "livekit-client";
import { useEffect, useState } from "react";
import { CaptionStore, decodeCaption, decodeInterpreterStatus, type CaptionEvent, type InterpreterStatusCode } from "../lib/captions";
import { languageLabel, type SpokenLanguage } from "../lib/call";

function speakerLabel(speakerId: CaptionEvent["speakerId"]): string {
  return speakerId === "human:owner" ? "Owner" : "Guest";
}

const originalOnly = (caption: CaptionEvent) => caption.translationState === "source_only";
const displayText = (caption: CaptionEvent) => caption.translationState === "pending"
  ? caption.sourceText : caption.translatedText || caption.sourceText;
const displayLanguage = (caption: CaptionEvent) =>
  originalOnly(caption) || caption.translationState === "pending"
    ? caption.sourceLanguage : caption.targetLanguage;
const captionState = (caption: CaptionEvent) => originalOnly(caption)
  ? "Original only · translation unavailable" : caption.isFinal ? "Translated" : "Original · translating";
const statusLabel = (status: InterpreterStatusCode) => ({
  ready: "Interpreter ready",
  stt_unavailable: "Speech recognition unavailable; original call continues",
  language_mismatch: "Speech language mismatch; check language selection",
  translation_unavailable: "Translation unavailable; showing original speech",
})[status];

export function CaptionPanel({
  room, language,
}: {
  room: Room;
  language: SpokenLanguage;
}) {
  const [enabled, setEnabled] = useState(true);
  const [store] = useState(() => new CaptionStore());
  const [captions, setCaptions] = useState<CaptionEvent[]>([]);
  const [interpreterStatus, setInterpreterStatus] = useState<InterpreterStatusCode | null>(null);

  useEffect(() => {
    if (!enabled) {
      store.clear();
      return;
    }
    let active = true;
    const receive = (
      payload: Uint8Array,
      participant?: RemoteParticipant,
      _kind?: unknown,
      topic?: string,
    ) => {
      if (!active) return;
      const envelope = {
        topic: topic ?? "", senderIdentity: participant?.identity,
        roomId: room.name, listenerLanguage: language,
      };
      const caption = decodeCaption(payload, envelope);
      if (caption && store.receive(caption)) setCaptions(store.snapshot());
      const status = decodeInterpreterStatus(payload, envelope);
      if (status) setInterpreterStatus(status.code);
    };
    const invalidatePartials = () => {
      if (!active) return;
      store.clearPartials();
      setCaptions(store.snapshot());
    };
    const reset = () => {
      if (!active) return;
      store.clear();
      setCaptions([]);
      setInterpreterStatus(null);
    };
    const interpreterLeft = (participant: { identity: string }) => {
      // The interpreter may restart with a fresh sequence counter.
      if (participant.identity === "interpreter") reset();
    };
    room.on(RoomEvent.DataReceived, receive);
    room.on(RoomEvent.ParticipantDisconnected, interpreterLeft);
    room.on(RoomEvent.Reconnecting, invalidatePartials);
    room.on(RoomEvent.Disconnected, reset);
    return () => {
      active = false;
      room.off(RoomEvent.DataReceived, receive);
      room.off(RoomEvent.ParticipantDisconnected, interpreterLeft);
      room.off(RoomEvent.Reconnecting, invalidatePartials);
      room.off(RoomEvent.Disconnected, reset);
      store.clear();
    };
  }, [room, language, enabled, store]);

  const latest = captions.at(-1);
  return <section className="captions-panel" aria-label="Live translated captions">
    <header className="captions-header">
      <div>
        <h2>Live captions</h2>
        <p>Translations into {languageLabel(language)} · This session only</p>
      </div>
      <button type="button" aria-pressed={enabled}
        aria-label={enabled ? "Hide captions" : "Show captions"}
        onClick={() => {
          store.clear();
          setCaptions([]);
          setInterpreterStatus(null);
          setEnabled((value) => !value);
        }}>
        {enabled ? "Hide captions" : "Show captions"}
      </button>
    </header>

    {enabled && <>
      <div className="caption-overlay" role="status" aria-live="polite" aria-atomic="true">
        {latest ? <>
          <strong>{speakerLabel(latest.speakerId)} · {captionState(latest)}</strong>
          <p lang={displayLanguage(latest)}>{displayText(latest)}</p>
        </> : <p>Translated captions will appear when the interpreter is available.</p>}
        {interpreterStatus && interpreterStatus !== "ready" &&
          <p role="status">{statusLabel(interpreterStatus)}</p>}
      </div>
      <div className="caption-history" aria-label="Conversation captions" role="log" aria-live="off">
        {captions.slice(-16).map((caption) =>
          <article key={caption.speakerId + ":" + caption.utteranceId}
            className={"caption-entry " + (caption.isFinal ? "final" : "partial")}>
            <div className="caption-entry-heading">
              <strong>{speakerLabel(caption.speakerId)}</strong>
              <span>{captionState(caption)}</span>
            </div>
            <p lang={displayLanguage(caption)}>{displayText(caption)}</p>
          </article>
        )}
      </div>
    </>}
  </section>;
}
