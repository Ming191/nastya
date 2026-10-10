"""Independent opt-in config, never required for STT/MT/caption startup."""

import os
from dataclasses import dataclass

from nastya_worker.providers.edge_tts import EdgeSpeechSynthesizer
from nastya_worker.providers.tts import OptionalTts


@dataclass(frozen=True, slots=True)
class TtsSettings:
    enabled: bool = False
    provider: str = "edge"
    timeout_seconds: float = 8.0

    @classmethod
    def from_env(cls) -> "TtsSettings":
        flag = os.getenv("NASTYA_TTS_ENABLED", "false").lower()
        if flag not in ("false", "true", "0", "1"):
            raise ValueError("NASTYA_TTS_ENABLED must be true or false")
        provider = os.getenv("NASTYA_TTS_PROVIDER", "edge").lower()
        if provider != "edge":
            raise ValueError("NASTYA_TTS_PROVIDER currently supports edge")
        try:
            timeout = float(os.getenv("NASTYA_TTS_TIMEOUT_SECONDS", "8"))
        except ValueError as exc:
            raise ValueError("NASTYA_TTS_TIMEOUT_SECONDS must be numeric") from exc
        if not 0.5 <= timeout <= 30:
            raise ValueError("NASTYA_TTS_TIMEOUT_SECONDS must be 0.5..30")
        return cls(flag in ("true", "1"), provider, timeout)

    def build(self) -> OptionalTts:
        if not self.enabled:
            return OptionalTts(False, None)
        return OptionalTts(True, EdgeSpeechSynthesizer(self.timeout_seconds))
