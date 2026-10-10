import assert from "node:assert/strict";
import test from "node:test";

import {
  getMediaError, guestShareUrl, isHumanIdentity, isRoomId,
  languageLabel, readRoomInvite,
} from "./call";

const roomId = "nastya_" + "a".repeat(32);
const origin = "https://app.example";
const link = origin + "/call/" + roomId + "#invite=guest_capability";

function store(seed: Record<string, string> = {}) {
  const data = new Map(Object.entries(seed));
  return {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => { data.set(key, value); },
  };
}

test("accepts only valid opaque room IDs", () => {
  assert.equal(isRoomId(roomId), true);
  assert.equal(isRoomId("../../admin"), false);
  assert.equal(isRoomId("nastya_" + "x".repeat(32)), false);
});

test("reads guest capability from fragment and keeps it only in tab session", () => {
  const session = store();
  assert.deepEqual(readRoomInvite(roomId, "#invite=guest_capability", session), {
    token: "guest_capability",
    clearFragment: true,
  });
  assert.deepEqual(readRoomInvite(roomId, "", session), {
    token: "guest_capability",
    clearFragment: false,
  });
  assert.equal(session.getItem("nastya:guest:" + roomId), "guest_capability");
});

test("owner token is not converted into a shareable guest token", () => {
  const session = store({ ["nastya:owner:" + roomId]: "PRIVATE_OWNER_INVITE" });
  assert.equal(guestShareUrl(origin, roomId, session), null);
  assert.equal(readRoomInvite(roomId, "", session).token, "PRIVATE_OWNER_INVITE");
});

test("owner guest link can be copied without including owner credential", () => {
  const session = store({
    ["nastya:owner:" + roomId]: "PRIVATE_OWNER_INVITE",
    ["nastya:share:" + roomId]: link,
  });
  assert.equal(guestShareUrl(origin, roomId, session), link);
  assert.equal(guestShareUrl("https://other.example", roomId, session), null);
});

test("guest may re-share their own invite, never a cross-origin stored URL", () => {
  const session = store({
    ["nastya:guest:" + roomId]: "guest_capability",
    ["nastya:share:" + roomId]: "https://attacker.test/call/" + roomId + "#invite=bad",
  });
  assert.equal(guestShareUrl(origin, roomId, session), link);
});

test("filters interpreter tracks and labels languages", () => {
  assert.equal(isHumanIdentity("human:owner"), true);
  assert.equal(isHumanIdentity("human:guest"), true);
  assert.equal(isHumanIdentity("interpreter"), false);
  assert.equal(isHumanIdentity("human:unknown"), false);
  assert.equal(languageLabel("ru"), "Russian");
  assert.equal(languageLabel("vi"), "Vietnamese");
});

test("returns usable device permission and availability errors", () => {
  assert.match(getMediaError({ name: "NotAllowedError" }, "camera"), /denied/);
  assert.match(getMediaError({ name: "NotFoundError" }, "microphone"), /No usable/);
  assert.match(getMediaError({ name: "NotReadableError" }, "camera"), /busy/);
  assert.match(getMediaError(new Error("private user text"), "camera"), /Unable to use/);
  assert.ok(!getMediaError(new Error("private user text"), "camera").includes("private"));
});
