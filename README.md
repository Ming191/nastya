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
