"""Entrypoint for the minimal worker process (NAS-2)."""

import argparse
import asyncio
import json
import logging

from nastya_worker.config import Settings


def health() -> dict[str, str | bool]:
    """Distinguish a runnable process from a ready inference pipeline."""
    return {"service": "ai-worker", "status": "ok", "rtcReady": False, "modelsReady": False}


async def serve() -> None:
    """Hold a healthy idle process until the RTC adapter lands in NAS-11."""
    logging.info("Worker started in bootstrap mode; RTC/AI integration is not yet installed")
    try:
        while True:
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        logging.info("Worker is stopping")
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Nastya worker bootstrap")
    parser.add_argument("--health", action="store_true", help="print process readiness as JSON")
    args = parser.parse_args()
    config = Settings.from_env()
    logging.basicConfig(level=config.log_level)
    if args.health:
        print(json.dumps(health()))
        return
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        logging.info("Stopped")


if __name__ == "__main__":
    main()
