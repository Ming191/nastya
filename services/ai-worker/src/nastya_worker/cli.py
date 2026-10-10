"""Entrypoint for the minimal worker and external provider smoke testing."""

import argparse
import asyncio
import json
import logging
from pathlib import Path

import httpx

from nastya_worker.config import Settings
from nastya_worker.providers.http import HttpSpeechRecognizer, HttpTranslator, InferenceError


def health(settings: Settings | None = None) -> dict[str, str | bool]:
    """Report configuration without claiming models/RTC work yet."""
    settings = settings or Settings.from_env()
    return {
        "service": "ai-worker",
        "status": "ok",
        "rtcReady": False,
        "modelsReady": False,
        "externalApiConfigured": settings.api_ready,
    }


async def serve() -> None:
    """Hold an idle process until explicitly started with --room."""
    logging.info("Idle worker: use --room <room-id> for LiveKit audio ingestion")
    try:
        while True:
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        logging.info("Worker is stopping")
        raise


async def probe(args: argparse.Namespace, settings: Settings) -> None:
    async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as client:
        if args.probe_translation is not None:
            if not settings.mt_url:
                raise ValueError("configure NASTYA_MT_URL to probe translation")
            translator = HttpTranslator(
                client, settings.mt_url, settings.mt_api_key, settings.mt_model
            )
            result = await translator.translate(args.probe_translation, args.source, args.target)
            print(json.dumps({"translated_text": result.text}, ensure_ascii=False))
        else:
            if not settings.stt_url:
                raise ValueError("configure NASTYA_STT_URL to probe transcription")
            recognizer = HttpSpeechRecognizer(
                client, settings.stt_url, settings.stt_api_key, settings.stt_model
            )
            wav = Path(args.probe_stt).read_bytes()
            result = await recognizer.transcribe(wav, args.source)
            print(json.dumps({"text": result.text}, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="Nastya worker and external AI API smoke tests")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--health", action="store_true", help="print process status as JSON")
    group.add_argument("--room", metavar="ROOM_ID", help="subscribe one LiveKit room to remote STT")
    group.add_argument("--probe-translation", metavar="TEXT", help="translate sample text via API")
    group.add_argument("--probe-stt", metavar="WAV", help="transcribe WAV via API")
    parser.add_argument("--source", choices=("vi", "ru"), default="vi")
    parser.add_argument("--target", choices=("vi", "ru"), default="ru")
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
        logging.basicConfig(level=settings.log_level)
        if args.health:
            print(json.dumps(health(settings)))
        elif args.room:
            from nastya_worker.livekit_ingest import serve_room

            asyncio.run(serve_room(args.room, settings))
        elif args.probe_translation is not None or args.probe_stt is not None:
            asyncio.run(probe(args, settings))
        else:
            asyncio.run(serve())
    except (ValueError, InferenceError, OSError) as exc:
        parser.exit(2, f"error: {exc}\n")
    except KeyboardInterrupt:
        logging.info("Stopped")


if __name__ == "__main__":
    main()
