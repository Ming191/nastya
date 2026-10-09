# Nastya

Private 1:1 Vietnamese ↔ Russian call translation. **NAS-2 is a development scaffold, not a working video call.** LiveKit room auth and media join arrive in NAS-4/NAS-5; audio AI integration and translated captions arrive in later tickets.

## Prerequisites

- Node.js **22** and npm (see `.nvmrc`).
- Python **3.12** and pip (or a compatible Python 3.12 virtual environment).
- No GPU, LiveKit account, database, or secrets are needed to run this scaffold.

## Web app

```bash
npm install
npm run dev:web
# Visit http://localhost:3000 and http://localhost:3000/api/health
```

Check frontend locally:

```bash
npm run check:web
npm run build:web
```

Dependencies are locked in the committed `package-lock.json`. Use `npm ci` for a clean reproducible installation (including in CI); run `npm install` only when updating dependencies, and commit any lockfile changes.

## Python worker

```bash
cd services/ai-worker
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m nastya_worker --health
python -m nastya_worker  # starts an idle process; Ctrl+C to stop
```

On Windows PowerShell activate with `.venv\\Scripts\\Activate.ps1` instead. Worker checks:

```bash
ruff check .
ruff format --check .
pytest -q
```

The worker deliberately **does not** join a LiveKit room, load AI models, or consume GPU in NAS-2. Its health output is honest: `rtcReady=false`, `modelsReady=false`.

## Required configuration for future integration

- [`apps/web/.env.example`](apps/web/.env.example): `NEXT_PUBLIC_LIVEKIT_URL` is public; `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET` are **server only**.
- [`services/ai-worker/.env.example`](services/ai-worker/.env.example): worker-side LiveKit URL and server secrets plus `NASTYA_LOG_LEVEL`.
- No env file is required for this scaffold. Copy example files when wiring NAS-4/NAS-11, and configure secrets through deployment environment. The Python worker **does not auto-load `.env` files**; export environment variables explicitly when needed.
- `.gitignore` excludes real env files, models, weights, personal audio, transcripts, recordings and local caches. Do not commit credentials or personal conversations.

## Repository layout

- `apps/web`: Next.js 16 App Router, TypeScript and health endpoint.
- `services/ai-worker`: minimal Python package with CLI, config and tests.
- `.github/workflows/ci.yml`: independent web and worker checks for pull requests and main.

Architecture contracts and ADRs are maintained **only in Linear**: [NAS-1](https://linear.app/cmms-warehouse/issue/NAS-1). Next ticket: [NAS-3](https://linear.app/cmms-warehouse/issue/NAS-3) feasibility, then [NAS-4](https://linear.app/cmms-warehouse/issue/NAS-4) room auth.

## Remote model API interfaces (NAS-3)

No Kaggle, model weights or GPU are required to run the provider interfaces. Future LiveKit code calls external STT and MT via `services/ai-worker/src/nastya_worker/providers/`.

Start the development-only mock server:

```bash
cd services/ai-worker
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python -m nastya_worker.mock_api
```

In another terminal (same virtual environment):

```bash
export NASTYA_STT_URL=http://127.0.0.1:8765/v1/audio/transcriptions
export NASTYA_MT_URL=http://127.0.0.1:8765/v1/translate
python -m nastya_worker --probe-translation 'Xin chào' --source vi --target ru
python -m nastya_worker --probe-translation 'Привет' --source ru --target vi
python -m nastya_worker --health
```

To use real model APIs later, replace the URLs with HTTPS endpoints and optionally set `NASTYA_STT_API_KEY`, `NASTYA_MT_API_KEY`, `NASTYA_STT_MODEL`, and `NASTYA_MT_MODEL` in the **worker environment**. Each endpoint can be probed independently. `--probe-stt path/to/file.wav --source ru` uploads WAV bytes; audio resampling/codec validation will be handled by the future RTC adapter.

HTTP contracts:

- **STT**: `POST /v1/audio/transcriptions` multipart fields `file` (speech.wav), `model`, `language` (`vi` or `ru`), `response_format=json`; response `{"text":"recognized words"}`.
- **MT**: `POST /v1/translate` JSON fields `text`, `source_language`, `target_language`, optional `model`; response `{"translated_text":"..."}`.
- Both routes optionally use `Authorization: Bearer <token>`. Internet-facing endpoints must use HTTPS, while HTTP is allowed on loopback only.

These are **Nastya-defined contracts**. A future self-hosted model server may need a lightweight wrapper to expose these exact routes. The mock returns visibly fake output, not real inference.

No WebRTC audio integration is implemented yet (`rtcReady=false`, `modelsReady=false`). Real Vietnam–Russia RTC/TURN testing requires LiveKit credentials and two real clients, tracked in [NAS-3](https://linear.app/cmms-warehouse/issue/NAS-3) and [NAS-7](https://linear.app/cmms-warehouse/issue/NAS-7). Architectural records remain exclusively in Linear.
