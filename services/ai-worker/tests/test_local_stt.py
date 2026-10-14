import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nastya_worker.config import Settings
from nastya_worker.providers.http import InferenceError
from nastya_worker.providers.local import LocalSpeechRecognizer


def test_local_provider_does_not_require_remote_stt_endpoint():
    assert Settings(stt_provider="local", mt_url="https://test/mt").api_ready


def test_local_recognizer_is_stateless_and_uses_fixed_language():
    calls = []

    class Model:
        def transcribe(self, audio, **kwargs):
            calls.append(kwargs)
            return [SimpleNamespace(text=" Xin chào ")], None

    async def go():
        with patch("nastya_worker.providers.local.load_model", return_value=Model()):
            stt = LocalSpeechRecognizer()
            result = await stt.transcribe(b"RIFF", "vi")
            assert result.text == "Xin chào"
            assert result.language == "vi"
            assert calls[0]["language"] == "vi"
            assert calls[0]["condition_on_previous_text"] is False
            assert calls[0]["beam_size"] == 1
            with pytest.raises(ValueError):
                await stt.transcribe(b"RIFF", "en")

    asyncio.run(go())


def test_cancelled_native_inference_returns_promptly_without_gpu_overlap():
    entered = threading.Event()
    release = threading.Event()
    calls = []

    class Model:
        def transcribe(self, audio, **kwargs):
            calls.append(kwargs["language"])
            entered.set()
            release.wait(2)
            return [SimpleNamespace(text="late synthetic result")], None

    async def go():
        with patch("nastya_worker.providers.local.load_model", return_value=Model()):
            stt = LocalSpeechRecognizer()
            task = asyncio.create_task(stt.transcribe(b"RIFF", "vi"))
            try:
                while not entered.is_set():
                    await asyncio.sleep(0.001)
                started = time.perf_counter()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 0.1)
                assert time.perf_counter() - started < 0.1
                with pytest.raises(InferenceError, match="busy"):
                    await stt.transcribe(b"RIFF", "ru")
                assert calls == ["vi"]
            finally:
                release.set()
                if stt._inflight is not None:
                    await stt._inflight
            result = await stt.transcribe(b"RIFF", "ru")
            assert result.language == "ru"
            assert calls == ["vi", "ru"]

    asyncio.run(go())


def test_local_cuda_error_is_sanitized():
    class Model:
        def transcribe(self, audio, **kwargs):
            raise RuntimeError("private audio and driver information")

    async def go():
        with patch("nastya_worker.providers.local.load_model", return_value=Model()):
            stt = LocalSpeechRecognizer()
            with pytest.raises(InferenceError) as error:
                await stt.transcribe(b"RIFF", "ru")
            assert str(error.value) == "local STT inference failed"

    asyncio.run(go())
