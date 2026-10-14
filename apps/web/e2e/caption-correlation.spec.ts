import { expect, test } from "@playwright/test";
import { build } from "esbuild";
import { resolve } from "node:path";

// Browser protocol -> CaptionStore -> real CaptionPanel -> explicit DOM ID.
// Transport is a synthetic Room event; this is not a real SFU acceptance test.
test("final captions preserve exact IDs even without partial packets and with identical text", async ({ page }) => {
  const bundle = await build({
    stdin: {
      contents: `
        import React from "react";
        import { createRoot } from "react-dom/client";
        import { RoomEvent } from "livekit-client";
        import { CaptionPanel } from "./src/components/caption-panel";
        const listeners = new Map();
        const room = { name: "nastya_" + "a".repeat(32),
          on(event, callback) { listeners.set(event, [...(listeners.get(event) || []), callback]); },
          off(event, callback) { listeners.set(event, (listeners.get(event) || []).filter(c => c !== callback)); }
        };
        window.publishSyntheticCaption = packet => {
          for (const callback of listeners.get(RoomEvent.DataReceived) || []) {
            callback(new TextEncoder().encode(JSON.stringify(packet)), { identity: "interpreter" }, undefined, "nastya.caption.v1");
          }
        };
        createRoot(document.getElementById("root")).render(<CaptionPanel room={room} language="ru" />);
      `,
      loader: "tsx",
      resolveDir: resolve("."),
    },
    bundle: true, write: false, platform: "browser", format: "iife", jsx: "automatic",
    define: { "process.env.NODE_ENV": '"production"' },
  });
  await page.route("**/caption-fixture.js", route => route.fulfill({
    contentType: "application/javascript", body: bundle.outputFiles[0].text,
  }));
  await page.setContent('<div id="root"></div><script src="http://127.0.0.1:3100/caption-fixture.js"></script>');
  await expect(page.getByRole("button", { name: "Hide captions" })).toBeVisible();
  for (const sequence of [1, 2]) {
    await page.evaluate(sequence => {
      const packet = { version: 1, type: "caption.upsert", roomId: "nastya_" + "a".repeat(32),
        speakerId: "human:owner", utteranceId: "human:owner:1:" + sequence,
        revision: 1, sequence, sourceLanguage: "vi", targetLanguage: "ru",
        sourceText: "synthetic", translatedText: "Одинаковый текст", translationState: "translated",
        isFinal: true, startOffsetMs: 0, endOffsetMs: 100 };
      (window as unknown as { publishSyntheticCaption: (p: unknown) => void }).publishSyntheticCaption(packet);
    }, sequence);
    await expect(page.locator(`[data-utterance-id="human:owner:1:${sequence}"]`))
      .toHaveText(/Одинаковый текст/);
  }
  await expect(page.locator(".caption-entry.final")).toHaveCount(2);
  expect(await page.locator(".caption-entry").evaluateAll(entries =>
    entries.map(entry => entry.getAttribute("data-utterance-id"))))
    .toEqual(["human:owner:1:1", "human:owner:1:2"]);
});
