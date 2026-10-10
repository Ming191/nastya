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
  -d '{"version":1,"roomId":"...","invite":"...","preferredLanguage":"vi"}'
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

The worker runs on a separate computer from the web UI and makes **outbound** connections to LiveKit and a configured speech API. It does not need a GPU on this computer. In the Python worker directory:

```bash
python -m pip install -e '.[rtc]'
# Configure LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET,
# NASTYA_STT_URL and optional NASTYA_STT_API_KEY in the worker environment.
python -m nastya_worker --room nastya_0123456789abcdef0123456789abcdef
```

The room ID is the opaque ID returned by the room-creation API. Run **one** interpreter process per room. It joins under `interpreter`, subscribes exclusively to owner/guest microphone tracks, segments each independently, and forwards temporary WAV utterances to your external STT endpoint. It never records audio or transcripts on disk. Disconnect/reconnect invalidates stale segments. The VAD is an uncalibrated energy heuristic: tune its settings after measuring speech/noise cases with permitted recordings. Captions and translations are separate functionality; this command currently exercises audio ingestion and transcription only. Default `python -m nastya_worker` without `--room` remains idle.

## Experimental RU/VI speech synthesis

The Python worker has an optional server-side Edge Read Aloud adapter. Speech
synthesis is **disabled by default**; it is not connected to the video call
or translated captions. It requires the optional Python package:

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
confirm usage permissions and availability before deployment.

