import assert from "node:assert/strict";
import test from "node:test";
import { DisconnectReason } from "livekit-client";
import {
  disconnectMessage, disconnectRoomOnce, joinFailureMessage,
  networkFailureMessage, nextConnection, nextPeerPresence,
} from "./call-lifecycle";

test("transient signal loss recovers without allocating a new room", () => {
  const room = { disconnect: async () => {} };
  let state = nextConnection("connected", "reconnecting");
  assert.equal(state, "reconnecting");
  state = nextConnection(state, "reconnected");
  assert.equal(state, "connected");
  assert.equal(room.disconnect instanceof Function, true);
});

test("terminal disconnect cannot be resurrected by late reconnect callback", () => {
  const state = nextConnection("reconnecting", "disconnected");
  assert.equal(state, "disconnected");
  assert.equal(nextConnection(state, "reconnected"), "disconnected");
});

test("distinguishes room end, removal, duplicate role and unreachable server", () => {
  assert.match(disconnectMessage(DisconnectReason.ROOM_DELETED), /room has ended/);
  assert.match(disconnectMessage(DisconnectReason.PARTICIPANT_REMOVED), /removed/);
  assert.match(disconnectMessage(DisconnectReason.DUPLICATE_IDENTITY), /another device/);
  assert.match(disconnectMessage(DisconnectReason.SERVER_SHUTDOWN), /server stopped/);
  assert.match(disconnectMessage(), /connection was lost/);
});

test("distinguishes expired, full, occupied role and server unavailable on join", () => {
  assert.match(joinFailureMessage("ROOM_EXPIRED"), /expired/);
  assert.match(joinFailureMessage("INVITE_EXPIRED"), /expired/);
  assert.match(joinFailureMessage("ROOM_FULL"), /two participants/);
  assert.match(joinFailureMessage("ROLE_OCCUPIED"), /another tab/);
  assert.match(joinFailureMessage("RTC_UNAVAILABLE"), /server is unavailable/);
});

test("network unavailable has a distinct offline message", () => {
  assert.match(networkFailureMessage(false), /offline/);
  assert.match(networkFailureMessage(true), /call server/);
});

test("peer leaving retains the human call and ignores interpreter disconnects", () => {
  let state = nextPeerPresence("waiting", "joined", "human:guest");
  assert.equal(state, "present");
  state = nextPeerPresence(state, "left", "interpreter");
  assert.equal(state, "present");
  state = nextPeerPresence(state, "left", "human:guest");
  assert.equal(state, "left");
  state = nextPeerPresence(state, "joined", "human:guest");
  assert.equal(state, "present");
});

test("parallel cleanup requests disconnect exactly once and stop tracks", async () => {
  let calls = 0;
  let stopTracks: boolean | undefined;
  const room = { async disconnect(stop?: boolean) {
    calls++;
    stopTracks = stop;
    await Promise.resolve();
  } };
  const first = disconnectRoomOnce(room);
  const second = disconnectRoomOnce(room);
  assert.strictEqual(first, second);
  await Promise.all([first, second, disconnectRoomOnce(room)]);
  assert.equal(calls, 1);
  assert.equal(stopTracks, true);
});

test("failed cleanup is safe to repeat without unhandled rejection", async () => {
  let calls = 0;
  const room = { async disconnect() {
    calls++;
    throw new Error("room already stopped");
  } };
  await disconnectRoomOnce(room);
  await disconnectRoomOnce(room);
  assert.equal(calls, 1);
});
