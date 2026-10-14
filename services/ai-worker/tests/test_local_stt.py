import asyncio
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
