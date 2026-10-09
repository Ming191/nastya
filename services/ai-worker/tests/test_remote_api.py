import asyncio
import json
import os
from unittest.mock import patch

import httpx
import pytest

from nastya_worker.config import Settings
from nastya_worker.providers.gateway import open_gateway
from nastya_worker.providers.http import (
    MAX_WAV_BYTES,
    HttpSpeechRecognizer,
    HttpTranslator,
    InferenceError,
)


def run(coro):
    return asyncio.run(coro)


def test_settings_rejects_plain_http_to_remote_machine():
    with patch.dict(os.environ, {"NASTYA_STT_URL": "http://example.com/v1/stt"}, clear=True):
        with pytest.raises(ValueError, match="HTTPS"):
            Settings.from_env()


def test_settings_supports_https_and_localhost():
    with patch.dict(
        os.environ,
        {"NASTYA_STT_URL": "https://ai.example.net/v1/audio/transcriptions",
         "NASTYA_MT_URL": "http://127.0.0.1:8765/v1/translate"},
        clear=True,
    ):
        settings = Settings.from_env()
        assert settings.api_ready
        assert settings.http_timeout_seconds == 20


@pytest.mark.parametrize("bad", ["https://user:pass@host/t", "https://host/t?q=1", "ftp://x/t"])
def test_settings_rejects_unsafe_urls(bad):
    with patch.dict(os.environ, {"NASTYA_MT_URL": bad}, clear=True):
        with pytest.raises(ValueError):
            Settings.from_env()


def test_settings_rejects_invalid_timeout():
    with patch.dict(os.environ, {"NASTYA_HTTP_TIMEOUT_SECONDS": "-1"}, clear=True):
        with pytest.raises(ValueError, match="NASTYA_HTTP_TIMEOUT_SECONDS"):
            Settings.from_env()


def test_speech_to_translation_round_trip_and_auth_headers():
    requests = []

    def handle(request: httpx.Request):
        requests.append(request)
        assert request.headers.get("authorization") == "Bearer demo-secret"
        if request.url.path == "/v1/audio/transcriptions":
            assert b'name="file"' in request.content
            assert b'name="language"' in request.content
            assert b"vi" in request.content
            return httpx.Response(200, json={"text": "Xin chào"})
        assert request.url.path == "/v1/translate"
        body = json.loads(request.content)
        assert body == {"text": "Xin chào", "source_language": "vi",
                        "target_language": "ru", "model": "nllb"}
        return httpx.Response(200, json={"translated_text": "Привет"})

    async def go():
        settings = Settings(
            stt_url="https://gateway.test/v1/audio/transcriptions",
            mt_url="https://gateway.test/v1/translate",
            stt_api_key="demo-secret",
            mt_api_key="demo-secret",
            mt_model="nllb",
        )
        async with open_gateway(settings, transport=httpx.MockTransport(handle)) as gateway:
            transcript = await gateway.stt.transcribe(b"RIFFfakeWAV", "vi")
            translation = await gateway.mt.translate(transcript.text, "vi", "ru")
            assert transcript.text == "Xin chào"
            assert translation.text == "Привет"

    run(go())
    assert len(requests) == 2


def test_reverse_direction_and_no_api_key():
    def handler(request: httpx.Request):
        assert "authorization" not in request.headers
        assert request.url.path == "/v1/translate"
        return httpx.Response(200, json={"translated_text": "Chào"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            mt = HttpTranslator(client, "https://gateway.test/v1/translate")
            actual = await mt.translate("Привет", "ru", "vi")
            assert actual.text == "Chào"

    run(go())


def test_short_circuit_same_language():
    def handler(request: httpx.Request):
        raise AssertionError("same language should not make API call")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            mt = HttpTranslator(client, "https://gateway.test/v1/translate")
            assert (await mt.translate("Здравствуйте", "ru", "ru")).text == "Здравствуйте"

    run(go())


def test_errors_do_not_include_private_text():
    def handler(request: httpx.Request):
        return httpx.Response(500, text="PRIVATE_USER_AUDIO_AND_TOKEN")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            mt = HttpTranslator(client, "https://gateway.test/v1/translate")
            with pytest.raises(InferenceError, match="HTTP 500") as exc:
                await mt.translate("confidential", "vi", "ru")
            assert "PRIVATE" not in str(exc.value)
            assert "confidential" not in str(exc.value)

    run(go())


def test_rejects_malformed_response_and_oversized_audio():
    def handler(request: httpx.Request):
        return httpx.Response(200, json={"unexpected": "value"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            stt = HttpSpeechRecognizer(client, "https://gateway.test/v1/audio/transcriptions")
            with pytest.raises(ValueError, match="WAV audio"):
                await stt.transcribe(b"x" * (MAX_WAV_BYTES + 1), "vi")
            with pytest.raises(InferenceError, match="non-empty 'text'"):
                await stt.transcribe(b"RIFF", "vi")

    run(go())


def test_connection_failure_has_safe_error():
    def handler(request: httpx.Request):
        raise httpx.ConnectError("private-hostname", request=request)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            mt = HttpTranslator(client, "https://gateway.test/v1/translate")
            with pytest.raises(InferenceError, match="connection failed") as exc:
                await mt.translate("hello", "vi", "ru")
            assert "private-hostname" not in str(exc.value)

    run(go())
