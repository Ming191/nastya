import { chromium, expect, type Browser, type BrowserContext, type Page } from "@playwright/test";
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { collectRtcStats, trackRtcPeers } from "../apps/web/e2e/support/rtc-stats";

const origin = process.argv[2] ?? "https://nastya-amber.vercel.app";
const output = process.argv[3];
if (!output || new URL(origin).protocol !== "https:") throw new Error("Usage: tsx scripts/validate-deployment.ts https://your-app output-directory");
const report = {
  startedAt: new Date().toISOString(), target: origin, browser: "", platform: process.platform,
  methodology: "Single Windows host, isolated Chromium contexts, synthetic camera/microphone. No recording or transcript. No Vietnam–Russia, Safari, TURN-only, sustained load or AI inference claim. No trimmed samples. p95 uses nearest-rank and is descriptive with small n; RTC samples within a call are correlated. HTTP throttling does not emulate WebRTC UDP bandwidth.",
  cases: [] as Array<{ name: string; status: string; durationMs: number; detail: unknown }>,
  navigation: [] as Array<Record<string, number | string | null>>,
  health: [] as number[], createMs: [] as number[], joinMs: [] as number[],
  calls: [] as Array<Record<string, unknown>>,
};
const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
const round = (n: number) => Math.round(n * 100) / 100;
function summary(values: number[]) {
  const xs = values.filter(Number.isFinite).sort((a,b) => a-b);
  const n = xs.length;
  if (!n) return { n, median: null, mean: null, p95: null, min: null, max: null };
  return { n, median: round(n % 2 ? xs[Math.floor(n/2)] : (xs[n/2-1]+xs[n/2])/2),
    mean: round(xs.reduce((a,b) => a+b,0)/n), p95: round(xs[Math.ceil(n*.95)-1]),
    min: round(xs[0]), max: round(xs[n-1]) };
}
async function persist() {
  await mkdir(output, { recursive: true });
  await writeFile(join(output, "measurements.json"), JSON.stringify(report,null,2));
}
async function scenario(name: string, run: () => Promise<unknown>) {
  if (process.env.NASTYA_VALIDATION_CASE && !name.includes(process.env.NASTYA_VALIDATION_CASE)) return;
  const start = performance.now();
  try { const detail = await run(); report.cases.push({name,status:"pass",durationMs:round(performance.now()-start),detail}); }
  catch (error) { report.cases.push({name,status:"fail",durationMs:round(performance.now()-start),
    detail: error instanceof Error ? error.message.split("\n").filter((line,i)=>i===0||line.trim().startsWith("Locator:")).join(" ").replace(/https?:\/\/\S+/g,"[URL omitted]") : "Unknown error"}); }
  console.log(name + ": " + report.cases.at(-1)!.status);
  await persist();
}
async function post(path: string, body?: unknown, headers: Record<string,string> = {}) {
  const start = performance.now();
  const response = await fetch(origin+path,{method:"POST",headers:{Origin:origin,"Content-Type":"application/json",...headers},
    body: body === undefined ? undefined : typeof body === "string" ? body : JSON.stringify(body), signal:AbortSignal.timeout(20000)});
  return {response,ms:performance.now()-start};
}
async function newContext(browser: Browser, options: Parameters<Browser["newContext"]>[0] = {}) {
  const context = await browser.newContext({baseURL:origin,permissions:["camera","microphone"],...options});
  // tsx/esbuild may preserve nested function names using this host-side helper.
  // Provide it only in the isolated test pages for serialized evaluate callbacks.
  await context.addInitScript({content:"globalThis.__name = (value) => value;"});
  await trackRtcPeers(context);
  await context.addInitScript(() => {
    const Native = window.WebSocket;
    const sockets: WebSocket[] = [];
    Object.assign(window,{__validationSockets:sockets,__cspViolations:[]});
    window.WebSocket = new Proxy(Native,{construct(target,args,newTarget){const s=Reflect.construct(target,args,newTarget);sockets.push(s);return s;}});
    document.addEventListener("securitypolicyviolation", event => {
      let host = "non-url"; try {host = new URL(event.blockedURI).host;} catch {}
      (window as unknown as {__cspViolations:unknown[]}).__cspViolations.push({directive:event.effectiveDirective,host});
    });
  });
  return context;
}
async function pair(browser: Browser, mobile = false, denied = false, guestBrowser = browser) {
  const ownerContext=await newContext(browser,denied ? {permissions:[]} : {});
  const guestContext=await newContext(guestBrowser,mobile ? {viewport:{width:390,height:844},isMobile:true,hasTouch:true} : {});
  const owner=await ownerContext.newPage(), guest=await guestContext.newPage();
  try {
    await owner.goto("/");
    await owner.waitForTimeout(650);
    const start=performance.now();
    const pending=owner.waitForResponse(r=>r.url().endsWith("/api/rooms")&&r.request().method()==="POST");
    await owner.getByRole("button",{name:"Create a private room"}).click();
    const response=await pending;
    if (!response.ok()) throw new Error("Room creation HTTP "+response.status());
    report.createMs.push(performance.now()-start);
    const invitation=owner.getByRole("region",{name:"Room invitation"});
    await expect(invitation).toBeVisible();
    const url=await invitation.getByRole("link").first().getAttribute("href");
    if (!url) throw new Error("Invite missing");
    await owner.getByRole("link",{name:"Continue as room owner"}).click();
    await guest.goto(url);
    return {owner,guest,ownerContext,guestContext,close:async()=>{await Promise.allSettled([ownerContext.close(),guestContext.close()]);}};
  } catch(error) {await ownerContext.close();await guestContext.close();throw error;}
}
async function connect(page: Page, language: "vi"|"ru", media=true) {
  await expect(page.getByRole("heading",{name:"Join your video call"})).toBeVisible();
  await expect(page.getByRole("button",{name:"Join video call"})).toBeDisabled();
  await page.getByLabel("Spoken language").selectOption(language);
  await page.getByRole("checkbox",{name:/I understand that camera\/microphone media/}).check();
  const start=performance.now();
  await page.getByRole("button",{name:"Join video call"}).click();
  await expect(page.getByRole("heading",{name:"Video call",exact:true})).toBeVisible({timeout:60000});
  if(media) await expect(page.getByRole("button",{name:"Mute mic"})).toBeEnabled({timeout:30000});
  report.joinMs.push(performance.now()-start);
}
async function remote(page: Page) {
  await expect.poll(()=>page.locator(".video-tile:not(.self) video").evaluate((v:HTMLVideoElement)=>v.videoWidth),{timeout:45000}).toBeGreaterThan(0);
}
async function sample(page: Page) {
  const cdp=await page.context().newCDPSession(page);await cdp.send("Performance.enable");
  const memory=await cdp.send("Performance.getMetrics");await cdp.detach();
  return {rtc:await collectRtcStats(page),heapBytes:memory.metrics.find(x=>x.name==="JSHeapUsedSize")?.value??null};
}
async function mediaRun(browser: Browser, label: string, seconds: number, mobile=false) {
  const p=await pair(browser,mobile);
  try {
    await connect(p.owner,"vi");await connect(p.guest,"ru");
    const waitStart=performance.now();await Promise.all([remote(p.owner),remote(p.guest)]);
    const firstRemoteMs=performance.now()-waitStart;
    const samples=[];
    const samplingStart=performance.now();
    for(let i=0;i<=seconds/5;i++) {samples.push({elapsedMs:performance.now()-samplingStart,owner:await sample(p.owner),guest:await sample(p.guest)});if(i<seconds/5)await sleep(5000);}
    for(const s of samples) for(const side of [s.owner,s.guest]) {
      if(!side.rtc.some(pc=>pc.state==="connected")) throw new Error("No connected peer during media sampling");
    }
    const violations=await Promise.all([p.owner,p.guest].map(page=>page.evaluate(()=>(window as unknown as {__cspViolations:unknown[]}).__cspViolations)));
    await p.guest.getByRole("button",{name:"Turn camera off"}).click();await expect(p.guest.getByText("Your camera is off")).toBeVisible();
    await p.guest.getByRole("button",{name:"Turn camera on"}).click();await remote(p.owner);
    await p.owner.getByRole("button",{name:"Mute mic"}).click();await expect(p.owner.getByRole("button",{name:"Turn mic on"})).toBeVisible();
    await p.owner.getByRole("button",{name:"Turn mic on"}).click();
    await p.guest.evaluate(()=>{const stream=(document.querySelector("video[aria-label='You video']") as HTMLVideoElement)?.srcObject as MediaStream;Object.assign(window,{__capturedTracks:stream?.getTracks()??[]});});
    await p.guest.getByRole("button",{name:"Leave call"}).click();
    await expect(p.guest.getByRole("button",{name:"Create a private room"})).toBeVisible();
    await expect.poll(()=>p.guest.evaluate(()=>(window as unknown as {__capturedTracks:MediaStreamTrack[]}).__capturedTracks.every(t=>t.readyState==="ended"))).toBe(true);
    await expect(p.owner.getByText("The other person left the call.",{exact:false})).toBeVisible();
    report.calls.push({label,firstRemoteAfterBothJoinedMs:firstRemoteMs,samples,cspViolations:violations});
    return {durationSeconds:seconds,samples:samples.length,cspViolations:violations.flat().length};
  } finally {await p.close();}
}

async function main() {
const browser=await chromium.launch({headless:true,args:["--use-fake-device-for-media-stream","--use-fake-ui-for-media-stream","--autoplay-policy=no-user-gesture-required"]});
report.browser=browser.version();
try {
  await scenario("HTTPS headers and health timings",async()=>{
    const site=await fetch(origin);expect(site.status).toBe(200);
    expect(site.headers.get("content-security-policy")).toContain("frame-ancestors 'none'");
    expect(site.headers.get("referrer-policy")).toBe("no-referrer");
    for(let i=0;i<12;i++){const start=performance.now();const r=await fetch(origin+"/api/health");expect(r.status).toBe(200);const body=await r.json();report.health.push(performance.now()-start);expect(body.aiReady).toBe(false);}
    return {healthSamples:12,aiReady:false};
  });
  await scenario("Fresh-context desktop and mobile/CPU4 page performance",async()=>{
    for(const profile of ["desktop","mobile-cpu4"])for(let i=0;i<6;i++){
      const context=await newContext(browser,profile==="desktop"?{}:{viewport:{width:390,height:844},isMobile:true,hasTouch:true});
      try {const page=await context.newPage();const cdp=await context.newCDPSession(page);
        if(profile!=="desktop")await cdp.send("Emulation.setCPUThrottlingRate",{rate:4});
        await page.addInitScript(()=>{Object.assign(window,{__lcp:null});new PerformanceObserver(list=>{const e=list.getEntries().at(-1);if(e)Object.assign(window,{__lcp:e.startTime});}).observe({type:"largest-contentful-paint",buffered:true});});
        await page.goto("/",{waitUntil:"networkidle"});await page.waitForTimeout(350);
        const m=await page.evaluate(()=>{const n=performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming;
          const resources=performance.getEntriesByType("resource") as PerformanceResourceTiming[];
          return {ttfbMs:n.responseStart-n.startTime,domContentLoadedMs:n.domContentLoadedEventEnd-n.startTime,
            loadMs:n.loadEventEnd-n.startTime,fcpMs:performance.getEntriesByName("first-contentful-paint")[0]?.startTime??null,
            lcpMs:(window as unknown as {__lcp:number|null}).__lcp,transferBytes:n.transferSize+resources.reduce((sum,r)=>sum+r.transferSize,0),
            requests:resources.length+1,horizontalOverflow:document.documentElement.scrollWidth>innerWidth?1:0};});
        report.navigation.push({profile,...m});expect(m.horizontalOverflow).toBe(0);
      }finally{await context.close();}
    }return {desktop:6,mobileCpu4:6};
  });
  await scenario("API adversarial inputs, grants and live atomic role claims",async()=>{
    const created=await post("/api/rooms");expect(created.response.status).toBe(201);report.createMs.push(created.ms);
    const room=await created.response.json();const invite=new URL(room.guestUrl).hash.slice(8);
    const valid={version:1,roomId:room.roomId,invite,preferredLanguage:"ru",sessionNonce:randomUUID()};
    const bad=[{body:valid,headers:{Origin:"https://invalid.example"},status:403},
      {body:valid,headers:{"Sec-Fetch-Site":"cross-site"},status:403},
      {body:"x".repeat(4096),headers:{},status:400},{body:"{bad",headers:{},status:400},
      {body:{...valid,sessionNonce:"bad"},headers:{},status:400},
      {body:{...valid,preferredLanguage:"en"},headers:{},status:400},
      {body:{...valid,invite:invite+"x"},headers:{},status:403},
      {body:{...valid,roomId:"nastya_"+"0".repeat(32)},headers:{},status:403}];
    for(const item of bad){const r=await post("/api/rooms/join",item.body,item.headers);expect(r.response.status).toBe(item.status);expect(r.response.headers.get("cache-control")).toContain("no-store");}
    const claims=await Promise.all([post("/api/rooms/join",valid),post("/api/rooms/join",{...valid,sessionNonce:randomUUID()})]);
    expect(claims.map(x=>x.response.status).sort()).toEqual([200,409]);
    const good=claims.find(x=>x.response.status===200)!;const data=await good.response.json();
    const jwt=JSON.parse(Buffer.from(data.participantToken.split(".")[1],"base64url").toString());
    expect(jwt.video.canPublishSources).toEqual(["camera","microphone"]);expect(jwt.video.canPublishData).toBe(false);expect(jwt.video.roomAdmin).toBe(false);
    expect(jwt.exp-jwt.nbf).toBeLessThanOrEqual(300);
    return {negativeCases:bad.length,concurrentClaims:[200,409],humanSources:jwt.video.canPublishSources,tokenTtlSeconds:jwt.exp-jwt.nbf};
  });
  await scenario("Desktop baseline 1 (30s)",()=>mediaRun(browser,"desktop-1",30));
  await scenario("Desktop baseline 2 (30s)",()=>mediaRun(browser,"desktop-2",30));
  await scenario("Mobile viewport call (30s)",()=>mediaRun(browser,"mobile-emulation",30,true));
  await scenario("Soak call (120s)",()=>mediaRun(browser,"soak",120));
  await scenario("Permission denied and receive-only media",async()=>{
    // Auto-accept-media launch flags bypass actual denials despite Permissions API state.
    const deniedBrowser=await chromium.launch({headless:true,args:["--use-fake-device-for-media-stream","--autoplay-policy=no-user-gesture-required"]});
    const p=await pair(deniedBrowser,false,true,browser);try{
      const permission=await p.owner.evaluate(async()=>({camera:(await navigator.permissions.query({name:"camera" as PermissionName})).state,microphone:(await navigator.permissions.query({name:"microphone" as PermissionName})).state}));
      expect(permission).toEqual({camera:"denied",microphone:"denied"});await connect(p.owner,"vi",false);await connect(p.guest,"ru");await remote(p.owner);
      await expect(p.owner.getByText("Your camera is off")).toBeVisible();
      const notices=await p.owner.getByRole("alert").allTextContents();
      const outboundMedia=(await collectRtcStats(p.owner)).flatMap(pc=>pc.outbound);
      await writeFile(join(output,"permission-observation.json"),JSON.stringify({permissions:permission,receiveOnlyVideo:true,notices,outboundMediaRecords:outboundMedia.length},null,2));
      await p.owner.screenshot({path:join(output,"permission-denied.png"),fullPage:true});
      if(!notices.some(x=>x.includes("Access to the microphone was denied.")))
        throw new Error("Permission denied notice missing; visible alerts: "+notices.join(" | "));
      await expect(p.owner.getByText("Access to the microphone was denied.",{exact:false})).toBeVisible();
      await expect(p.owner.getByText("Access to the camera was denied.",{exact:false})).toBeVisible();
      await p.owner.getByRole("button",{name:"Turn mic on"}).click();
      await expect(p.owner.getByText("Access to the microphone was denied.",{exact:false})).toBeVisible();
      await p.owner.getByRole("button",{name:"Turn camera on"}).click();
      await expect(p.owner.getByText("Access to the camera was denied.",{exact:false})).toBeVisible();
      return {permissions:permission,receiveOnlyVideo:true,permissionErrorsVisible:true};
    }finally{await p.close();await deniedBrowser.close();}
  });
  await scenario("Offline join then retry; signaling interruption recovery",async()=>{
    const p=await pair(browser);try{
      await p.owner.getByRole("checkbox",{name:/I understand that camera\/microphone media/}).check();
      await p.ownerContext.setOffline(true);await p.owner.getByRole("button",{name:"Join video call"}).click();
      await expect(p.owner.getByText("You appear to be offline.",{exact:false})).toBeVisible();
      await p.ownerContext.setOffline(false);
      await p.owner.getByRole("button",{name:"Join video call"}).click();await expect(p.owner.getByRole("heading",{name:"Video call",exact:true})).toBeVisible({timeout:60000});
      await connect(p.guest,"ru");await remote(p.guest);
      const before=await p.guest.evaluate(()=>(window as unknown as {__validationSockets:WebSocket[]}).__validationSockets.length);
      const start=performance.now();
      await p.guest.evaluate(()=>{for(const s of (window as unknown as {__validationSockets:WebSocket[]}).__validationSockets)if(s.readyState===WebSocket.OPEN)s.close(4000,"synthetic validation");});
      await expect.poll(()=>p.guest.evaluate(()=>(window as unknown as {__validationSockets:WebSocket[]}).__validationSockets.filter(s=>s.readyState===WebSocket.OPEN).length),{timeout:45000}).toBeGreaterThan(0);
      await remote(p.guest);return {offlineJoinRetry:true,signalRecoveryMs:round(performance.now()-start),socketsBefore:before,injection:"client WebSocket close code 4000; not physical network/UDP loss"};
    }finally{await p.close();}
  });
} finally {await browser.close();await persist();}
const pageMetrics=Object.fromEntries(["desktop","mobile-cpu4"].map(profile=>[profile,Object.fromEntries(["ttfbMs","fcpMs","lcpMs","loadMs","transferBytes"].map(key=>[key,summary(report.navigation.filter(x=>x.profile===profile).flatMap(x=>typeof x[key]==="number"?[x[key] as number]:[]))]))]));
await writeFile(join(output,"summary.json"),JSON.stringify({cases:report.cases,healthMs:summary(report.health),createRoomMs:summary(report.createMs),joinCallMs:summary(report.joinMs),pageMetrics},null,2));
console.log("Report saved; failed scenarios: "+report.cases.filter(x=>x.status==="fail").length);
process.exitCode=report.cases.some(x=>x.status==="fail")?1:0;
}
main().catch(error=>{console.error(error instanceof Error ? error.message.split("\n")[0] : "Validation failed");process.exitCode=1;});
