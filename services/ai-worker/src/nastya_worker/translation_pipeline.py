"""Per-speaker STT -> MT -> targeted LiveKit caption orchestration.

Only ephemeral text is processed. No inference provider may tear down human RTC.
The existing STT adapter is segment-based: the first revision is an original
transcript, not an invented token-level streaming recognition result.
"""

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nastya_worker.caption_protocol import HUMANS, MAX_BYTES, publish_caption
from nastya_worker.providers.edge_tts import DEFAULT_VOICE
from nastya_worker.providers.tts import OptionalTts
from nastya_worker.providers.types import Transcript, Translator
from nastya_worker.speaker_pipeline import Segment
from nastya_worker.voice_transport import VoiceDelivery

STATUS_TOPIC = "nastya.interpreter-status.v1"
STATUS_CODES = frozenset({"ready", "stt_unavailable", "language_mismatch", "translation_unavailable"})
MAX_TEXT = 2000


def target_language(source: str) -> str:
    if source == "vi":
        return "ru"
    if source == "ru":
        return "vi"
    raise ValueError("unknown source language")


async def publish_status(room: Any, speaker: str, source: str, code: str) -> int:
    """Send an allowlisted health code exclusively to the other language-matched human."""
    if speaker not in HUMANS or code not in STATUS_CODES:
        raise ValueError("invalid interpreter status")
    target = target_language(source)
    if room.local_participant.identity != "interpreter":
        raise ValueError("only interpreter can send health data")
    recipients = sorted(
        p.identity for p in room.remote_participants.values()
        if p.identity in HUMANS and p.identity != speaker
        and p.attributes.get("sourceLanguage") == target
    )
    if not recipients:
        return 0
    packet = {
        "version": 1,
        "type": "interpreter.status",
        "roomId": room.name,
        "speakerId": speaker,
        "targetLanguage": target,
        "code": code,
    }
    data = json.dumps(packet, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ValueError("health packet too large")
    await room.local_participant.publish_data(
        data, topic=STATUS_TOPIC, reliable=True, destination_identities=recipients
    )
    return len(recipients)


@dataclass(frozen=True, slots=True)
class CaptionObservation:
    """No private text, speech-wall-clock guess, or cross-device latency claims."""
    sample_id: str
    direction: str
    result: str
    queue_age_ms: int
    stable_caption_unix_ms: int | None
    speech_end_unix_ms: None = None
    clock_error_bound_ms: None = None


class TranslationPipeline:
    """Sequences advance per speaker, and stay monotonic during track replacement."""

    def __init__(
        self,
        room: Any,
        translator: Translator,
        *,
        is_current: Callable[[Segment], bool],
        timeout_seconds: float = 8.0,
        tts: OptionalTts | None = None,
        observe: Callable[[CaptionObservation], None] | None = None,
    ):
        if not 0.1 <= timeout_seconds <= 30:
            raise ValueError("translation timeout must be 0.1..30 seconds")
        self.room = room
        self.translator = translator
        self.is_current = is_current
        self.timeout_seconds = timeout_seconds
        self.observe = observe
        self.next_sequence = dict.fromkeys(HUMANS, 0)
        self.voices = (
            {speaker: VoiceDelivery(room, tts) for speaker in HUMANS}
            if tts is not None else {}
        )
        self.closed = False
        self.fallbacks = 0
        self.delivered = 0

    def active(self, segment: Segment) -> bool:
        return not self.closed and self.is_current(segment)

    async def _status(self, segment: Segment, code: str) -> None:
        if not self.active(segment):
            return
        try:
            await asyncio.wait_for(
                publish_status(self.room, segment.speaker, segment.language, code),
                timeout=2,
            )
        except Exception:
            # Signalling failure must not interrupt the human call or a final caption.
            pass

    async def stt_error(self, segment: Segment) -> None:
        await self._status(segment, "stt_unavailable")

    def _observe(self, segment: Segment, result: str, stable: bool) -> None:
        if self.observe is None:
            return
        try:
            self.observe(
                CaptionObservation(
                    sample_id=segment.utterance_id,
                    direction=segment.language + "-" + target_language(segment.language),
                    result=result,
                    queue_age_ms=max(0, min(120000, round((time.monotonic() - segment.created_at) * 1000))),
                    stable_caption_unix_ms=round(time.time() * 1000) if stable else None,
                )
            )
        except Exception:
            pass

    async def _publish(self, segment: Segment, caption: dict, *, retry: bool) -> int:
        attempts = 2 if retry else 1
        for attempt in range(attempts):
            if not self.active(segment):
                return 0
            try:
                return await asyncio.wait_for(publish_caption(self.room, caption), timeout=2)
            except (Exception, asyncio.CancelledError):
                if attempt + 1 < attempts and self.active(segment):
                    await asyncio.sleep(0.1)
        return 0

    async def process(self, segment: Segment, transcript: Transcript) -> None:
        if not self.active(segment):
            return
        if transcript.language != segment.language:
            await self._status(segment, "language_mismatch")
            self._observe(segment, "language_mismatch", False)
            return
        text = transcript.text.strip()
        if not text:
            return
        # The protocol and the browser both enforce a 2000-character bound.
        if len(text) > MAX_TEXT:
            await self._status(segment, "stt_unavailable")
            return

        self.next_sequence[segment.speaker] += 1
        sequence = self.next_sequence[segment.speaker]
        caption = {
            "version": 1,
            "type": "caption.upsert",
            "roomId": self.room.name,
            "speakerId": segment.speaker,
            "utteranceId": segment.utterance_id,
            "revision": 0,
            "sequence": sequence,
            "sourceLanguage": segment.language,
            "targetLanguage": target_language(segment.language),
            "sourceText": text,
            "translatedText": "",
            "translationState": "pending",
            "isFinal": False,
            "startOffsetMs": segment.start_ms,
            "endOffsetMs": segment.end_ms,
        }
        # Interrupt stale synthesized voice before showing a new utterance.
        if segment.speaker in self.voices:
            await self.voices[segment.speaker].stop()
        # A partial lets the recipient see that a phrase is being translated.
        await self._publish(segment, caption, retry=False)
        if not self.active(segment):
            return

        translated = None
        try:
            result = await asyncio.wait_for(
                self.translator.translate(text, segment.language, caption["targetLanguage"]),
                timeout=self.timeout_seconds,
            )
            if (
                result.source_language != segment.language
                or result.target_language != caption["targetLanguage"]
                or not isinstance(result.text, str)
                or not 1 <= len(result.text.strip()) <= MAX_TEXT
            ):
                raise ValueError("invalid translation response")
            translated = result.text.strip()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Includes HTTP timeout/connection errors, slow MT, and remote GPU OOM.
            self.fallbacks += 1

        if not self.active(segment):
            return
        final = {
            **caption,
            "revision": 1,
            "translatedText": translated if translated is not None else text,
            "translationState": "translated" if translated is not None else "source_only",
            "isFinal": True,
        }
        published = await self._publish(segment, final, retry=True)
        if not self.active(segment):
            return
        if translated is None:
            await self._status(segment, "translation_unavailable")
        else:
            await self._status(segment, "ready")
        self.delivered += published
        self._observe(segment, "translated" if translated is not None else "source_only", published > 0)

        # Only verified translated finals may be synthesized, never source fallback.
        if published and translated is not None and segment.speaker in self.voices:
            try:
                await self.voices[segment.speaker].replace(
                    final, DEFAULT_VOICE[final["targetLanguage"]]
                )
            except Exception:
                pass  # Voice transport must never downgrade successfully delivered text.

    async def stop_speaker(self, speaker: str) -> None:
        if speaker in self.voices:
            await self.voices[speaker].stop()

    async def shutdown(self) -> None:
        self.closed = True
        await asyncio.gather(*(voice.stop() for voice in self.voices.values()), return_exceptions=True)
