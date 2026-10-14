import { chromium, expect, type Page } from "@playwright/test";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { resolve, join } from "node:path";

type Fixture = { id: string; language: "vi" | "ru"; source: string; reference: string;
  audioPath: string; condition: string };
type Caption = { index: number; final: boolean; text: string; language: string | null;
  atUnixMs: number };
const [origin, corpusPath, output, profile = "baseline"] = process.argv.slice(2);
if (!origin || new URL(origin).protocol !== "https:" || !corpusPath || !output ||
    !process.env.NASTYA_CAPTION_SMOKE_WORKER_ENV) throw new Error("Missing benchmark arguments");

async function prepare(page: Page) {
  await page.addInitScript({ content: "globalThis.__name = (value) => value;" });
  await page.addInitScript(() => {
    let context: AudioContext;
    let destination: MediaStreamAudioDestinationNode;
    const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async (constraints) => {
      if (!constraints?.audio) return original(constraints);
      if (!context) {
        context = new AudioContext({ sampleRate: 48000 });
        destination = context.createMediaStreamDestination();
      }
      await context.resume();
      const stream = constraints.video ? await original({ ...constraints, audio: false }) : new MediaStream();
      stream.addTrack(destination.stream.getAudioTracks()[0].clone());
      return stream;
    };
    const events: Array<{ index: number; final: boolean; text: string; language: string | null; atUnixMs: number }> = [];
    const states = new WeakMap<Element, string>();
    const indexes = new WeakMap<Element, number>();
    let serial = 0;
    const clocks: Array<{ errorMs: number | null }> = [];
    Object.assign(window, { __benchmarkCaptions: events, __benchmarkClocks: clocks, __playFixture: async (encoded: string, noise: boolean) => {
      await context.resume();
      const raw = Uint8Array.from(atob(encoded), c => c.charCodeAt(0));
      const decoded = await context.decodeAudioData(raw.buffer);
      const pcm = decoded.getChannelData(0);
      let first = 0, last = pcm.length - 1;
      while (first < last && Math.abs(pcm[first]) < 0.003) first++;
      while (last > first && Math.abs(pcm[last]) < 0.003) last--;
      first = Math.max(0, first - 3840);
      const end = Math.min(pcm.length, last + 3840);
      const buffer = context.createBuffer(1, end - first, context.sampleRate);
      const data = buffer.getChannelData(0);
      let seed = 123456;
      for (let i = 0; i < data.length; i++) {
        seed = (1664525 * seed + 1013904223) >>> 0;
        data[i] = pcm[first + i] + (noise ? ((seed / 4294967296) * 2 - 1) * 0.02 : 0);
      }
      const source = context.createBufferSource();
      source.buffer = buffer;
      source.connect(destination);
      const scheduled = context.currentTime + 0.1;
      const nowUnix = performance.timeOrigin + performance.now();
      const startUnix = nowUnix + (scheduled - context.currentTime) * 1000;
      const endUnix = startUnix + (last - first + 1) / context.sampleRate * 1000;
      const clock = { errorMs: null as number | null };
      const clockIndex = clocks.push(clock) - 1;
      source.onended = () => {
        clock.errorMs = Math.abs(performance.timeOrigin + performance.now() -
          (startUnix + buffer.duration * 1000)) + 3;
      };
      source.start(scheduled);
      return { startUnixMs: startUnix, speechEndUnixMs: endUnix,
        durationMs: (last - first + 1) / context.sampleRate * 1000,
        clockIndex, definition: "last synthetic PCM sample above amplitude 0.003; AudioContext schedule mapped to same-host monotonic clock, checked against source onended callback" };
    } });
    document.addEventListener("DOMContentLoaded", () => {
      new MutationObserver(() => {
        for (const entry of document.querySelectorAll(".caption-entry")) {
          const text = entry.querySelector("p")?.textContent ?? "";
          const final = entry.classList.contains("final");
          const state = `${final}:${text}`;
          if (states.get(entry) === state) continue;
          states.set(entry, state);
          if (!indexes.has(entry)) indexes.set(entry, serial++);
          events.push({ index: indexes.get(entry)!, final, text,
            language: entry.querySelector("p")?.getAttribute("lang") ?? null,
            atUnixMs: performance.timeOrigin + performance.now() });
        }
      }).observe(document.documentElement, { childList: true, subtree: true, attributes: true });
    });
  });
}

async function connect(page: Page, language: string) {
  await page.getByLabel("Spoken language").selectOption(language);
  await page.getByRole("checkbox", { name: /I understand that camera\/microphone media/ }).check();
  await page.getByRole("button", { name: "Join video call" }).click();
  await expect(page.getByRole("heading", { name: "Video call", exact: true })).toBeVisible({ timeout: 60000 });
  await expect(page.getByRole("button", { name: "Mute mic" })).toBeEnabled({ timeout: 30000 });
}
async function events(page: Page): Promise<Caption[]> {
  return page.evaluate(() => (window as unknown as { __benchmarkCaptions: Caption[] }).__benchmarkCaptions);
}

async function main() {
  await mkdir(output, { recursive: true });
  const fixtures: Fixture[] = JSON.parse(await readFile(corpusPath, "utf8"));
  const report = { source: "synthetic", profile, startedAt: new Date().toISOString(),
    clockMethod: "Same Windows host for source and receiver. AudioContext schedule is mapped to browser performance wall clock and checked against source onended; callback discrepancy plus 3ms is recorded per case. Latency is unavailable above a 50ms check threshold. Endpoint is DOM mutation, not optical paint detection. These are synthetic capture-to-DOM estimates, not human or cross-device acceptance.",
    samples: [] as unknown[], errors: [] as string[], gpuSamples: [] as unknown[], workerWarnings: [] as string[],
    gpuScope: "Whole GPU, includes other applications; not process VRAM" };
  function gpuSample(stage: string) {
    if (!profile.startsWith("local") && !profile.startsWith("hybrid")) return;
    const result = spawnSync("nvidia-smi", ["--query-gpu=memory.used,memory.total,utilization.gpu", "--format=csv,noheader,nounits"], { windowsHide: true, encoding: "utf8" });
    if (result.status === 0) {
      const values = result.stdout.trim().split(",").map(Number);
      if (values.every(Number.isFinite)) report.gpuSamples.push({ stage, atUnixMs: Date.now(), usedMiB: values[0], totalMiB: values[1], utilizationPercent: values[2] });
    }
  }
  gpuSample("before_worker");
  let worker: ChildProcess | undefined;
  const browser = await chromium.launch({ args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required"] });
  try {
    const owner = await (await browser.newContext({ permissions: ["camera", "microphone"] })).newPage();
    const guest = await (await browser.newContext({ permissions: ["camera", "microphone"] })).newPage();
    await prepare(owner); await prepare(guest);
    await owner.goto(origin);
    const pending = owner.waitForResponse(r => r.url().endsWith("/api/rooms") && r.request().method() === "POST");
    await owner.getByRole("button", { name: "Create a private room" }).click();
    const response = await pending;
    if (!response.ok()) throw new Error(`Room creation HTTP ${response.status()}`);
    const { roomId } = await response.json();
    if (!/^nastya_[a-f0-9]{32}$/.test(roomId)) throw new Error("Invalid room ID");
    const guestUrl = await owner.getByRole("region", { name: "Room invitation" }).getByRole("link").first().getAttribute("href");
    if (!guestUrl) throw new Error("Missing invitation");
    const cwd = resolve("services/ai-worker");
    worker = spawn(resolve(cwd, ".venv/Scripts/python.exe"), ["-m", "nastya_worker.benchmark_room", "--room", roomId,
      "--env-file", process.env.NASTYA_CAPTION_SMOKE_WORKER_ENV!, "--output", resolve(output, "worker-events.jsonl")],
      { cwd, env: { ...process.env, ...(profile !== "baseline" ? { NASTYA_STT_BURST_REQUESTS: "2", NASTYA_STT_CONCURRENCY: "2", NASTYA_VAD_SILENCE_MS: "250" } : {}),
        ...(["realtime", "pressure", "aggressive"].includes(profile) ? { NASTYA_MT_REASONING_EFFORT: "none", NASTYA_MT_TIMEOUT_SECONDS: "2" } : {}),
        ...(profile === "aggressive" ? { NASTYA_VAD_SILENCE_MS: "150" } : {}),
        ...(profile.startsWith("local") ? { NASTYA_STT_PROVIDER: "local", NASTYA_MT_REASONING_EFFORT: "none", NASTYA_MT_TIMEOUT_SECONDS: "2", NASTYA_STT_CONCURRENCY: "1", NASTYA_STT_MIN_INTERVAL_SECONDS: "0" } : {}),
        ...(profile.includes("int8") ? { NASTYA_STT_LOCAL_COMPUTE_TYPE: "int8_float16" } : {}),
        ...(profile.startsWith("hybrid") ? { NASTYA_STT_PROVIDER: "hybrid", NASTYA_MT_REASONING_EFFORT: "none", NASTYA_MT_TIMEOUT_SECONDS: "2" } : {}) }, windowsHide: true, stdio: ["ignore", "ignore", "pipe"] });
    worker.stderr?.on("data", (chunk: Buffer) => {
      const text = chunk.toString();
      if (text.includes("audio stream failed")) {
        const errorType = /error_type=([A-Za-z]+Error)/.exec(text)?.[1] ?? "unknown";
        report.workerWarnings.push("audio_stream_failure:" + errorType);
      }
      if (text.includes("audio frame format unavailable")) report.workerWarnings.push("audio_format_failure");
      if (text.includes("interpreter RTC unavailable")) report.workerWarnings.push("rtc_failure");
    });
    worker.on("error", () => report.errors.push("Worker startup failed"));
    await new Promise(done => setTimeout(done, 2500));
    await owner.getByRole("link", { name: "Continue as room owner" }).click();
    await guest.goto(guestUrl);
    await Promise.all([connect(owner, "vi"), connect(guest, "ru")]);
    const readyDeadline = Date.now() + 60000;
    while (true) {
      try { await readFile(resolve(output, "worker-events.ready")); break; }
      catch { if (Date.now() > readyDeadline || worker.exitCode !== null) throw new Error("Worker not ready"); }
      await new Promise(done => setTimeout(done, 250));
    }
    await new Promise(done => setTimeout(done, 3500));
    gpuSample("worker_ready");
    if (profile.includes("pressure")) {
      const jobs: Array<{ fixture: Fixture; timing: Record<string, unknown>; source: Page }> = [];
      for (let pair = 0; pair < 6; pair++) {
        const started = Date.now();
        for (const language of ["vi", "ru"] as const) {
          const fixture = fixtures.find(f => f.id === `${language}-${pair}`)!;
          const source = language === "vi" ? owner : guest;
          const encoded = (await readFile(fixture.audioPath)).toString("base64");
          const timing = await source.evaluate(encoded =>
            (window as unknown as { __playFixture: (s: string, n: boolean) => Promise<Record<string, unknown>> }).__playFixture(encoded, false), encoded);
          jobs.push({ fixture, timing, source });
        }
        await new Promise(done => setTimeout(done, Math.max(0, 4500 - (Date.now() - started))));
        gpuSample("pressure_pair_" + pair);
      }
      await new Promise(done => setTimeout(done, 14000));
      const workerEvents = (await readFile(join(output, "worker-events.jsonl"), "utf8")).trim().split("\n")
        .map(line => JSON.parse(line) as { sample: string; stage: string; unixMs: number });
      const normalize = (text: string) => text.toLowerCase().replace(/chín|девять/g, "9")
        .replace(/ba|три/g, "3").replace(/hai|две|два/g, "2").replace(/năm|пять/g, "5")
        .replace(/ё/g, "е").replace(/[^\p{L}\p{N}]/gu, "");
      const distance = (a: string, b: string) => {
        let row = Array.from({ length: b.length + 1 }, (_, i) => i);
        for (let i = 1; i <= a.length; i++) {
          const next = [i];
          for (let j = 1; j <= b.length; j++) next.push(Math.min(next[j - 1] + 1, row[j] + 1, row[j - 1] + Number(a[i - 1] !== b[j - 1])));
          row = next;
        }
        return row[b.length];
      };
      for (const language of ["vi", "ru"] as const) {
        const target = language === "vi" ? guest : owner;
        const received = await events(target);
        const groups = new Map<number, Caption[]>();
        for (const event of received) groups.set(event.index, [...(groups.get(event.index) ?? []), event]);
        const matches = new Map<string, Caption[]>();
        const role = language === "vi" ? "human:owner:" : "human:guest:";
        for (const group of groups.values()) {
          const partial = group.find(e => !e.final);
          if (!partial) continue;
          // Correlate private synthetic fixtures through metadata timestamps,
          // preserving multiple VAD fragments belonging to the same sentence.
          const publication = workerEvents.filter(e => e.stage === "partial_publish" && e.sample.startsWith(role))
            .map(e => ({ event: e, delay: partial.atUnixMs - e.unixMs }))
            .filter(e => e.delay >= -20 && e.delay < 1500).sort((a, b) => a.delay - b.delay)[0];
          const enqueued = publication && workerEvents.find(e => e.stage === "enqueued" && e.sample === publication.event.sample);
          const job = enqueued && jobs.find(j => j.fixture.language === language &&
            Number(j.timing.startUnixMs) <= enqueued.unixMs && enqueued.unixMs <= Number(j.timing.speechEndUnixMs) + 1500);
          if (job) {
            matches.set(job.fixture.id, [...(matches.get(job.fixture.id) ?? []), ...group]);
            continue;
          }
          const choices = jobs.filter(j => j.fixture.language === language).map(j => ({ id: j.fixture.id,
            error: distance(normalize(j.fixture.source), normalize(partial.text)) / Math.max(1, normalize(j.fixture.source).length) })).sort((a, b) => a.error - b.error);
          if (choices[0].error < 0.4) matches.set(choices[0].id, [...(matches.get(choices[0].id) ?? []), ...group]);
        }
        for (const job of jobs.filter(j => j.fixture.language === language)) {
          const group = matches.get(job.fixture.id) ?? [];
          const partials = group.filter(e => !e.final);
          const finals = group.filter(e => e.final);
          const final = finals.at(-1);
          const clockErrorBoundMs = await job.source.evaluate(index =>
            (window as unknown as { __benchmarkClocks: Array<{ errorMs: number | null }> }).__benchmarkClocks[index].errorMs, Number(job.timing.clockIndex));
          report.samples.push({ id: job.fixture.id, condition: "sustained_overlap",
            direction: language === "vi" ? "vi-ru" : "ru-vi", sourceReference: job.fixture.source,
            reference: job.fixture.reference, recognizedText: partials.map(e => e.text).join(" "), translatedText: finals.map(e => e.text).join(" "),
            ...job.timing, clockErrorBoundMs, finalUnixMs: final?.atUnixMs ?? null,
            speechEndToFinalMs: final && clockErrorBoundMs !== null && clockErrorBoundMs <= 50 ? final.atUnixMs - Number(job.timing.speechEndUnixMs) : null,
            success: !!final && final.language === (language === "vi" ? "ru" : "vi"), finalLanguage: final?.language ?? null });
        }
      }
      console.log("pressure: completed 12 simultaneous utterances at 4.5-second cadence");
      return;
    }
    async function run(fixture: Fixture) {
      const source = fixture.language === "vi" ? owner : guest;
      const target = fixture.language === "vi" ? guest : owner;
      const before = (await events(target)).length;
      const encoded = (await readFile(fixture.audioPath)).toString("base64");
      const timing = await source.evaluate(({ encoded, noise }) =>
        (window as unknown as { __playFixture: (s: string, n: boolean) => Promise<Record<string, unknown>> }).__playFixture(encoded, noise),
        { encoded, noise: fixture.condition === "noise" });
      const end = Number(timing.speechEndUnixMs);
      const deadline = Date.now() + Number(timing.durationMs) + 20000;
      let recent: Caption[] = [];
      while (Date.now() < deadline) {
        recent = (await events(target)).slice(before);
        if (recent.some(e => e.final && e.atUnixMs >= end - 100)) break;
        await new Promise(done => setTimeout(done, 100));
      }
      // Allow final corrections belonging to this fixed, isolated fixture.
      await new Promise(done => setTimeout(done, 450));
      recent = (await events(target)).slice(before);
      const finals = recent.filter(e => e.final);
      const partials = recent.filter(e => !e.final);
      const final = finals.at(-1);
      const clockErrorBoundMs = await source.evaluate((index) =>
        (window as unknown as { __benchmarkClocks: Array<{ errorMs: number | null }> }).__benchmarkClocks[index].errorMs,
        Number(timing.clockIndex));
      const result = { id: fixture.id, direction: fixture.language === "vi" ? "vi-ru" : "ru-vi", condition: fixture.condition,
        reference: fixture.reference, sourceReference: fixture.source,
        recognizedText: partials.map(e => e.text).join(" "), translatedText: finals.map(e => e.text).join(" "),
        ...timing, finalUnixMs: final?.atUnixMs ?? null,
        clockErrorBoundMs,
        speechEndToFinalMs: final && clockErrorBoundMs !== null && clockErrorBoundMs <= 50 ? final.atUnixMs - end : null,
        captionCount: finals.length, success: !!final && final.language === (fixture.language === "vi" ? "ru" : "vi"),
        finalLanguage: final?.language ?? null };
      report.samples.push(result);
      gpuSample(fixture.id);
      console.log(`${profile} ${fixture.id}: ${result.speechEndToFinalMs !== null ? Math.round(result.speechEndToFinalMs) + " ms" : "latency unavailable"}`);
      await writeFile(join(output, "browser-results.json"), JSON.stringify(report, null, 2));
      await new Promise(done => setTimeout(done, 1200));
    }
    for (const fixture of fixtures.filter(f => f.condition !== "simultaneous")) await run(fixture);
    await Promise.all(fixtures.filter(f => f.condition === "simultaneous").map(run));
  } catch {
    report.errors.push("Benchmark failed; no credentials or upstream content logged");
    process.exitCode = 1;
  } finally {
    await browser.close();
    if (worker?.pid && worker.exitCode === null) spawnSync("taskkill", ["/PID", String(worker.pid), "/T", "/F"], { windowsHide: true, stdio: "ignore" });
    await writeFile(join(output, "browser-results.json"), JSON.stringify(report, null, 2));
  }
}
main().catch(() => { console.error("Benchmark could not start"); process.exitCode = 1; });
