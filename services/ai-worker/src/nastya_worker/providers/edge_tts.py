"""Experimental unofficial Edge Read Aloud TTS, never used by default."""

import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from typing import Any

from nastya_worker.providers.tts import SpeechSynthesisError, SynthesizedSpeech
from nastya_worker.providers.types import Language

MAX_CHARS = 500
MAX_AUDIO_BYTES = 2 * 1024 * 1024
UTTERANCE_RE = re.compile(r"[A-Za-z0-9:_-]{1,96}\Z")
VOICES: dict[str, tuple[str, ...]] = {
    "ru": ("ru-RU-DmitryNeural", "ru-RU-SvetlanaNeural"),
    "vi": ("vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural"),
}
DEFAULT_VOICE: dict[str, str] = {
    "ru": "ru-RU-SvetlanaNeural",
    "vi": "vi-VN-HoaiMyNeural",
}


def validate_request(text: str, language: Language, voice: str, utterance_id: str) -> str:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_CHARS:
        raise SpeechSynthesisError("invalid_request")
    if any(ord(c) < 32 and c not in "\t\n" for c in text):
        raise SpeechSynthesisError("invalid_request")
    if not isinstance(utterance_id, str) or not UTTERANCE_RE.fullmatch(utterance_id):
        raise SpeechSynthesisError("invalid_request")
    if language not in VOICES:
        raise SpeechSynthesisError("invalid_request")
    if voice not in VOICES[language]:
        raise SpeechSynthesisError("unsupported_voice")
    return text.strip()


class EdgeSpeechSynthesizer:
    """Collects <=2 MiB MP3 in RAM and measures time to first real audio chunk."""

    def __init__(
        self,
        timeout_seconds: float = 8.0,
        *,
        communicate_factory: Callable[[str, str], Any] | None = None,
    ) -> None:
        if not 0.5 <= timeout_seconds <= 30:
            raise ValueError("TTS timeout must be between 0.5 and 30 seconds")
        self.timeout_seconds = timeout_seconds
        self._factory = communicate_factory

    def _communicate(self, text: str, voice: str) -> Any:
        if self._factory is not None:
            return self._factory(text, voice)
        try:
            import edge_tts
        except ImportError as exc:
            raise SpeechSynthesisError("unavailable") from exc
        return edge_tts.Communicate(text, voice)

    async def synthesize(
        self,
        text: str,
        target_language: Language,
        speaker_voice: str,
        utterance_id: str,
    ) -> SynthesizedSpeech:
        safe_text = validate_request(text, target_language, speaker_voice, utterance_id)
        started = time.perf_counter()
        audio = bytearray()
        first_ms: float | None = None
        try:
            async with asyncio.timeout(self.timeout_seconds):
                client = self._communicate(safe_text, speaker_voice)
                stream: AsyncIterator[dict] = client.stream()
                async with aclosing(stream):
                    async for chunk in stream:
                        if chunk.get("type") != "audio":
                            continue
                        payload = chunk.get("data")
                        if not isinstance(payload, bytes):
                            raise SpeechSynthesisError("invalid_audio")
                        if not payload:
                            continue
                        if first_ms is None:
                            first_ms = (time.perf_counter() - started) * 1000
                        if len(audio) + len(payload) > MAX_AUDIO_BYTES:
                            raise SpeechSynthesisError("invalid_audio")
                        audio.extend(payload)
        except TimeoutError as exc:
            raise SpeechSynthesisError("timeout") from exc
        except SpeechSynthesisError:
            raise
        except Exception as exc:
            # Upstream exceptions can contain private input, proxy URL or headers.
            status = getattr(exc, "status", None)
            code = (
                "throttled"
                if status == 429
                else ("denied" if status in (401, 403) else "unavailable")
            )
            raise SpeechSynthesisError(code) from exc
        if not audio or first_ms is None:
            raise SpeechSynthesisError("invalid_audio")
        return SynthesizedSpeech(
            utterance_id=utterance_id,
            language=target_language,
            voice=speaker_voice,
            format="audio/mpeg",
            audio=bytes(audio),
            first_audio_latency_ms=round(first_ms, 2),
            total_latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )
