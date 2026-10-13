"""Lifecycle for HTTP inference clients. The LiveKit adapter will depend on this."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from nastya_worker.config import Settings
from nastya_worker.providers.http import (
    ChatCompletionTranslator,
    HttpSpeechRecognizer,
    HttpTranslator,
)
from nastya_worker.providers.types import SpeechRecognizer, Translator


@dataclass(frozen=True, slots=True)
class InferenceGateway:
    stt: SpeechRecognizer
    mt: Translator


def make_translator(client: httpx.AsyncClient, settings: Settings) -> Translator:
    adapter = ChatCompletionTranslator if settings.mt_provider == "chat" else HttpTranslator
    return adapter(client, settings.mt_url, settings.mt_api_key, settings.mt_model)


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
            stt=HttpSpeechRecognizer(
                client,
                settings.stt_url,
                settings.stt_api_key,
                settings.stt_model,
                settings.stt_min_interval_seconds,
            ),
            mt=make_translator(client, settings),
        )
