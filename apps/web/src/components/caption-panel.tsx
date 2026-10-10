"use client";

import { Room, RoomEvent, type RemoteParticipant } from "livekit-client";
import { useEffect, useState } from "react";
import { CaptionStore, decodeCaption, type CaptionEvent } from "../lib/captions";
import { languageLabel, type SpokenLanguage } from "../lib/call";

function speakerLabel(speakerId: CaptionEvent["speakerId"]): string {
  return speakerId === "human:owner" ? "Owner" : "Guest";
}

export function CaptionPanel({
  room, language,
}: {
  room: Room;
  language: SpokenLanguage;
}) {
  const [enabled, setEnabled] = useState(true);
  const [store] = useState(() => new CaptionStore());
  const [captions, setCaptions] = useState<CaptionEvent[]>([]);

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
      const caption = decodeCaption(payload, {
        topic: topic ?? "",
        senderIdentity: participant?.identity,
        roomId: room.name,
        listenerLanguage: language,
      });
      if (caption && store.receive(caption)) setCaptions(store.snapshot());
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
    };
    room.on(RoomEvent.DataReceived, receive);
    room.on(RoomEvent.Reconnecting, invalidatePartials);
    room.on(RoomEvent.Disconnected, reset);
    return () => {
      active = false;
      room.off(RoomEvent.DataReceived, receive);
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
          setEnabled((value) => !value);
        }}>
        {enabled ? "Hide captions" : "Show captions"}
      </button>
    </header>

    {enabled && <>
      <div className="caption-overlay" role="status" aria-live="polite" aria-atomic="true">
        {latest ? <>
          <strong>{speakerLabel(latest.speakerId)} · {latest.isFinal ? "Final" : "Translating…"}</strong>
          <p lang={latest.targetLanguage}>
            {latest.translatedText || "Translating…"}
          </p>
        </> : <p>Translated captions will appear when the interpreter is available.</p>}
      </div>
      <div className="caption-history" aria-label="Conversation captions" role="log" aria-live="off">
        {captions.slice(-16).map((caption) =>
          <article key={caption.speakerId + ":" + caption.utteranceId}
            className={"caption-entry " + (caption.isFinal ? "final" : "partial")}>
            <div className="caption-entry-heading">
              <strong>{speakerLabel(caption.speakerId)}</strong>
              <span>{caption.isFinal ? "Final" : "Partial"}</span>
            </div>
            <p lang={caption.targetLanguage}>{caption.translatedText || "Translating…"}</p>
          </article>
        )}
      </div>
    </>}
  </section>;
}
