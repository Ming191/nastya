/** Exact browser RTC destinations; never allow arbitrary HTTPS/WSS egress. */
export function rtcConnectSources(env: Record<string, string | undefined>): string {
  const origins = new Set<string>(["'self'"]);
  const endpoints = [env.LIVEKIT_URL,
    ...(env.NASTYA_LIVEKIT_CONNECT_ORIGINS?.split(",") ?? [])];
  if (env.NODE_ENV === "development") {
    endpoints.push((env.NASTYA_PUBLIC_ORIGIN ?? "http://localhost:3000")
      .replace(/^https:/, "wss:").replace(/^http:/, "ws:"));
  }
  for (const endpoint of endpoints) {
    if (!endpoint) continue;
    const url = new URL(endpoint.trim());
    const local = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
    const secure = ["https:", "wss:"].includes(url.protocol);
    const localDev = env.NODE_ENV !== "production" && local &&
      ["http:", "ws:"].includes(url.protocol);
    if ((!secure && !localDev) || url.username || url.password || url.search ||
        url.hash || url.pathname !== "/" ||
        !/^(?:[a-z0-9.-]+|\[[0-9a-f:]+\])$/i.test(url.hostname)) {
      throw new Error("Invalid LiveKit CSP origin configuration");
    }
    origins.add(url.origin);
    if (url.protocol === "wss:" || url.protocol === "ws:") {
      origins.add(url.origin.replace(/^wss:/, "https:").replace(/^ws:/, "http:"));
    }
  }
  return [...origins].join(" ");
}
