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

`npm install` resolves dependencies for this first scaffold. **There is no committed npm lockfile yet**; pinning a full resolved dependency graph and replacing CI install with `npm ci` is follow-up work before claiming fully deterministic installs. The CI workflow currently uses the exact direct versions in `apps/web/package.json`.

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
