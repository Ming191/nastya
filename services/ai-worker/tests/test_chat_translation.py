import asyncio
import json
import os
from unittest.mock import patch

import httpx
import pytest

from nastya_worker.config import Settings
from nastya_worker.providers.gateway import open_gateway
from nastya_worker.providers.http import ChatCompletionTranslator, InferenceError


def test_chat_gateway_contract():
    def handle(request):
        body = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer test-key"
        assert body["model"] == "test-model"
        assert body["response_format"] == {"type": "json_object"}
        assert body["messages"][1] == {"role": "user", "content": "Xin chào"}
        assert "Russian" in body["messages"][0]["content"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps({"translated_text": "Привет"})},
                    }
                ]
            },
        )

    async def go():
        settings = Settings(
            stt_url="https://test/stt",
            mt_url="https://test/chat",
            mt_provider="chat",
            mt_model="test-model",
            mt_api_key="test-key",
        )
        async with open_gateway(settings, transport=httpx.MockTransport(handle)) as gateway:
            assert (await gateway.mt.translate("Xin chào", "vi", "ru")).text == "Привет"

    asyncio.run(go())


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": [None]},
        {"choices": [{"finish_reason": "length", "message": {"content": "private"}}]},
        {"choices": [{"finish_reason": "stop", "message": {"content": "private"}}]},
        {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]},
    ],
)
def test_chat_rejects_bad_responses_without_leaking_content(payload):
    async def go():
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
        async with httpx.AsyncClient(transport=transport) as client:
            mt = ChatCompletionTranslator(client, "https://test/chat", model="test")
            with pytest.raises(InferenceError) as error:
                await mt.translate("private source", "vi", "ru")
            assert "private" not in str(error.value)

    asyncio.run(go())


@pytest.mark.parametrize(
    "env",
    [
        {"NASTYA_MT_PROVIDER": "other"},
        {"NASTYA_MT_PROVIDER": "chat"},
        {"NASTYA_STT_MIN_INTERVAL_SECONDS": "-1"},
        {"NASTYA_STT_MIN_INTERVAL_SECONDS": "nan"},
    ],
)
def test_invalid_provider_configuration(env):
    with patch.dict(os.environ, env, clear=True), pytest.raises(ValueError):
        Settings.from_env()


def test_shared_speech_request_pacing():
    from nastya_worker.providers.http import HttpSpeechRecognizer

    async def go():
        starts = []

        def handle(request):
            starts.append(asyncio.get_running_loop().time())
            return httpx.Response(200, json={"text": "test"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            stt = HttpSpeechRecognizer(client, "https://test/stt", min_interval_seconds=0.03)
            await asyncio.gather(stt.transcribe(b"RIFF", "vi"), stt.transcribe(b"RIFF", "ru"))
        assert starts[1] - starts[0] >= 0.025

    asyncio.run(go())
