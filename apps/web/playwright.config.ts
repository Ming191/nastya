import { defineConfig } from "@playwright/test";

const baseURL = "http://127.0.0.1:3000";

export default defineConfig({
  testDir: "./e2e",
  testMatch: ["**/*.spec.ts"],
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  timeout: 120_000,
  expect: { timeout: 25_000 },
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  outputDir: "test-results",
  use: {
    baseURL,
    browserName: "chromium",
    headless: true,
    actionTimeout: 20_000,
    navigationTimeout: 35_000,
    screenshot: "off",
    trace: "off",
    video: "off",
    launchOptions: {
      args: [
        "--use-fake-device-for-media-stream",
        "--use-fake-ui-for-media-stream",
        "--autoplay-policy=no-user-gesture-required",
      ],
    },
  },
  webServer: {
    command: "npm run dev",
    url: baseURL + "/api/health",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: {
      NASTYA_PUBLIC_ORIGIN: baseURL,
      LIVEKIT_URL: "ws://127.0.0.1:7880",
      LIVEKIT_API_KEY: "devkey",
      LIVEKIT_API_SECRET: "secret",
      NASTYA_INVITE_SECRET: "this-is-a-local-only-test-secret-with-more-than-32-bytes",
    },
  },
});
