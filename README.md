# Nastya

Nastya is a private Vietnamese ↔ Russian video-call translation application, with separate web and AI-worker components.

## Requirements

- Node.js 22 and npm
- Python 3.12
- LiveKit credentials when configuring live calls
- Remote STT/translation endpoints when using AI inference

## Web

From the repository root:

```bash
npm ci
npm run dev:web
```

Open http://localhost:3000. Health endpoint: http://localhost:3000/api/health.

Checks:

```bash
npm run check:web
npm run build:web
```

## Vercel deployment

Import this repository into a Vercel project with **Root Directory** `apps/web`,
framework **Next.js**, and Node.js **22.x**. Include source files outside the root
directory so npm can use the repository workspace lockfile. The app's
`vercel.json` configures the build and a single Singapore function region.

Before deploying, set these environment variables in Vercel's **Production**
environment (use Secret/sensitive values for credentials):

- `NASTYA_PUBLIC_ORIGIN`: the exact HTTPS production URL, without a path.
- `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`: your LiveKit project.
- `NASTYA_INVITE_SECRET`: a dedicated random signing key of at least 32 bytes.
- `NASTYA_REDIS_REST_URL`, `NASTYA_REDIS_REST_TOKEN`: your Redis REST instance.
- `NASTYA_LIVEKIT_CONNECT_ORIGINS`: exact regional LiveKit HTTPS/WSS origins,
  when required by your deployment's routing.

Use independent credentials for Preview if enabling preview room access. Missing
credentials leave room creation/join unavailable. Changes to connection origins
require a new build. Confirm `/api/health` responds, create a room and test a
two-browser call after deployment. Configure trusted-edge request protection
before sharing the app publicly.

Run the Python interpreter on your own computer using the worker instructions
below; it is not hosted in a Vercel function. It needs outbound access to LiveKit
and configured STT/translation providers. Avoid sharing room invites or secrets
in logs. Review each provider's recording and retention settings.

## Python worker

```bash
cd services/ai-worker
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m nastya_worker --health
python -m nastya_worker
```

On Windows PowerShell, activate the environment with `.venv\Scripts\Activate.ps1` instead of `source`.

Run the worker checks from `services/ai-worker`:

```bash
ruff check .
ruff format --check .
pytest -q
```

## Remote model APIs

Configure these variables **in the Python worker environment**, not in the browser:

```dotenv
NASTYA_STT_URL=https://your-server.example/v1/audio/transcriptions
NASTYA_MT_URL=https://your-server.example/v1/translate
NASTYA_STT_API_KEY=your-stt-key
NASTYA_MT_API_KEY=your-mt-key
NASTYA_STT_MODEL=whisper
NASTYA_MT_MODEL=nllb
NASTYA_HTTP_TIMEOUT_SECONDS=20
```

The STT endpoint accepts a multipart request with `file` (WAV), `model`, `language` and `response_format=json`, returning `{"text":"..."}`. The translation endpoint accepts JSON with `text`, `source_language`, `target_language` and an optional `model`, returning `{"translated_text":"..."}`. The translation endpoint is a Nastya-specific contract and may require an adapter in front of your model server.

Probe the endpoints without connecting to a room:

```bash
python -m nastya_worker --probe-translation "Xin chào" --source vi --target ru
python -m nastya_worker --probe-translation "Привет" --source ru --target vi
python -m nastya_worker --probe-stt /path/to/speech.wav --source vi
```

For local development without a model server, start the **mock** API:

```bash
python -m nastya_worker.mock_api
```

Then configure `NASTYA_STT_URL=http://127.0.0.1:8765/v1/audio/transcriptions` and `NASTYA_MT_URL=http://127.0.0.1:8765/v1/translate` in another terminal. Mock responses are not real recognition or translation.

See `apps/web/.env.example` and `services/ai-worker/.env.example` for other configuration keys. The worker reads process environment variables directly; it does not automatically load `.env` files. Keep credentials out of Git.

## Project structure

- `apps/web/` — Next.js web application and server routes
- `services/ai-worker/` — Python worker and remote inference adapters
- `.github/workflows/ci.yml` — automated checks

Planning, architecture decisions and development records are maintained in [Linear](https://linear.app/cmms-warehouse/team/NAS/overview).

## Room access

Set these **server-side** environment variables in `apps/web/.env.local` or your deployment secrets:

```dotenv
NASTYA_PUBLIC_ORIGIN=http://localhost:3000
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=your-livekit-api-key
LIVEKIT_API_SECRET=your-livekit-api-secret
NASTYA_INVITE_SECRET=your-random-secret-at-least-32-bytes
```

Generate a random invite-signing secret with `openssl rand -hex 32`. Do not expose the LiveKit API secret or invite secret in the client bundle or committed files. Production room URLs require HTTPS; local WebSocket LiveKit development can use `ws://localhost:7880`.

Open the web application and select **Create a private room**. Send the displayed guest invitation link to the other participant. Each participant chooses Russian or Vietnamese and authorizes room access. The client receives a room-scoped LiveKit JWT with a five-minute join validity; invitation links expire after one hour. Token issuance and room creation require a reachable LiveKit service.

Alternatively, create/redeem rooms using JSON over HTTPS:

```bash
curl -X POST http://localhost:3000/api/rooms
curl -X POST http://localhost:3000/api/rooms/join \
  -H 'Content-Type: application/json' \
  -d '{"version":1,"roomId":"...","invite":"...","preferredLanguage":"vi","sessionNonce":"your-lowercase-uuid-v4"}'
```

Keep owner invitations private. Guest links are bearer credentials: anyone with a valid link can request the guest role while it is unoccupied. Per-process throttling is only a basic abuse backstop; use trusted edge rate limits for public deployment.

## Video calls

After configuring LiveKit under `apps/web/.env.local`, run `npm ci && npm run dev:web`. Create a room at http://localhost:3000, share the guest invitation link, and open **Continue as room owner**. On both browsers, choose a spoken language and select **Join video call**. Allow camera and microphone access when prompted. Both participants can mute/unmute, enable/disable camera, select input/output devices where supported, adjust the remote volume, copy the guest invitation, and leave the room.

Use HTTPS for remote/browser deployments so device permissions and WebRTC work reliably. If autoplay is blocked, press **Enable incoming sound**. If no camera or microphone is available, the other media stream can still be used. Speaker selection depends on browser support. LiveKit credentials are required for actual rooms and media; the localhost model mock does not replace LiveKit.

## Local RTC browser tests

Install npm dependencies, Chromium and browser prerequisites, then start a **local-only** LiveKit server in a separate terminal:

```bash
docker run --rm --network host livekit/livekit-server:v1.13.7 --dev --bind 0.0.0.0
```

Run the synthetic two-browser WebRTC suite from the repository root:

```bash
npm ci
npx playwright install --with-deps chromium
npm run test:rtc
```

The test runner starts the Next.js app with local LiveKit development credentials. The isolated browser sessions use fake camera/microphone devices. Inspect the Playwright HTML report and attached redacted RTC statistics in `apps/web/playwright-report/`. These tests need Docker and a browser, not a cloud account or AI model. They do not measure cross-country latency or prove TURN relay availability.

## RU/VI translation evaluation

The repository includes a versioned synthetic **text-only** conversation test set at `services/ai-worker/evaluation/`. From `services/ai-worker` with the Python worker environment installed:

```bash
python -m nastya_worker.evaluate validate
python -m nastya_worker.evaluate baseline --output /tmp/nastya-control.jsonl
python -m nastya_worker.evaluate score --predictions /tmp/nastya-control.jsonl --system source-copy-control --output /tmp/nastya-eval.json
python -m nastya_worker.evaluate review-template --predictions /tmp/nastya-control.jsonl --reviewer reviewer-1 --output /tmp/nastya-review.jsonl
```

Replace the control file with predictions from a hosted model: one JSON object per line, containing `id`, `hypothesis` and nullable `latency_ms`. Optional reviewed ratings can be supplied to `score` using `--reviews /path/to/completed-reviews.jsonl`. Fill the blank review template with integer scores before use. Do not treat the source-copy control or character-overlap proxy as semantic translation quality; reference translations are synthetic drafts pending bilingual review. Generated predictions, reports and reviews should remain outside the versioned corpus directory.

## Remote STT and translation benchmarking

The benchmark tools call hosted model endpoints. They do **not** download or run models locally. Set `NASTYA_MT_URL` and optional `NASTYA_MT_API_KEY` for translation; `NASTYA_STT_URL` and optional `NASTYA_STT_API_KEY` for speech recognition. The API contracts are the same as the worker providers above.

To run an apples-to-apples comparison on the 120-sentence synthetic RU/VI dataset:

```bash
cd services/ai-worker
python -m nastya_worker.benchmark_remote mt \
  --models facebook/nllb-200-distilled-600M,facebook/nllb-200-1.3B \
  --concurrency 2 --rounds 2 --warmup 1 --output-dir /tmp/nastya-mt-results
```

Model IDs are passed to your server; **the remote service must actually host and select each model**. Outputs include model-specific predictions and a JSON report of coverage, response latency (p50/p95), concurrency and measured HTTP throughput. Automatic text overlap is **not** a substitute for independent bilingual review. There is no automatic server/GPU memory measurement or first streaming partial measurement.

STT requires a separate **operator-supplied** JSON audio manifest and local PCM WAV fixtures with permission to use the recordings. The versioned RU/VI text corpus does not contain audio. The manifest schema is:

```json
{
  "schema_version": 1,
  "samples": [{
    "id": "speech_vi_001",
    "language": "vi",
    "audio_path": "clips/speech_vi_001.wav",
    "transcript": "Xin chào",
    "speech_segments_ms": [[120, 900]],
    "provenance": "consented",
    "rights_basis": "consent record reference held outside Git",
    "scenario": "speech"
  }]
}
```

Use mono PCM16 at 16 kHz, at most 30 seconds per clip. Allowed scenarios are `speech`, `noise`, `interruption` and `silence`. Keep recordings and manifests containing personal information out of the repository.

```bash
python -m nastya_worker.benchmark_remote stt \
  --models whisper-fast,whisper-quality \
  --audio-manifest /path/to/private-fixtures/manifest.json \
  --concurrency 2 --rounds 2 --output-dir /tmp/nastya-stt-results
```

Optionally configure `NASTYA_VAD_URL` (and `NASTYA_VAD_API_KEY`) to evaluate a VAD endpoint accepting a WAV file via multipart upload and returning `{"segments":[{"start_ms":120,"end_ms":900}]}`. Compare the observed word/character errors and VAD boundaries using your annotated fixture corpus. First-partial streaming latency, GPU peak VRAM, true server queue latency and cross-region performance must be measured separately where supported by the model host.


## One-room LiveKit interpreter (microphone ingestion)

### Groq API on a local Windows worker

In `services/ai-worker`, install with `uv sync --extra rtc` and create the
ignored `.env.local` file using `.env.example`. Set:

```dotenv
NASTYA_STT_URL=https://api.groq.com/openai/v1/audio/transcriptions
NASTYA_STT_MODEL=whisper-large-v3-turbo
NASTYA_MT_URL=https://api.groq.com/openai/v1/chat/completions
NASTYA_MT_PROVIDER=chat
NASTYA_MT_MODEL=qwen/qwen3.8-27b
NASTYA_STT_MIN_INTERVAL_SECONDS=3.2
```

Set both `NASTYA_STT_API_KEY` and `NASTYA_MT_API_KEY` to the Groq key, plus
the three LiveKit variables. Run `./start-worker.ps1` to discover active Nastya
rooms automatically, or `./start-worker.ps1 -RoomId nastya_<32 hex characters>`
to serve a specific room. `./start-worker.ps1 -Health` checks configuration only.
The watcher serves one call at a time, polls every five seconds and releases
the room when no owner or guest remains. Run only one watcher for this project.
Keep this computer running and connected during calls. Groq quotas apply;
the shared STT pacing introduces queue delay under simultaneous speech.
Keep API keys on this worker, outside the browser and source control.

To run the opt-in deployed caption check from the repository root:

```powershell
$env:NASTYA_CAPTION_SMOKE_WORKER_ENV = (Resolve-Path services/ai-worker/.env.local).Path
$env:NASTYA_CAPTION_SMOKE_MODE = 'translated' # or mt-failure / mt-timeout
npx tsx scripts/validate-captions.ts https://your-app.example vi.wav ru.wav C:/temp/caption-check
```

Supply original synthetic WAV fixtures: Vietnamese "Xin chào. Ngày mai chúng ta
gặp nhau lúc chín giờ." and Russian "Привет. Завтра мы встретимся в девять часов."
Use mono 48 kHz PCM16, 25 seconds of leading silence and 50 seconds of trailing
silence so the interpreter can attach before speech. The script uses fake
microphones, creates a separate test room and starts/stops its own worker.
Failure modes replace only that worker's translation endpoint with a local
HTTP failure or timeout. Reports contain timings and result flags, without
captions, invitations or keys. Never supply private call recordings as fixtures.

The worker runs on a separate computer from the web UI and makes **outbound** connections to LiveKit and a configured speech API. It does not need a GPU on this computer. In the Python worker directory:

```bash
python -m pip install -e '.[rtc]'
# Configure LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET,
# NASTYA_STT_URL and optional NASTYA_STT_API_KEY in the worker environment.
python -m nastya_worker --room nastya_0123456789abcdef0123456789abcdef
```

The room ID is the opaque ID returned by the room-creation API. Run **one** interpreter process per room. It joins under `interpreter`, subscribes exclusively to owner/guest microphone tracks, segments each independently, then invokes the configured STT and MT APIs. The other participant receives an original-text partial and a translated final caption. On MT failure the final caption explicitly displays original speech instead. Set `NASTYA_MT_TIMEOUT_SECONDS` (default 8) to bound the translation step. STT failures cannot produce a transcript; the interpreter sends a status code without inventing text. It never records audio or transcripts on disk. Disconnect/reconnect invalidates stale segments. The VAD is an uncalibrated energy heuristic: tune its settings after measuring speech/noise cases with permitted recordings. Default `python -m nastya_worker` without `--room` remains idle.

## Experimental RU/VI speech synthesis

The Python worker has an optional server-side Edge Read Aloud adapter. Speech
synthesis is **disabled by default**. When enabled in the interpreter worker, it
runs after a successfully delivered translated final caption and sends private,
recipient-only MP3 data packets. Original microphone audio remains independent.
It requires the optional Python package:

```bash
cd services/ai-worker
python -m pip install -e '.[tts]'
```

To run a deliberately opt-in experiment on three fixed synthetic sentences
per language (not user speech):

```bash
export NASTYA_TTS_ENABLED=true
python -m nastya_worker.tts_experiment --live --language ru --output-dir /tmp/nastya-tts-ru
python -m nastya_worker.tts_experiment --live --language vi --output-dir /tmp/nastya-tts-vi
```

Listen to the generated MP3 files before making any quality assessment. The
JSON report records time-to-first-audio and total request latency for each
sample; errors are recorded without exposing upstream response text.
Supported voices: `ru-RU-SvetlanaNeural`, `ru-RU-DmitryNeural`,
`vi-VN-HoaiMyNeural`, `vi-VN-NamMinhNeural`. No network request
occurs without both the opt-in feature flag and the explicit `--live` option.
The unofficial Edge Read Aloud endpoint is not a supported production API;
confirm usage permissions and availability before deployment. A web participant
must separately enable translated voice playback, which is off by default.

## Voice quality evaluation

You can prepare a bilingual 20-sentence listening rubric without connecting to
a speech service. From \`services/ai-worker\`:

```bash
python -m nastya_worker.voice_qa prepare --output-dir /tmp/nastya-voice-qa
```

To generate experimental speech for 10 original test sentences per language,
install the optional TTS provider and explicitly enable network synthesis:

```bash
python -m pip install -e '.[tts]'
export NASTYA_TTS_ENABLED=true
python -m nastya_worker.voice_qa live --live --language both   --output-dir /tmp/nastya-voice-live
```

This writes local MP3 clips and request-timing JSON. It never takes private
conversation transcripts from your call. Audio quality must be scored by human
Russian/Vietnamese listeners using the blank review form. After filling scores
and a reviewer/date, validate the scores with:

```bash
python -m nastya_worker.voice_qa review   --review-json /tmp/nastya-voice-qa/voice-review-blank.json   --output-dir /tmp/nastya-voice-review-report
```

To summarize an external privacy-safe *real session* timing trace:

```bash
python -m nastya_worker.voice_qa summarize   --session-jsonl /path/to/voice-session-events.jsonl   --output-dir /tmp/nastya-voice-session-report
```

The JSONL trace consists only of \`sampleId\`, \`direction\` (\`ru-vi\` or
\`vi-ru\`), \`source\` (\`live\` or \`synthetic\`), \`outcome\`,
\`queueAgeMs\`, \`speechEndUnixMs\`, \`firstPlaybackUnixMs\`, and
\`clockErrorBoundMs\`. Timestamp fields can be null when playback never
starts. Browser/server clocks must have a documented synchronisation error
bound; synthetic measurements never satisfy real-session acceptance.
Current production calls do not export such traces automatically.

## Room security settings

For production (including loopback origins) or any public deployment, configure the server-only `NASTYA_REDIS_REST_URL`
and `NASTYA_REDIS_REST_TOKEN` credentials (from a private Redis REST
deployment with atomic Lua EVAL), in addition to the LiveKit and invite-signing
variables. The app **rejects all room creation and join requests** if the shared
security store is missing or unavailable. Non-production local HTTP loopback testing uses a
process-local security store and must **never** be exposed to the public internet.

Creation is limited globally to 60 requests/minute and joins to 300/minute in
the shared Redis gate, in addition to smaller local backstops. Invite redemption
binds each human role to the **first browser session** to claim it until the
room invite expires; that same tab can rejoin/reload, but transferring an already
claimed invite to a new browser is rejected. Create a new room if the claiming
tab or its session storage is lost. The initial invite link remains a bearer
secret before first redemption: share it only with the intended participant.
Room invites live for one hour; LiveKit join JWTs are limited to five minutes
and to a specific room/identity. The server allows only camera/microphone
track sources from human participants, not arbitrary AI data packets.

Browser and API responses have no-referrer, nosniff, anti-framing and CSP
policies. The user must affirm the media/AI processing notice before joining.

Set `LIVEKIT_URL` before building the web app: browser CSP allows that exact
WebSocket origin and its HTTP/HTTPS equivalent. If the LiveKit deployment routes
clients to additional regional hosts, add their exact HTTPS/WSS origins to the
comma-separated `NASTYA_LIVEKIT_CONNECT_ORIGINS` build environment variable.
Rebuild after changing these settings. Wildcards and arbitrary HTTPS/WSS hosts
are rejected. Redis REST redirects are rejected; use its direct HTTPS endpoint.
CLI callers must generate a lowercase UUIDv4 `sessionNonce` and retain the same
value to rejoin the same room/role.

Neither the app nor the model worker persist call audio or transcripts by
default; configured inference providers and LiveKit may have separate data
retention rules. Review those providers and obtain user consent before use.

Redis REST must be reachable by the **server runtime only** (not via client
environment variables). Avoid exposing room invitations, Redis tokens,
LiveKit API secrets or Join API responses in analytics, server access logs,
support screenshots, monitoring, public chat or error messages. Room deletion,
join-token revocation and production-hosted edge DDoS protection require
deployment-side coordination; this app cannot retroactively invalidate an
already-issued LiveKit JWT or stop a compromised invite from being claimed
first.

