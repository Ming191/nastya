"""External HTTP inference adapters. No GPU/model hosting inside Nastya.

STT: OpenAI-style multipart /v1/audio/transcriptions (file, model, language).
MT: minimal JSON /v1/translate (text, source_language, target_language).
Adapters deliberately have no RTC dependency, so they can be tested independently.
"""

import httpx

from nastya_worker.providers.types import Language, Transcript, Translation

MAX_WAV_BYTES = 8 * 1024 * 1024
MAX_TEXT_CHARS = 4000


class InferenceError(RuntimeError):
    """Safe failure message without tokens or private model responses."""


def _check_language(value: str) -> None:
    if value not in ("vi", "ru"):
        raise ValueError("language must be 'vi' or 'ru'")


def _decode_text(payload: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(payload, dict):
        raise InferenceError("inference API response is not a JSON object")
    value = payload.get(field)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise InferenceError(f"inference API response must contain non-empty '{field}'")
    if len(value) > MAX_TEXT_CHARS:
        raise InferenceError("inference API response text is too long")
    return value.strip()


async def _post(client: httpx.AsyncClient, endpoint: str, **kwargs: object) -> dict:
    try:
        response = await client.post(endpoint, **kwargs)
        response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise InferenceError("inference API timeout") from exc
    except httpx.HTTPStatusError as exc:
        # Never leak upstream bodies, which can contain private audio or text.
        raise InferenceError(f"inference API returned HTTP {exc.response.status_code}") from exc
    except httpx.RequestError as exc:
        raise InferenceError("inference API connection failed") from exc
    try:
        result = response.json()
    except ValueError as exc:
        raise InferenceError("inference API returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise InferenceError("inference API response is not a JSON object")
    return result


class HttpSpeechRecognizer:
    def __init__(
        self, client: httpx.AsyncClient, endpoint: str, api_key: str = "", model: str = "whisper-1"
    ) -> None:
        self._client = client
        self._endpoint = endpoint
        self._api_key = api_key
        self._model = model

    async def transcribe(self, wav: bytes, language: Language) -> Transcript:
        _check_language(language)
        if not wav or len(wav) > MAX_WAV_BYTES:
            raise ValueError("WAV audio must contain 1 to 8388608 bytes")
        data = {"model": self._model, "language": language, "response_format": "json"}
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        payload = await _post(
            self._client,
            self._endpoint,
            data=data,
            files={"file": ("speech.wav", wav, "audio/wav")},
            headers=headers,
        )
        detected = payload.get("language", language)
        if detected not in ("vi", "ru"):
            raise InferenceError("STT returned unsupported detected language")
        return Transcript(text=_decode_text(payload, "text", allow_empty=True), language=detected)


class HttpTranslator:
    def __init__(
        self, client: httpx.AsyncClient, endpoint: str, api_key: str = "", model: str = ""
    ) -> None:
        self._client = client
        self._endpoint = endpoint
        self._api_key = api_key
        self._model = model

    async def translate(
        self, text: str, source_language: Language, target_language: Language
    ) -> Translation:
        _check_language(source_language)
        _check_language(target_language)
        if not text.strip() or len(text) > MAX_TEXT_CHARS:
            raise ValueError("text must contain 1 to 4000 characters")
        if source_language == target_language:
            return Translation(text.strip(), source_language, target_language)
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        data = {
            "text": text,
            "source_language": source_language,
            "target_language": target_language,
        }
        if self._model:
            data["model"] = self._model
        payload = await _post(self._client, self._endpoint, json=data, headers=headers)
        return Translation(
            text=_decode_text(payload, "translated_text"),
            source_language=source_language,
            target_language=target_language,
        )
