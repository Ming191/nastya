"""Lifecycle for HTTP inference clients. The LiveKit adapter will depend on this."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from nastya_worker.config import Settings
from nastya_worker.providers.http import (
    ChatCompletionTranslator,
    HttpSpeechRecognizer,
    HttpTranslator,
    InferenceError,
)
from nastya_worker.providers.types import SpeechRecognizer, Translator


@dataclass(frozen=True, slots=True)
class InferenceGateway:
    stt: SpeechRecognizer
    mt: Translator


def make_recognizer(client: httpx.AsyncClient, settings: Settings) -> SpeechRecognizer:
    local = None
    if settings.stt_provider in {"local", "hybrid"}:
        from nastya_worker.providers.local import LocalSpeechRecognizer

        try:
            local = LocalSpeechRecognizer(
                settings.stt_local_model,
                settings.stt_model_directory,
                settings.stt_local_compute_type,
            )
        except InferenceError:
            if settings.stt_provider == "local":
                raise
            logging.getLogger(__name__).warning("Local STT unavailable; using HTTP recognizer")
        if settings.stt_provider == "local":
            return local
    remote = HttpSpeechRecognizer(
        client,
        settings.stt_url,
        settings.stt_api_key,
        settings.stt_model,
        settings.stt_min_interval_seconds,
        settings.stt_burst_requests,
    )
    if settings.stt_provider == "hybrid" and local is not None:
        from nastya_worker.providers.hybrid import HybridSpeechRecognizer

        return HybridSpeechRecognizer(remote, local)
    return remote


def make_translator(client: httpx.AsyncClient, settings: Settings) -> Translator:
    if settings.mt_provider == "chat":
        return ChatCompletionTranslator(
            client,
            settings.mt_url,
            settings.mt_api_key,
            settings.mt_model,
            settings.mt_reasoning_effort,
        )
    return HttpTranslator(client, settings.mt_url, settings.mt_api_key, settings.mt_model)


@asynccontextmanager
async def open_gateway(
    settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
) -> AsyncIterator[InferenceGateway]:
    """One HTTP connection pool reused between both speaker directions."""
    if not settings.api_ready:
        raise ValueError("configure NASTYA_STT_URL and NASTYA_MT_URL")
    async with httpx.AsyncClient(
        timeout=settings.http_timeout_seconds, transport=transport
    ) as client:
        yield InferenceGateway(
            stt=make_recognizer(client, settings),
            mt=make_translator(client, settings),
        )
