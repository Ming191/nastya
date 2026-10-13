import assert from "node:assert/strict";
import test from "node:test";
import { rtcConnectSources } from "./security-headers";

test("production CSP permits only configured RTC origins and HTTPS equivalents", () => {
  assert.equal(rtcConnectSources({ NODE_ENV: "production", LIVEKIT_URL: "wss://rtc.example:8443",
    NASTYA_LIVEKIT_CONNECT_ORIGINS: "wss://region.example,https://region.example" }),
  "'self' wss://rtc.example:8443 https://rtc.example:8443 wss://region.example https://region.example");
  assert.equal(rtcConnectSources({ NODE_ENV: "production" }), "'self'");
});

test("CSP rejects wildcard, credentials, non-TLS production and directive injection", () => {
  for (const url of ["wss://*.example", "wss://user:password@rtc.example", "ws://localhost:7880",
    "http://rtc.example", "wss://rtc.example/path", "wss://rtc.example?x=1",
    "wss://rtc.example#fragment", "wss://rtc.example;script-src", "wss://rtc.example; script-src *"]) {
    assert.throws(() => rtcConnectSources({ NODE_ENV: "production", LIVEKIT_URL: url }));
  }
});

test("development supports local RTC and Next hot reload without broad schemes", () => {
  const sources = rtcConnectSources({ NODE_ENV: "development", LIVEKIT_URL: "ws://localhost:7880" });
  assert.ok(sources.includes("ws://localhost:7880 http://localhost:7880"));
  assert.ok(sources.includes("ws://localhost:3000"));
  assert.ok(rtcConnectSources({ NODE_ENV: "development", NASTYA_PUBLIC_ORIGIN: "http://127.0.0.1:3100" })
    .includes("ws://127.0.0.1:3100"));
  assert.throws(() => rtcConnectSources({ NODE_ENV: "development", LIVEKIT_URL: "ws://public.example" }));
});
