"use client";

import { Room, RoomEvent, type RemoteParticipant } from "livekit-client";
import { useCallback, useEffect, useRef, useState } from "react";
import { CAPTION_TOPIC, decodeCaption } from "../lib/captions";
import { languageLabel, type SpokenLanguage } from "../lib/call";
import { decodeVoice, VOICE_TOPIC, VoiceAssembler, type VoiceAction } from "../lib/voice-packets";

type Status = "off" | "waiting" | "playing" | "blocked" | "unavailable";

export function TranslatedVoice({
  room, language,
}: {
  room: Room;
  language: SpokenLanguage;
}) {
  const [enabled, setEnabled] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [status, setStatus] = useState<Status>("off");
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const urlRef = useRef<string | null>(null);
  const speedRef = useRef(1);
  const serial = useRef(0);

  const stop = useCallback(() => {
    serial.current += 1;
    const player = audioRef.current;
    if (player) {
      player.pause();
      player.removeAttribute("src");
      player.load();
    }
    if (urlRef.current) {
      URL.revokeObjectURL(urlRef.current);
      urlRef.current = null;
    }
    setStatus((old) => old === "off" ? "off" : "waiting");
  }, []);

  const play = useCallback((bytes: Uint8Array) => {
    stop();
    const id = serial.current;
    const player = audioRef.current;
    if (!player) return;
    const url = URL.createObjectURL(new Blob([new Uint8Array(bytes)], { type: "audio/mpeg" }));
    urlRef.current = url;
    player.src = url;
    player.playbackRate = speedRef.current;
    player.onended = () => {
      if (id === serial.current) stop();
    };
    void player.play().then(() => {
      if (id === serial.current) setStatus("playing");
    }).catch(() => {
      if (id === serial.current) setStatus("blocked");
    });
  }, [stop]);

  useEffect(() => {
    if (!enabled) return;
    let active = true;
    const assembler = new VoiceAssembler();
    const handleAction = (action: VoiceAction) => {
      if (!active || !action) return;
      if (action.type === "stop") stop();
      else play(action.audio);
    };
    const onData = (
      payload: Uint8Array, participant?: RemoteParticipant,
      _kind?: unknown, topic?: string,
    ) => {
      if (!active) return;
      const envelope = {
        topic: topic ?? "", senderIdentity: participant?.identity,
        roomId: room.name, listenerLanguage: language,
      };
      if (envelope.topic === CAPTION_TOPIC) {
        const caption = decodeCaption(payload, envelope);
        if (caption) handleAction(assembler.noteCaption(caption));
      } else if (envelope.topic === VOICE_TOPIC) {
        const packet = decodeVoice(payload, envelope);
        if (packet) handleAction(assembler.accept(packet));
      }
    };
    const onDisconnect = () => {
      assembler.reset();
      stop();
    };
    room.on(RoomEvent.DataReceived, onData);
    room.on(RoomEvent.Reconnecting, onDisconnect);
    room.on(RoomEvent.Disconnected, onDisconnect);
    room.on(RoomEvent.Reconnected, onDisconnect);
    return () => {
      active = false;
      room.off(RoomEvent.DataReceived, onData);
      room.off(RoomEvent.Reconnecting, onDisconnect);
      room.off(RoomEvent.Disconnected, onDisconnect);
      room.off(RoomEvent.Reconnected, onDisconnect);
      assembler.reset();
      stop();
    };
  }, [room, language, enabled, play, stop]);

  async function enable() {
    // Explicit user interaction grants audio playback; do not auto-start from an event.
    const player = audioRef.current ?? new Audio();
    audioRef.current = player;
    try {
      await room.startAudio();
      setEnabled(true);
      setStatus("waiting");
      if (status === "blocked" && urlRef.current) {
        try {
          await player.play();
          setStatus("playing");
        } catch {
          setStatus("blocked");
        }
      }
    } catch {
      setStatus("blocked");
    }
  }

  function disable() {
    setEnabled(false);
    stop();
    setStatus("off");
  }

  function resume() {
    const player = audioRef.current;
    if (!player || !urlRef.current) return;
    void player.play().then(() => setStatus("playing")).catch(() => setStatus("blocked"));
  }

  return <section className="voice-controls" aria-label="Translated voice settings">
    <div>
      <h2>Translated voice</h2>
      <p>Optional · captions and the original call audio work independently.</p>
    </div>
    <button type="button" aria-pressed={enabled}
      onClick={() => { if (enabled) disable(); else void enable(); }}>
      {enabled ? "Mute translated voice" : "Enable translated voice"}
    </button>
    {enabled && <>
      <label className="device-field">
        <span>Voice speed</span>
        <select aria-label="Translated voice speed" value={speed}
          onChange={(event) => {
            const rate = Number(event.target.value);
            speedRef.current = rate;
            if (audioRef.current) audioRef.current.playbackRate = rate;
            setSpeed(rate);
          }}>
          <option value="0.85">0.85×</option>
          <option value="1">1×</option>
          <option value="1.15">1.15×</option>
        </select>
      </label>
      {status === "blocked" && <button type="button" onClick={resume}>
        Resume voice (browser blocked playback)
      </button>}
    </>}
    <p role="status">Voice: {status}. Target language: {languageLabel(language)}.</p>
  </section>;
}
