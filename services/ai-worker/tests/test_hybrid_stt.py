import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from nastya_worker.config import Settings
from nastya_worker.providers.gateway import make_recognizer
from nastya_worker.providers.http import HttpSpeechRecognizer, InferenceError, RequestBudgetBusy
from nastya_worker.providers.hybrid import HybridSpeechRecognizer
from nastya_worker.providers.types import Transcript


def test_nonblocking_request_credit_does_not_send_a_request_or_spend_credit():
    async def go():
        calls = []

        def handle(request):
            calls.append(True)
            return httpx.Response(200, json={"text": "synthetic"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            stt = HttpSpeechRecognizer(client, "https://test/stt", min_interval_seconds=1)
            await stt.transcribe(b"RIFF", "vi")
            with pytest.raises(RequestBudgetBusy):
                await stt.transcribe(b"RIFF", "vi", wait_for_slot=False)
        assert calls == [True]

    asyncio.run(go())


def test_hybrid_uses_available_cloud_and_falls_back_without_quota_wait():
    async def go():
        remote = AsyncMock()
        local = AsyncMock()
        remote.transcribe.side_effect = [Transcript("cloud", "vi"), RequestBudgetBusy("busy")]
        local.transcribe.return_value = Transcript("local", "vi")
        stt = HybridSpeechRecognizer(remote, local)
        assert (await stt.transcribe(b"RIFF", "vi")).text == "cloud"
        local.transcribe.assert_not_called()
        assert (await stt.transcribe(b"RIFF", "vi")).text == "local"
        local.transcribe.assert_awaited_once()

    asyncio.run(go())


def test_hybrid_remote_timeout_or_error_keeps_local_available():
    async def go():
        class Slow:
            async def transcribe(self, *args, **kwargs):
                await asyncio.sleep(1)

        local = AsyncMock()
        local.transcribe.return_value = Transcript("local", "ru")
        stt = HybridSpeechRecognizer(Slow(), local, timeout_seconds=0.01)
        assert (await stt.transcribe(b"RIFF", "ru")).text == "local"
        remote = AsyncMock()
        remote.transcribe.side_effect = InferenceError("connection failed")
        assert (
            await HybridSpeechRecognizer(remote, local).transcribe(b"RIFF", "ru")
        ).text == "local"

    asyncio.run(go())


def test_hybrid_initialization_failure_uses_cloud_without_exposing_details():
    async def go():
        async with httpx.AsyncClient() as client:
            with patch(
                "nastya_worker.providers.local.load_model",
                side_effect=InferenceError("local initialization failed"),
            ):
                recognizer = make_recognizer(client, Settings(stt_provider="hybrid"))
                assert isinstance(recognizer, HttpSpeechRecognizer)

    asyncio.run(go())
