import type { BrowserContext, Page, TestInfo } from "@playwright/test";

const tracker = String.raw`(() => {
  const Native = window.RTCPeerConnection;
  if (!Native) return;
  window.__nastyaRtcTestPeers = [];
  window.RTCPeerConnection = new Proxy(Native, {
    construct(target, args, newTarget) {
      const peer = Reflect.construct(target, args, newTarget);
      window.__nastyaRtcTestPeers.push(peer);
      return peer;
    },
  });
})();`;

export async function trackRtcPeers(context: BrowserContext): Promise<void> {
  await context.addInitScript({ content: tracker });
}

type Sample = {
  state: string;
  selectedPairs: Array<{
    state: string;
    localCandidateType: string | null;
    remoteCandidateType: string | null;
    currentRttMs: number | null;
    packetsSent: number | null;
    packetsReceived: number | null;
  }>;
  inbound: Array<{
    kind: string;
    packetsReceived: number;
    packetsLost: number;
    jitterMs: number | null;
    bytesReceived: number;
    framesDecoded?: number;
  }>;
  outbound: Array<{
    kind: string;
    packetsSent: number;
    bytesSent: number;
  }>;
};

export async function collectRtcStats(page: Page): Promise<Sample[]> {
  return page.evaluate(async () => {
    type WindowWithPeers = Window & { __nastyaRtcTestPeers?: RTCPeerConnection[] };
    const peers = (window as WindowWithPeers).__nastyaRtcTestPeers ?? [];
    return Promise.all(peers.map(async (pc): Promise<Sample> => {
      const stats = await pc.getStats();
      const pairs: Sample["selectedPairs"] = [];
      const inbound: Sample["inbound"] = [];
      const outbound: Sample["outbound"] = [];
      stats.forEach((raw) => {
        const entry = raw as RTCStats & Record<string, unknown>;
        const num = (field: string) => typeof entry[field] === "number"
          ? entry[field] as number : null;
        if (entry.type === "candidate-pair" &&
            entry.state === "succeeded" && (entry.nominated || entry.selected)) {
          const local = stats.get(String(entry.localCandidateId ?? "")) as
            (RTCStats & { candidateType?: string }) | undefined;
          const remote = stats.get(String(entry.remoteCandidateId ?? "")) as
            (RTCStats & { candidateType?: string }) | undefined;
          pairs.push({
            state: String(entry.state),
            // Deliberately do NOT export candidate IPs, port numbers, SDP or URLs.
            localCandidateType: local?.candidateType ?? null,
            remoteCandidateType: remote?.candidateType ?? null,
            currentRttMs: num("currentRoundTripTime") !== null
              ? Math.round(num("currentRoundTripTime")! * 1000) : null,
            packetsSent: num("packetsSent"),
            packetsReceived: num("packetsReceived"),
          });
        } else if (entry.type === "inbound-rtp" && !entry.isRemote) {
          inbound.push({
            kind: String(entry.kind ?? entry.mediaType ?? "unknown"),
            packetsReceived: num("packetsReceived") ?? 0,
            packetsLost: num("packetsLost") ?? 0,
            jitterMs: num("jitter") !== null ? Math.round(num("jitter")! * 1000) : null,
            bytesReceived: num("bytesReceived") ?? 0,
            ...(num("framesDecoded") !== null ? { framesDecoded: num("framesDecoded")! } : {}),
          });
        } else if (entry.type === "outbound-rtp" && !entry.isRemote) {
          outbound.push({
            kind: String(entry.kind ?? entry.mediaType ?? "unknown"),
            packetsSent: num("packetsSent") ?? 0,
            bytesSent: num("bytesSent") ?? 0,
          });
        }
      });
      return { state: pc.connectionState, selectedPairs: pairs, inbound, outbound };
    }));
  });
}

export async function attachRtcReport(
  info: TestInfo,
  labels: Array<{ name: string; page: Page }>,
): Promise<void> {
  const samples = await Promise.all(labels.map(async ({ name, page }) => ({
    client: name,
    connections: await collectRtcStats(page),
  })));
  await info.attach("rtc-metrics.json", {
    body: Buffer.from(JSON.stringify({
      scenario: "local synthetic two-browser LiveKit media",
      capturedAt: new Date().toISOString(),
      topology: "single GitHub runner; same host; no VPN or TURN assertion",
      samples,
    }, null, 2)),
    contentType: "application/json",
  });
}
