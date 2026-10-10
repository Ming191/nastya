import { expect, test } from "@playwright/test";
import { createCallPair, joinCall, waitForRemoteMedia } from "./support/call-fixture";
import { attachRtcReport, collectRtcStats } from "./support/rtc-stats";

test("two isolated browser sessions exchange camera and microphone media", async ({ browser }, info) => {
  const pair = await createCallPair(browser);
  try {
    await joinCall(pair.owner, "vi");
    await joinCall(pair.guest, "ru");
    await Promise.all([waitForRemoteMedia(pair.owner), waitForRemoteMedia(pair.guest)]);

    const ownerStats = await collectRtcStats(pair.owner);
    const guestStats = await collectRtcStats(pair.guest);
    expect(ownerStats.length).toBeGreaterThan(0);
    expect(guestStats.length).toBeGreaterThan(0);
    expect(ownerStats.flatMap((pc) => pc.selectedPairs).length).toBeGreaterThan(0);
    expect(guestStats.flatMap((pc) => pc.selectedPairs).length).toBeGreaterThan(0);
    expect(ownerStats.flatMap((pc) => pc.inbound)
      .some((stream) => stream.kind === "video" && stream.bytesReceived > 0)).toBe(true);
    expect(guestStats.flatMap((pc) => pc.inbound)
      .some((stream) => stream.kind === "audio" && stream.packetsReceived > 0)).toBe(true);

    await attachRtcReport(info, [
      { name: "owner-vi", page: pair.owner },
      { name: "guest-ru", page: pair.guest },
    ]);

    await pair.owner.getByRole("button", { name: "Mute mic" }).click();
    await expect(pair.owner.getByRole("button", { name: "Turn mic on" })).toBeVisible();
    await pair.owner.getByRole("button", { name: "Turn mic on" }).click();
    await expect(pair.owner.getByRole("button", { name: "Mute mic" })).toBeVisible();

    await pair.guest.getByRole("button", { name: "Turn camera off" }).click();
    await expect(pair.guest.getByText("Your camera is off")).toBeVisible();
    await pair.guest.getByRole("button", { name: "Turn camera on" }).click();
    await waitForRemoteMedia(pair.owner);

    // Record the actual local tracks so cleanup is checked after SPA navigation.
    await pair.owner.evaluate(() => {
      const el = document.querySelector("video[aria-label='You video']") as HTMLVideoElement | null;
      const stream = el?.srcObject as MediaStream | null;
      (window as Window & { __testCapturedTracks?: MediaStreamTrack[] }).__testCapturedTracks =
        stream?.getTracks() ?? [];
    });
    await pair.owner.getByRole("button", { name: "Leave call" }).click();
    await expect(pair.owner.getByRole("button", { name: "Create a private room" })).toBeVisible();
    await expect.poll(() => pair.owner.evaluate(() =>
      ((window as Window & { __testCapturedTracks?: MediaStreamTrack[] }).__testCapturedTracks ?? [])
        .every((track) => track.readyState === "ended"),
    )).toBe(true);

    await expect(pair.guest.getByText("The other person left the call.", { exact: false }))
      .toBeVisible({ timeout: 30_000 });
  } finally {
    await pair.close();
  }
});

test("reload closes old media session and allows a fresh guest rejoin", async ({ browser }) => {
  const pair = await createCallPair(browser);
  try {
    await joinCall(pair.owner, "vi");
    await joinCall(pair.guest, "ru");
    await Promise.all([waitForRemoteMedia(pair.owner), waitForRemoteMedia(pair.guest)]);

    await pair.guest.reload();
    await expect(pair.guest.getByRole("heading", { name: "Join your video call" }))
      .toBeVisible();
    await expect(pair.owner.getByText("The other person left the call.", { exact: false }))
      .toBeVisible({ timeout: 35_000 });

    await joinCall(pair.guest, "ru");
    await Promise.all([waitForRemoteMedia(pair.owner), waitForRemoteMedia(pair.guest)]);

    // On reload, the browser must not keep two microphone/video publications.
    const localTracks = await pair.guest.locator("video[aria-label='You video']")
      .evaluate((video: HTMLVideoElement) => {
        const stream = video.srcObject as MediaStream | null;
        return stream?.getVideoTracks().filter((track) => track.readyState === "live").length ?? 0;
      });
    expect(localTracks).toBe(1);
  } finally {
    await pair.close();
  }
});

test("invalid or forged invitation cannot join a room", async ({ browser }) => {
  const pair = await createCallPair(browser);
  try {
    await pair.guest.goto("http://127.0.0.1:3000/call/" + pair.roomId + "#invite=invalid");
    await pair.guest.getByRole("button", { name: "Join video call" }).click();
    await expect(pair.guest.getByText("This invitation is invalid.", { exact: false }))
      .toBeVisible();
    await expect(pair.guest.getByRole("heading", { name: "Video call", exact: true }))
      .toHaveCount(0);
  } finally {
    await pair.close();
  }
});
