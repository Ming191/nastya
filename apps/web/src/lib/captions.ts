import type { SpokenLanguage } from "./call";

export const CAPTION_TOPIC = "nastya.caption.v1";
const ROOM_ID = /^nastya_[a-f0-9]{32}$/;
const UTTERANCE_ID = /^[A-Za-z0-9:_-]{1,96}$/;
const SPEAKERS = ["human:owner", "human:guest"] as const;
const LANGUAGES = ["ru", "vi"] as const;
export const MAX_CAPTION_BYTES = 12_000;
export const MAX_CAPTIONS = 32;
const MAX_SEEN = 256;

export interface CaptionEvent {
  version: 1;
  type: "caption.upsert";
  roomId: string;
  speakerId: "human:owner" | "human:guest";
  utteranceId: string;
  revision: number;
  sequence: number;
  sourceLanguage: SpokenLanguage;
  targetLanguage: SpokenLanguage;
  sourceText: string;
  translatedText: string;
  isFinal: boolean;
  startOffsetMs: number;
  endOffsetMs: number;
}

export type CaptionEnvelope = {
  topic: string;
  senderIdentity: string | undefined;
  roomId: string;
  listenerLanguage: SpokenLanguage;
};

const boundedInt = (value: unknown, max: number): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0 && value <= max;
const isLanguage = (value: unknown): value is SpokenLanguage =>
  typeof value === "string" && LANGUAGES.some((language) => language === value);

export function parseCaption(value: unknown): CaptionEvent | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const p = value as Record<string, unknown>;
  const names: Array<keyof CaptionEvent> = [
    "version", "type", "roomId", "speakerId", "utteranceId", "revision", "sequence",
    "sourceLanguage", "targetLanguage", "sourceText", "translatedText", "isFinal",
    "startOffsetMs", "endOffsetMs",
  ];
  if (Object.keys(p).length !== names.length ||
      names.some((name) => !Object.prototype.hasOwnProperty.call(p, name))) return null;
  if (p.version !== 1 || p.type !== "caption.upsert" ||
      typeof p.roomId !== "string" || !ROOM_ID.test(p.roomId) ||
      typeof p.speakerId !== "string" || !SPEAKERS.some((speaker) => speaker === p.speakerId) ||
      typeof p.utteranceId !== "string" || !UTTERANCE_ID.test(p.utteranceId) ||
      !boundedInt(p.revision, 1_000_000) || !boundedInt(p.sequence, 1_000_000_000) ||
      !isLanguage(p.sourceLanguage) || !isLanguage(p.targetLanguage) ||
      p.sourceLanguage === p.targetLanguage || typeof p.sourceText !== "string" ||
      p.sourceText.length === 0 || p.sourceText.length > 2000 ||
      typeof p.translatedText !== "string" || p.translatedText.length > 2000 ||
      typeof p.isFinal !== "boolean" ||
      (p.isFinal && p.translatedText.length === 0) ||
      !boundedInt(p.startOffsetMs, 86_400_000) ||
      !boundedInt(p.endOffsetMs, 86_400_000) ||
      p.endOffsetMs < p.startOffsetMs ||
      p.endOffsetMs - p.startOffsetMs > 30_000) return null;
  return p as unknown as CaptionEvent;
}

export function decodeCaption(data: Uint8Array, envelope: CaptionEnvelope): CaptionEvent | null {
  if (envelope.topic !== CAPTION_TOPIC || envelope.senderIdentity !== "interpreter" ||
      data.byteLength > MAX_CAPTION_BYTES) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(data));
  } catch {
    return null;
  }
  const caption = parseCaption(parsed);
  return caption && caption.roomId === envelope.roomId &&
    caption.targetLanguage === envelope.listenerLanguage ? caption : null;
}

/** Ephemeral in-memory state. Never writes to storage or network. */
export class CaptionStore {
  private captions = new Map<string, CaptionEvent>();
  private seen = new Map<string, { revision: number; final: boolean }>();
  private sequences = new Map<string, number>();

  receive(next: CaptionEvent): boolean {
    const key = next.speakerId + "|" + next.utteranceId;
    const latest = this.seen.get(key);
    if (latest?.final || (latest && next.revision <= latest.revision)) return false;
    const current = this.captions.get(key);
    if (current && (
      current.sequence !== next.sequence ||
      current.sourceLanguage !== next.sourceLanguage ||
      current.targetLanguage !== next.targetLanguage ||
      current.startOffsetMs !== next.startOffsetMs
    )) return false;
    const lastSequence = this.sequences.get(next.speakerId) ?? -1;
    if (!latest && next.sequence <= lastSequence) return false;
    this.sequences.set(next.speakerId, Math.max(lastSequence, next.sequence));
    if (this.seen.has(key)) this.seen.delete(key);
    this.seen.set(key, { revision: next.revision, final: next.isFinal });
    if (this.seen.size > MAX_SEEN) this.seen.delete(this.seen.keys().next().value!);
    this.captions.delete(key);
    this.captions.set(key, next);
    while (this.captions.size > MAX_CAPTIONS) {
      this.captions.delete(this.captions.keys().next().value!);
    }
    return true;
  }

  snapshot(): CaptionEvent[] {
    // Each speaker has an independent sample clock; arrival order is the
    // only safe ordering across speakers. Updated revisions move to the end.
    return [...this.captions.values()];
  }

  clearPartials(): void {
    for (const [key, event] of this.captions) {
      if (!event.isFinal) this.captions.delete(key);
    }
    // Retain seen revisions to reject stale partial events after reconnect.
  }

  clear(): void {
    this.captions.clear();
    this.seen.clear();
    this.sequences.clear();
  }

  get size(): number {
    return this.captions.size;
  }
}
