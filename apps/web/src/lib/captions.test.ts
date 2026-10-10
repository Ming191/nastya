import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { CaptionStore, decodeCaption, parseCaption, MAX_CAPTIONS, CAPTION_TOPIC, type CaptionEvent } from "./captions";

const ROOM = "nastya_" + "a".repeat(32);
const base: CaptionEvent = {
  version: 1, type: "caption.upsert", roomId: ROOM,
  speakerId: "human:owner", utteranceId: "human:owner:4:1",
  revision: 0, sequence: 1,
  sourceLanguage: "vi", targetLanguage: "ru",
  sourceText: "Xin chào", translatedText: "", isFinal: false,
  startOffsetMs: 40, endOffsetMs: 740,
};
const data = (event: unknown): Uint8Array => new TextEncoder().encode(JSON.stringify(event));
const envelope = {
  topic: CAPTION_TOPIC, senderIdentity: "interpreter",
  roomId: ROOM, listenerLanguage: "ru" as const,
};

test("canonical shared JSON schema agrees on v1 field names and ranges", () => {
  const schema = JSON.parse(readFileSync(
    new URL("../../../../protocol/caption.v1.schema.json", import.meta.url), "utf8",
  ));
  assert.deepEqual(schema.required.slice().sort(), Object.keys(base).sort());
  assert.equal(schema.additionalProperties, false);
  assert.equal(schema.properties.sourceText.maxLength, 2000);
  assert.equal(schema.properties.translatedText.maxLength, 2000);
  assert.equal(schema.properties.revision.maximum, 1_000_000);
  assert.ok(parseCaption(base));
});

test("reject malformed extra fields, oversized text, offset anomalies and wrong direction", () => {
  const bad = [
    { ...base, unknown: true }, { ...base, revision: -1 },
    { ...base, revision: 1.2 }, { ...base, revision: Infinity },
    { ...base, sourceText: "" }, { ...base, translatedText: "x".repeat(2001) },
    { ...base, roomId: "attacker" }, { ...base, utteranceId: "bad/string" },
    { ...base, sourceLanguage: "ru" }, { ...base, speakerId: "interpreter" },
    { ...base, isFinal: true }, { ...base, startOffsetMs: 1000, endOffsetMs: 500 },
    { ...base, endOffsetMs: 45000 }, { ...base, version: 2 },
  ];
  for (const e of bad) assert.equal(parseCaption(e), null);
});

test("reject spoofed sender, wrong topic, room and language and invalid UTF-8", () => {
  const valid = data(base);
  assert.ok(decodeCaption(valid, envelope));
  for (const info of [
    { ...envelope, senderIdentity: "human:owner" },
    { ...envelope, senderIdentity: undefined },
    { ...envelope, topic: "wrong" },
    { ...envelope, roomId: "nastya_" + "b".repeat(32) },
    { ...envelope, listenerLanguage: "vi" as const },
  ]) assert.equal(decodeCaption(valid, info), null);
  assert.equal(decodeCaption(new Uint8Array([255, 128]), envelope), null);
  assert.equal(decodeCaption(data(base).slice(0, 4), envelope), null);
  assert.equal(decodeCaption(new Uint8Array(12001), envelope), null);
});

test("partial revisions update in place, duplicate or delayed partial cannot overwrite final", () => {
  const store = new CaptionStore();
  assert.equal(store.receive(base), true);
  assert.equal(store.receive({ ...base, revision: 1, translatedText: "Привет" }), true);
  assert.equal(store.size, 1);
  assert.equal(store.receive({ ...base, revision: 1, translatedText: "duplicated" }), false);
  const final = { ...base, revision: 2, translatedText: "Здравствуйте", isFinal: true };
  assert.equal(store.receive(final), true);
  assert.equal(store.receive({ ...base, revision: 3, isFinal: false }), false);
  assert.equal(store.receive({ ...base, revision: 4, isFinal: true, translatedText: "tampered" }), false);
  assert.equal(store.snapshot()[0].translatedText, "Здравствуйте");
});

test("two simultaneous humans retain separate utterance identity and origin languages", () => {
  const store = new CaptionStore();
  const reply: CaptionEvent = {
    ...base, speakerId: "human:guest", utteranceId: "human:guest:5:1",
    sourceLanguage: "ru", targetLanguage: "vi",
    translatedText: "Xin chào", isFinal: true,
  };
  assert.equal(store.receive(base), true);
  assert.equal(store.receive(reply), true);
  assert.equal(store.snapshot().length, 2);
  assert.ok(store.snapshot().some((e) => e.speakerId === "human:guest"));
  assert.ok(store.snapshot().some((e) => e.speakerId === "human:owner"));
});

test("reject replayed old utterance after receiving higher sequence", () => {
  const store = new CaptionStore();
  assert.equal(store.receive(base), true);
  assert.equal(store.receive({ ...base, utteranceId: "new", sequence: 2 }), true);
  assert.equal(store.receive({ ...base, utteranceId: "old", sequence: 1 }), false);
  assert.equal(store.receive({ ...base, utteranceId: "new", sequence: 5, revision: 2 }), false);
  assert.equal(store.receive({ ...base, revision: 2 }), true);
});

test("bounded memory, cleared partials and sealed final tombstones survive reconnect", () => {
  const store = new CaptionStore();
  for (let i = 0; i < MAX_CAPTIONS + 20; i++) {
    assert.equal(store.receive({ ...base, utteranceId: "u_" + i, sequence: i }), true);
  }
  assert.equal(store.size, MAX_CAPTIONS);
  const finished = { ...base, utteranceId: "done", sequence: 100, revision: 1,
    translatedText: "финал", isFinal: true };
  assert.equal(store.receive(finished), true);
  store.clearPartials();
  assert.equal(store.size, 1);
  assert.equal(store.receive({ ...finished, revision: 3, isFinal: false }), false);
  store.clear();
  assert.equal(store.size, 0);
  assert.equal(store.receive(base), true);
});
