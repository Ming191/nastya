"""Async vendor-neutral text-to-speech contract and safe optional fallback."""

from dataclasses import dataclass
from typing import Literal, Protocol

from nastya_worker.providers.types import Language

TtsFailure = Literal[
    "disabled", "invalid_request", "unsupported_voice", "timeout",
    "throttled", "denied", "unavailable", "invalid_audio",
]


@dataclass(frozen=True, slots=True)
class SynthesizedSpeech:
    utterance_id: str
    language: Language
    voice: str
    format: Literal["audio/mpeg"]
    audio: bytes
    first_audio_latency_ms: float
    total_latency_ms: float


class SpeechSynthesizer(Protocol):
    async def synthesize(
        self, text: str, target_language: Language,
        speaker_voice: str, utterance_id: str,
    ) -> SynthesizedSpeech: ...


class SpeechSynthesisError(RuntimeError):
    def __init__(self, code: TtsFailure) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class TtsOutcome:
    audio: SynthesizedSpeech | None
    reason: TtsFailure | None


class OptionalTts:
    """TTS failures are isolated from translated captions and human RTC audio."""

    def __init__(self, enabled: bool, provider: SpeechSynthesizer | None) -> None:
        self._enabled, self._provider = enabled, provider

    async def synthesize(
        self, text: str, target_language: Language,
        speaker_voice: str, utterance_id: str,
    ) -> TtsOutcome:
        if not self._enabled:
            return TtsOutcome(None, "disabled")
        if self._provider is None:
            return TtsOutcome(None, "unavailable")
        try:
            value = await self._provider.synthesize(
                text, target_language, speaker_voice, utterance_id,
            )
        except SpeechSynthesisError as exc:
            return TtsOutcome(None, exc.code)
        except Exception:
            # Never emit upstream error descriptions or submitted private text.
            return TtsOutcome(None, "unavailable")
        return TtsOutcome(value, None)
