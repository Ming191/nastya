import { chromium, expect, type Page } from "@playwright/test";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { createServer, type Server } from "node:http";
import { resolve, join } from "node:path";

const [origin, viAudio, ruAudio, output] = process.argv.slice(2);
if (!origin || new URL(origin).protocol !== "https:" || !viAudio || !ruAudio || !output) {
  throw new Error("Usage: tsx scripts/validate-captions.ts HTTPS_URL VI_WAV RU_WAV OUTPUT_DIR");
}
const report: Record<string, unknown> = {
  source: "synthetic", startedAt: new Date().toISOString(),
  methodology: "Two Chromium instances on one Windows host, synthetic Vietnamese/Russian WAV microphone input, actual LiveKit and inference APIs. Speech-end to caption latency is unavailable: audio capture clocks were not calibrated. Partial-to-final timings describe browser rendering only. No real-person or Vietnam–Russia acceptance claim.",
  speechEndToCaptionMs: null,
  status: "running",
};

async function connect(page: Page, language: "vi" | "ru") {
  await page.getByLabel("Spoken language").selectOption(language);
  await page.getByRole("checkbox", { name: /I understand that camera\/microphone media/ }).check();
  await page.getByRole("button", { name: "Join video call" }).click();
  await expect(page.getByRole("heading", { name: "Video call", exact: true })).toBeVisible({ timeout: 60000 });
  await expect(page.getByRole("button", { name: "Mute mic" })).toBeEnabled({ timeout: 30000 });
}

async function observe(page: Page) {
  await page.addInitScript({ content: "globalThis.__name = (value) => value;" });
  await page.addInitScript(() => {
    const samples: Array<{ at: number; final: boolean; language: string | null }> = [];
    Object.assign(window, { __captionSamples: samples });
    document.addEventListener("DOMContentLoaded", () => {
      const observer = new MutationObserver(() => {
        const entries = document.querySelectorAll(".caption-entry");
        const entry = entries.item(entries.length - 1);
        if (!entry) return;
        const value = { at: performance.now(), final: entry.classList.contains("final"),
          language: entry.querySelector("p")?.getAttribute("lang") ?? null };
        const previous = samples.at(-1);
        if (!previous || previous.final !== value.final || previous.language !== value.language) {
          samples.push(value);
        }
      });
      observer.observe(document.documentElement, { subtree: true, childList: true, attributes: true });
    });
  });
}

async function main() {
  let worker: ChildProcess | undefined;
  let failureServer: Server | undefined;
  const mode = process.env.NASTYA_CAPTION_SMOKE_MODE ?? "translated";
  if (!["translated", "mt-failure", "mt-timeout"].includes(mode)) throw new Error("Unknown validation mode");
  if (mode !== "translated" && !process.env.NASTYA_CAPTION_SMOKE_WORKER_ENV) {
    throw new Error("Failure mode requires NASTYA_CAPTION_SMOKE_WORKER_ENV");
  }
  report.mode = mode;
  const args = (file: string) => ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
    `--use-file-for-fake-audio-capture=${resolve(file)}`, "--autoplay-policy=no-user-gesture-required"];
  const ownerBrowser = await chromium.launch({ args: args(viAudio) });
  const guestBrowser = await chromium.launch({ args: args(ruAudio) });
  try {
    const ownerContext = await ownerBrowser.newContext({ permissions: ["camera", "microphone"] });
    const guestContext = await guestBrowser.newContext({ permissions: ["camera", "microphone"] });
    const owner = await ownerContext.newPage();
    const guest = await guestContext.newPage();
    await observe(owner);
    await observe(guest);
    await owner.goto(origin);
    const pending = owner.waitForResponse(r => r.url().endsWith("/api/rooms") && r.request().method() === "POST");
    await owner.getByRole("button", { name: "Create a private room" }).click();
    const response = await pending;
    if (!response.ok()) throw new Error(`Room creation HTTP ${response.status()}`);
    if (process.env.NASTYA_CAPTION_SMOKE_WORKER_ENV) {
      const room = await response.json();
      if (!/^nastya_[a-f0-9]{32}$/.test(room.roomId)) throw new Error("Invalid test room ID");
      const env = { ...process.env };
      const configuration = await readFile(process.env.NASTYA_CAPTION_SMOKE_WORKER_ENV, "utf8");
      for (const line of configuration.split(/\r?\n/)) {
        const match = /^(NASTYA_[A-Z0-9_]+|LIVEKIT_[A-Z0-9_]+)=(.*)$/.exec(line);
        if (match) env[match[1]] = match[2];
      }
      if (mode !== "translated") {
        failureServer = createServer((_request, response) => {
          if (mode === "mt-timeout") return; // deadline is enforced by the worker
          response.writeHead(503, { "Content-Type": "application/json" });
          response.end('{}');
        });
        await new Promise<void>(done => failureServer!.listen(0, "127.0.0.1", done));
        const address = failureServer.address();
        if (!address || typeof address === "string") throw new Error("Failure API could not start");
        env.NASTYA_MT_URL = `http://127.0.0.1:${address.port}/translate`;
        env.NASTYA_MT_PROVIDER = "http";
        env.NASTYA_MT_API_KEY = "";
        env.NASTYA_MT_TIMEOUT_SECONDS = "0.3";
      }
      const workerDirectory = resolve("services/ai-worker");
      worker = spawn(resolve(workerDirectory, ".venv/Scripts/python.exe"),
        ["-m", "nastya_worker", "--room", room.roomId],
        { cwd: workerDirectory, env, windowsHide: true, stdio: "ignore" });
      worker.on("error", () => { report.workerStartupError = true; });
      await new Promise(done => setTimeout(done, 2500));
      if (!worker.pid || worker.exitCode !== null || report.workerStartupError) {
        throw new Error("Isolated test worker failed to start");
      }
      report.isolatedTestWorker = true;
    }
    const invitation = owner.getByRole("region", { name: "Room invitation" });
    const guestUrl = await invitation.getByRole("link").first().getAttribute("href");
    if (!guestUrl) throw new Error("Missing invitation");
    await owner.getByRole("link", { name: "Continue as room owner" }).click();
    await guest.goto(guestUrl);
    await Promise.all([connect(owner, "vi"), connect(guest, "ru")]);
    if (mode !== "translated" && !worker) throw new Error("Failure mode requires an isolated worker");
    const ownerCaptions = owner.locator(`.caption-entry.final p[lang="${mode === "translated" ? "vi" : "ru"}"]`);
    const guestCaptions = guest.locator(`.caption-entry.final p[lang="${mode === "translated" ? "ru" : "vi"}"]`);
    await expect.poll(async () => (await ownerCaptions.allTextContents()).join(" "), { timeout: 65000 })
      .toMatch(mode === "translated" ? /ngày mai/i : /завтра/i);
    await expect.poll(async () => (await guestCaptions.allTextContents()).join(" "), { timeout: 65000 })
      .toMatch(mode === "translated" ? /завтра/i : /ngày mai/i);
    report.status = "pass";
    report.viRecipient = { correctLanguage: true, sourceSpeaker: "Guest", finalCaptionReceived: true };
    report.ruRecipient = { correctLanguage: true, sourceSpeaker: "Owner", finalCaptionReceived: true };
    for (const [name, page] of [["viRecipient", owner], ["ruRecipient", guest]] as const) {
      const samples = await page.evaluate(() =>
        (window as unknown as { __captionSamples: unknown[] }).__captionSamples);
      report[name + "Events"] = samples;
      await expect(page.locator(".caption-entry.final .caption-entry-heading strong").first())
        .toHaveText(name === "viRecipient" ? "Guest" : "Owner");
      await expect(page.getByRole("button", { name: "Mute mic" })).toBeEnabled();
      const remoteVideo = page.locator(".video-tile:not(.self) video");
      const frames = await remoteVideo.evaluate((video: HTMLVideoElement) =>
        video.getVideoPlaybackQuality().totalVideoFrames);
      await expect.poll(() => remoteVideo.evaluate((video: HTMLVideoElement) =>
        video.getVideoPlaybackQuality().totalVideoFrames), { timeout: 5000 }).toBeGreaterThan(frames);
      if (mode !== "translated") {
        await expect(page.locator('.caption-entry.final .caption-entry-heading span').first())
          .toHaveText("Original only · translation unavailable");
      }
    }
    report.humanCallContinues = true;
    await owner.getByRole("button", { name: "Hide captions" }).click();
    await expect(owner.getByRole("log", { name: "Conversation captions" })).toHaveCount(0);
    await owner.getByRole("button", { name: "Show captions" }).click();
    await expect(owner.locator(".caption-entry")).toHaveCount(0);
    report.captionToggleClearsHistory = true;
    if (mode === "translated") {
      const beforeReload = await guestCaptions.count();
      await owner.reload();
      await connect(owner, "vi");
      await expect(owner.locator(".caption-entry")).toHaveCount(0);
      await expect.poll(() => guestCaptions.count(), { timeout: 65000 }).toBeGreaterThan(beforeReload);
      await expect.poll(() => guestCaptions.last().textContent(), { timeout: 15000 }).toMatch(/завтра/i);
      report.microphoneReloadResumesCaptions = true;
    }
    console.log(`PASS: synthetic VI/RU captions (${mode})`);
  } catch (error) {
    report.status = "fail";
    report.error = error instanceof Error ? error.message.split("\n")[0].replace(/https?:\/\/\S+/g, "[URL omitted]") : "Unknown failure";
    console.log("FAIL: synthetic caption integration; see report");
    process.exitCode = 1;
  } finally {
    await Promise.allSettled([ownerBrowser.close(), guestBrowser.close()]);
    if (worker?.pid && worker.exitCode === null) {
      if (process.platform === "win32") {
        spawnSync("taskkill", ["/PID", String(worker.pid), "/T", "/F"],
          { windowsHide: true, stdio: "ignore" });
      } else worker.kill("SIGTERM");
    }
    if (failureServer) {
      failureServer.closeAllConnections();
      await new Promise<void>(done => failureServer!.close(() => done()));
    }
    await mkdir(output, { recursive: true });
    await writeFile(join(output, "caption-integration.json"), JSON.stringify(report, null, 2));
  }
}
main().catch(() => { console.error("Caption validation could not start"); process.exitCode = 1; });
