"""Vendor-independent interfaces consumed by the future RTC pipeline."""

from dataclasses import dataclass
from typing import Literal, Protocol

Language = Literal["ru", "vi"]


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    language: Language


@dataclass(frozen=True, slots=True)
class Translation:
    text: str
    source_language: Language
    target_language: Language


class SpeechRecognizer(Protocol):
    async def transcribe(self, wav: bytes, language: Language) -> Transcript: ...


class Translator(Protocol):
    async def translate(
        self, text: str, source_language: Language, target_language: Language
    ) -> Translation: ...
