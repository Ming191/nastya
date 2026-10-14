import { defineConfig } from "@playwright/test";
import config from "./playwright.config";

// The protocol-to-DOM fixture bundles CaptionPanel and intercepts its script.
// It needs neither a Next server nor an SFU; RTC tests retain their own config.
export default defineConfig({
  ...config,
  testMatch: "**/caption-correlation.spec.ts",
  webServer: undefined,
});
