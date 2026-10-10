import { expect, type Browser, type BrowserContext, type Page } from "@playwright/test";
import { trackRtcPeers } from "./rtc-stats";

type CallPair = {
  ownerContext: BrowserContext;
  guestContext: BrowserContext;
  owner: Page;
  guest: Page;
  roomId: string;
  guestUrl: string;
  close: () => Promise<void>;
};

export async function createCallPair(browser: Browser): Promise<CallPair> {
  // Separate isolated browser sessions with fake Chrome camera and microphone.
  const ownerContext = await browser.newContext({ permissions: ["camera", "microphone"] });
  const guestContext = await browser.newContext({ permissions: ["camera", "microphone"] });
  await trackRtcPeers(ownerContext);
  await trackRtcPeers(guestContext);
  const owner = await ownerContext.newPage();
  const guest = await guestContext.newPage();
  try {
    await owner.goto("/");
    const creation = owner.waitForResponse((response) =>
      response.url().endsWith("/api/rooms") && response.request().method() === "POST");
    await owner.getByRole("button", { name: "Create a private room" }).click();
    const response = await creation;
    if (!response.ok()) {
      const result = await response.json() as { error?: { code?: string } };
      // Never print successful bearer invitations or LiveKit credentials.
      throw new Error("Room creation returned HTTP " + response.status() +
        " / " + (result.error?.code ?? "UNKNOWN"));
    }
    const invitation = owner.getByRole("region", { name: "Room invitation" });
    await expect(invitation).toBeVisible();
    const guestUrl = await invitation.getByRole("link").first().getAttribute("href");
    if (!guestUrl) throw new Error("No guest invitation provided by the room API");
    const url = new URL(guestUrl);
    const roomId = url.pathname.split("/").pop();
    if (!roomId) throw new Error("Invalid room ID");
    await owner.getByRole("link", { name: "Continue as room owner" }).click();
    await guest.goto(guestUrl);
    return {
      ownerContext, guestContext, owner, guest, roomId, guestUrl,
      close: async () => { await Promise.allSettled([ownerContext.close(), guestContext.close()]); },
    };
  } catch (error) {
    await Promise.allSettled([ownerContext.close(), guestContext.close()]);
    throw error;
  }
}

export async function joinCall(page: Page, language: "vi" | "ru"): Promise<void> {
  await expect(page.getByRole("heading", { name: "Join your video call" })).toBeVisible();
  await page.getByLabel("Spoken language").selectOption(language);
  await page.getByRole("button", { name: "Join video call" }).click();
  await expect(page.getByRole("heading", { name: "Video call", exact: true })).toBeVisible({
    timeout: 60_000,
  });
  await expect(page.getByRole("button", { name: "Mute mic" })).toBeEnabled({
    timeout: 30_000,
  });
  await expect(page.getByRole("button", { name: "Turn camera off" })).toBeEnabled({
    timeout: 30_000,
  });
}

export async function waitForRemoteMedia(page: Page): Promise<void> {
  await expect.poll(async () => page.locator(".video-tile:not(.self) video")
    .evaluate((video: HTMLVideoElement) => {
      const stream = video.srcObject;
      return video.videoWidth > 0 && stream instanceof MediaStream &&
        stream.getVideoTracks().some((track) => track.readyState === "live");
    }), { timeout: 60_000, message: "remote video track must play real synthetic frames" }).toBe(true);

  await expect.poll(async () => page.locator("audio[aria-label='Other participant audio']")
    .evaluate((audio: HTMLAudioElement) => {
      const stream = audio.srcObject;
      return stream instanceof MediaStream &&
        stream.getAudioTracks().some((track) => track.readyState === "live");
    }), { timeout: 40_000, message: "remote audio track must be subscribed" }).toBe(true);
}
