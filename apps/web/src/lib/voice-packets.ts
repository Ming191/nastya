import type { CaptionEvent } from "./captions";
import type { SpokenLanguage } from "./call";

export const VOICE_TOPIC = "nastya.voice.v1";
export const MAX_VOICE_PACKET = 15 * 1024;
export const MAX_VOICE_BYTES = 384 * 1024;
export const CHUNK_BYTES = 8 * 1024;
export const MAX_CHUNKS = Math.ceil(MAX_VOICE_BYTES / CHUNK_BYTES);
const ROOM_RE = /^nastya_[a-f0-9]{32}$/;
const UTTERANCE_RE = /^[A-Za-z0-9:_-]{1,96}$/;
const SPEAKERS = ["human:owner", "human:guest"];
type Speaker = CaptionEvent["speakerId"];

type Base = {
  version: 1;
  roomId: string;
  speakerId: Speaker;
  utteranceId: string;
  sequence: number;
  targetLanguage: SpokenLanguage;
};
export type VoicePacket = Base & (
  { type: "voice.begin"; mimeType: "audio/mpeg"; byteLength: number; chunkCount: number } |
  { type: "voice.chunk"; index: number; data: string } |
  { type: "voice.end" } |
  { type: "voice.cancel" }
);
export type VoiceEnvelope = {
  topic: string;
  senderIdentity?: string;
  roomId: string;
  listenerLanguage: SpokenLanguage;
};
const int = (x: unknown, max: number): x is number =>
  typeof x === "number" && Number.isSafeInteger(x) && x >= 0 && x <= max;

export function parseVoice(value: unknown): VoicePacket | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const v = value as Record<string, unknown>;
  const base = ["version", "type", "roomId", "speakerId", "utteranceId",
    "sequence", "targetLanguage"];
  const op = v.type;
  const extra = op === "voice.begin" ? ["mimeType", "byteLength", "chunkCount"] :
    op === "voice.chunk" ? ["index", "data"] :
      op === "voice.end" || op === "voice.cancel" ? [] : null;
  if (extra === null || Object.keys(v).length !== base.length + extra.length ||
    [...base, ...extra].some((key) => !Object.hasOwn(v, key))) return null;
  if (v.version !== 1 || typeof v.roomId !== "string" || !ROOM_RE.test(v.roomId) ||
    typeof v.speakerId !== "string" || !SPEAKERS.includes(v.speakerId) ||
    typeof v.utteranceId !== "string" || !UTTERANCE_RE.test(v.utteranceId) ||
    !int(v.sequence, 1_000_000_000) ||
    (v.targetLanguage !== "vi" && v.targetLanguage !== "ru")) return null;
  if (op === "voice.begin" && (v.mimeType !== "audio/mpeg" ||
    !int(v.byteLength, MAX_VOICE_BYTES) || v.byteLength === 0 ||
    !int(v.chunkCount, MAX_CHUNKS) || v.chunkCount === 0 ||
    v.chunkCount !== Math.ceil(v.byteLength / CHUNK_BYTES))) return null;
  if (op === "voice.chunk" && (!int(v.index, MAX_CHUNKS - 1) ||
    typeof v.data !== "string" || v.data.length === 0 ||
    v.data.length > Math.ceil(CHUNK_BYTES / 3) * 4 ||
    !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(v.data))) {
    return null;
  }
  return v as VoicePacket;
}

export function decodeVoice(data: Uint8Array, envelope: VoiceEnvelope): VoicePacket | null {
  if (envelope.topic !== VOICE_TOPIC || envelope.senderIdentity !== "interpreter" ||
    data.byteLength > MAX_VOICE_PACKET) return null;
  let value: unknown;
  try {
    value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(data));
  } catch {
    return null;
  }
  const packet = parseVoice(value);
  return packet && packet.roomId === envelope.roomId &&
    packet.targetLanguage === envelope.listenerLanguage ? packet : null;
}

type Active = {
  packet: VoicePacket & { type: "voice.begin" };
  chunks: Uint8Array[];
  bytes: number;
  nextIndex: number;
  since: number;
};
export type VoiceAction =
  | { type: "stop"; utteranceId: string }
  | { type: "play"; utteranceId: string; audio: Uint8Array }
  | null;

/**
 * Latest-utterance wins, one inflight assembly, no persisted voice.
 * Only accepts audio matching a trusted final caption from the interpreter.
 */
export class VoiceAssembler {
  private approved = new Map<string, number>();
  private seen = new Set<string>();
  private sequence = new Map<Speaker, number>();
  private active: Active | null = null;
  private current: string | null = null;

  noteCaption(caption: CaptionEvent): VoiceAction {
    const key = caption.speakerId + "|" + caption.utteranceId;
    const latest = this.sequence.get(caption.speakerId) ?? -1;
    if (caption.sequence < latest) return null;
    if (caption.sequence > latest) {
      this.sequence.set(caption.speakerId, caption.sequence);
      if (this.current && this.current !== key) {
        const old = this.current;
        this.current = null;
        this.active = null;
        if (caption.isFinal) this.approve(key, caption.sequence);
        return { type: "stop", utteranceId: old };
      }
    }
    if (caption.isFinal) this.approve(key, caption.sequence);
    return null;
  }

  private approve(key: string, sequence: number): void {
    this.approved.delete(key);
    this.approved.set(key, sequence);
    if (this.approved.size > 128) this.approved.delete(this.approved.keys().next().value!);
  }

  accept(packet: VoicePacket, now = Date.now()): VoiceAction {
    const key = packet.speakerId + "|" + packet.utteranceId;
    if (packet.type === "voice.cancel") {
      if (this.current !== key) return null;
      this.active = null;
      this.current = null;
      this.markSeen(key);
      return { type: "stop", utteranceId: key };
    }
    if (packet.type === "voice.begin") {
      if (this.approved.get(key) !== packet.sequence || this.seen.has(key) ||
        packet.sequence < (this.sequence.get(packet.speakerId) ?? -1)) return null;
      const previous = this.current;
      this.current = key;
      this.active = { packet, chunks: [], bytes: 0, nextIndex: 0, since: now };
      return previous ? { type: "stop", utteranceId: previous } : null;
    }
    const active = this.active;
    if (!active || this.current !== key ||
      active.packet.sequence !== packet.sequence || now - active.since > 12_000) {
      if (active && now - active.since > 12_000) this.active = null;
      return null;
    }
    if (packet.type === "voice.chunk") {
      if (packet.index !== active.nextIndex) {
        this.active = null;
        return null;
      }
      let decoded: Uint8Array;
      try {
        const binary = atob(packet.data);
        decoded = Uint8Array.from(binary, (char) => char.charCodeAt(0));
      } catch {
        this.active = null;
        return null;
      }
      if (!decoded.length || decoded.length > CHUNK_BYTES ||
        active.bytes + decoded.length > active.packet.byteLength) {
        this.active = null;
        return null;
      }
      active.chunks.push(decoded);
      active.bytes += decoded.length;
      active.nextIndex++;
      return null;
    }
    if (active.nextIndex !== active.packet.chunkCount ||
      active.bytes !== active.packet.byteLength) {
      this.active = null;
      return null;
    }
    const audio = new Uint8Array(active.bytes);
    let index = 0;
    for (const chunk of active.chunks) {
      audio.set(chunk, index);
      index += chunk.length;
    }
    this.active = null;
    this.markSeen(key);
    return { type: "play", utteranceId: key, audio };
  }

  private markSeen(key: string): void {
    this.seen.add(key);
    if (this.seen.size > 128) this.seen.delete(this.seen.values().next().value!);
  }

  reset(): void {
    this.active = null;
    this.current = null;
    this.approved.clear();
    this.seen.clear();
    this.sequence.clear();
  }
}
