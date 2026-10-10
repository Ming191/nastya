import assert from "node:assert/strict";
import test from "node:test";
import {
  VoiceAssembler, decodeVoice, parseVoice, VOICE_TOPIC, CHUNK_BYTES, MAX_VOICE_BYTES,
} from "./voice-packets";
import type { CaptionEvent } from "./captions";

const roomId = "nastya_" + "a".repeat(32);
const caption: CaptionEvent = {
  version: 1, type: "caption.upsert", roomId,
  speakerId: "human:owner", utteranceId: "human:owner:1:1",
  revision: 1, sequence: 1,
  sourceLanguage: "vi", targetLanguage: "ru",
  sourceText: "Xin chào", translatedText: "Привет", isFinal: true,
  startOffsetMs: 0, endOffsetMs: 1200,
};
const base = {
  version: 1, roomId, speakerId: "human:owner", utteranceId: "human:owner:1:1",
  sequence: 1, targetLanguage: "ru",
};
const payload = (p: unknown) => new TextEncoder().encode(JSON.stringify(p));
const scope = { topic: VOICE_TOPIC, senderIdentity: "interpreter",
  roomId, listenerLanguage: "ru" as const };

function pack(binary: Uint8Array, id = base.utteranceId, seq = base.sequence) {
  const common = { ...base, utteranceId: id, sequence: seq };
  const start = { ...common, type: "voice.begin", mimeType: "audio/mpeg",
    byteLength: binary.length, chunkCount: Math.ceil(binary.length / CHUNK_BYTES) };
  const chunks = [];
  for (let i = 0; i < binary.length; i += CHUNK_BYTES) {
    const bytes = binary.slice(i, i + CHUNK_BYTES);
    chunks.push({ ...common, type: "voice.chunk", index: i / CHUNK_BYTES,
      data: Buffer.from(bytes).toString("base64") });
  }
  return [start, ...chunks, { ...common, type: "voice.end" }];
}

test("strict packet validation rejects spoofing, invalid rooms, directions, unknown fields", () => {
  const valid = pack(Uint8Array.from([255, 251, 12]));
  assert.ok(parseVoice(valid[0]));
  assert.ok(decodeVoice(payload(valid[0]), scope));
  for (const bad of [
    { ...valid[0], ignored: 123 },
    { ...valid[0], byteLength: 0 },
    { ...valid[0], chunkCount: 4 },
    { ...valid[0], byteLength: MAX_VOICE_BYTES + 1 },
    { ...valid[0], roomId: "bad" },
    { ...valid[0], utteranceId: "foo/bar" },
    { ...valid[1], data: "%" },
    { ...valid[1], index: -1 },
    { ...valid[1], index: 0.5 },
    { ...valid[0], version: 2 },
  ]) assert.equal(parseVoice(bad), null);
  for (const envelope of [
    { ...scope, senderIdentity: "human:guest" },
    { ...scope, senderIdentity: undefined },
    { ...scope, roomId: "nastya_" + "b".repeat(32) },
    { ...scope, listenerLanguage: "vi" as const },
    { ...scope, topic: "wrong-topic" },
  ]) assert.equal(decodeVoice(payload(valid[0]), envelope), null);
  assert.equal(decodeVoice(new Uint8Array(15 * 1024 + 1), scope), null);
  assert.equal(decodeVoice(new Uint8Array([255, 128]), scope), null);
});

test("voice requires trusted final caption and exact utterance ID", () => {
  const store = new VoiceAssembler();
  const mp3 = pack(Uint8Array.from([255, 251, 0, 1, 2]));
  assert.equal(store.accept(parseVoice(mp3[0])!), null);
  for (const packet of mp3.slice(1)) assert.equal(store.accept(parseVoice(packet)!), null);
  store.noteCaption({ ...caption, isFinal: false, translatedText: "" });
  assert.equal(store.accept(parseVoice(mp3[0])!), null);
  store.noteCaption(caption);
  const results = mp3.map((packet) => store.accept(parseVoice(packet)!));
  const last = results.at(-1);
  assert.equal(last?.type, "play");
  if (last?.type === "play") {
    assert.deepEqual(Array.from(last.audio), [255, 251, 0, 1, 2]);
  }
  assert.equal(store.accept(parseVoice(mp3[0])!), null); // no replay
});

test("multiple reliable chunks reassemble only in order within the bounded size", () => {
  const store = new VoiceAssembler();
  store.noteCaption(caption);
  const bytes = Uint8Array.from({ length: CHUNK_BYTES * 2 + 12 }, (_, i) => i % 255);
  const events = pack(bytes).map((packet) => parseVoice(packet)!);
  let outcome;
  for (const p of events) outcome = store.accept(p);
  assert.equal(outcome?.type, "play");
  if (outcome?.type === "play") assert.deepEqual(Buffer.from(outcome.audio), Buffer.from(bytes));

  const other = new VoiceAssembler();
  other.noteCaption(caption);
  other.accept(events[0]);
  other.accept(events[2]); // Missing first chunk: never assemble a partial stream.
  assert.equal(other.accept(events.at(-1)!), null);
});

test("new speech interrupts older playback and old sequence cannot restart", () => {
  const store = new VoiceAssembler();
  store.noteCaption(caption);
  const a = pack(Uint8Array.from([1, 2, 3])).map((p) => parseVoice(p)!);
  for (const p of a) store.accept(p);
  const newer: CaptionEvent = { ...caption, utteranceId: "human:owner:1:2",
    sequence: 2, revision: 0, translatedText: "", isFinal: false };
  assert.deepEqual(store.noteCaption(newer), {
    type: "stop", utteranceId: "human:owner|human:owner:1:1",
  });
  assert.equal(store.accept(a[0]), null);
  store.noteCaption({ ...newer, revision: 1, isFinal: true, translatedText: "Здравствуйте" });
  const b = pack(Uint8Array.from([5, 6]), newer.utteranceId, 2);
  assert.equal(store.accept(parseVoice(b[0])!), null);
  assert.equal(store.accept(parseVoice(b[1])!), null);
  assert.equal(store.accept(parseVoice(b[2])!)?.type, "play");
  assert.deepEqual(store.accept(parseVoice({
    ...base, type: "voice.cancel", utteranceId: newer.utteranceId, sequence: 2,
  })!), { type: "stop", utteranceId: "human:owner|human:owner:1:2" });
});

test("reconnect/disable resets inflight buffered audio, independent target languages", () => {
  const store = new VoiceAssembler();
  store.noteCaption(caption);
  store.accept(parseVoice(pack(Uint8Array.from([1, 2]))[0])!);
  store.reset();
  assert.equal(store.accept(parseVoice(pack(Uint8Array.from([1, 2]))[1])!), null);
  const ru: CaptionEvent = {
    ...caption, speakerId: "human:guest", utteranceId: "human:guest:1:1",
    targetLanguage: "vi", sourceLanguage: "ru",
  };
  store.noteCaption(ru);
  const p = pack(Uint8Array.from([1, 2]));
  const wrong = { ...p[0], speakerId: "human:guest", targetLanguage: "vi",
    utteranceId: ru.utteranceId };
  assert.ok(decodeVoice(payload(wrong), { ...scope, listenerLanguage: "vi" }));
  assert.equal(decodeVoice(payload(wrong), scope), null);
});


test("source-only final cannot grant voice authorization", () => {
  const assembler = new VoiceAssembler();
  const sourceOnly = {
    version: 1 as const, type: "caption.upsert" as const,
    roomId: "nastya_" + "a".repeat(32),
    speakerId: "human:owner" as const,
    utteranceId: "human:owner:2:12", revision: 1, sequence: 12,
    sourceLanguage: "vi" as const, targetLanguage: "ru" as const,
    sourceText: "Xin chào", translatedText: "Xin chào", translationState: "source_only" as const,
    isFinal: true, startOffsetMs: 0, endOffsetMs: 900,
  };
  assembler.noteCaption(sourceOnly);
  const begin = {
    version: 1 as const, type: "voice.begin" as const,
    roomId: sourceOnly.roomId, speakerId: sourceOnly.speakerId,
    utteranceId: sourceOnly.utteranceId, sequence: sourceOnly.sequence,
    targetLanguage: "ru" as const, mimeType: "audio/mpeg" as const,
    byteLength: 2, chunkCount: 1,
  };
  assert.equal(assembler.accept(begin), null);
  assert.equal(assembler.accept({
    ...begin, type: "voice.chunk", index: 0, data: "AAE=",
  } as const), null);
  assert.equal(assembler.accept({
    version: 1, type: "voice.end", roomId: begin.roomId,
    speakerId: begin.speakerId, utteranceId: begin.utteranceId,
    sequence: begin.sequence, targetLanguage: begin.targetLanguage,
  }), null);
});
