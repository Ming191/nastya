"""Bounded recipient-only LiveKit MP3 data transport for optional translated voice.

Called by a future STT->MT->TTS orchestrator. This module never runs TTS,
records calls, broadcasts voice or publishes an AI microphone/media track.
"""

import asyncio
import base64
import json
from dataclasses import dataclass
from typing import Any

from nastya_worker.caption_protocol import recipient_identities, validate_caption
from nastya_worker.providers.tts import OptionalTts, SynthesizedSpeech

VOICE_TOPIC = "nastya.voice.v1"
CHUNK_BYTES = 8 * 1024
MAX_AUDIO_BYTES = 384 * 1024
MAX_CHUNKS = (MAX_AUDIO_BYTES + CHUNK_BYTES - 1) // CHUNK_BYTES
MAX_PACKET_BYTES = 15 * 1024


def _payload(message: dict) -> bytes:
    data = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_PACKET_BYTES:
        raise ValueError("voice data packet exceeds LiveKit reliable size")
    return data


def _base(caption: dict, operation: str) -> dict:
    return {
        "version": 1,
        "type": operation,
        "roomId": caption["roomId"],
        "speakerId": caption["speakerId"],
        "utteranceId": caption["utteranceId"],
        "sequence": caption["sequence"],
        "targetLanguage": caption["targetLanguage"],
    }


def _targets(room: Any, caption: dict) -> list[str]:
    validate_caption(caption)
    if room.name != caption["roomId"]:
        raise ValueError("voice room mismatch")
    if room.local_participant.identity != "interpreter":
        raise ValueError("only interpreter may send synthesized voice")
    return recipient_identities(room, caption)


def _check_audio(caption: dict, audio: SynthesizedSpeech) -> None:
    if not caption["isFinal"]:
        raise ValueError("voice requires a final translated caption")
    if audio.utterance_id != caption["utteranceId"] or (
        audio.language != caption["targetLanguage"]
    ):
        raise ValueError("voice/caption utterance or target language mismatch")
    if audio.format != "audio/mpeg" or not 1 <= len(audio.audio) <= MAX_AUDIO_BYTES:
        raise ValueError("voice MP3 exceeds bounded transport")


async def _send(room: Any, targets: list[str], message: dict) -> None:
    if not targets:
        return
    await room.local_participant.publish_data(
        _payload(message), reliable=True, destination_identities=targets, topic=VOICE_TOPIC
    )


async def publish_voice(room: Any, caption: dict, audio: SynthesizedSpeech) -> int:
    """One complete ordered, targeted and bounded MP3 utterance; no replay."""
    targets = _targets(room, caption)
    _check_audio(caption, audio)
    if not targets:
        return 0
    chunks = [audio.audio[i : i + CHUNK_BYTES] for i in range(0, len(audio.audio), CHUNK_BYTES)]
    if len(chunks) > MAX_CHUNKS:
        raise ValueError("too many voice chunks")
    await _send(
        room,
        targets,
        {
            **_base(caption, "voice.begin"),
            "mimeType": "audio/mpeg",
            "byteLength": len(audio.audio),
            "chunkCount": len(chunks),
        },
    )
    for index, chunk in enumerate(chunks):
        await _send(
            room,
            targets,
            {
                **_base(caption, "voice.chunk"),
                "index": index,
                "data": base64.b64encode(chunk).decode("ascii"),
            },
        )
    await _send(room, targets, _base(caption, "voice.end"))
    return len(targets)


async def cancel_voice(room: Any, caption: dict) -> int:
    targets = _targets(room, caption)
    await _send(room, targets, _base(caption, "voice.cancel"))
    return len(targets)


@dataclass(slots=True)
class VoiceDelivery:
    """Per-room latest-utterance-wins; cancellation cannot halt translated captions."""

    room: Any
    tts: OptionalTts
    _task: asyncio.Task | None = None
    _caption: dict | None = None
    _generation: int = 0

    async def replace(self, caption: dict, voice: str) -> None:
        """Cancel stale synthesis/data transmission before admitting newer speech."""
        _targets(self.room, caption)
        if not caption["isFinal"]:
            raise ValueError("voice delivery requires final caption")
        self._generation += 1
        previous = self._task
        previous_caption = self._caption
        if previous is not None:
            previous.cancel()
            await asyncio.gather(previous, return_exceptions=True)
        if previous_caption is not None:
            try:
                await cancel_voice(self.room, previous_caption)
            except Exception:
                pass  # Voice failure never blocks STT/MT/caption publishing.
        self._caption = caption
        generation = self._generation

        async def run() -> None:
            try:
                result = await self.tts.synthesize(
                    caption["translatedText"],
                    caption["targetLanguage"],
                    voice,
                    caption["utteranceId"],
                )
                if generation != self._generation or result.audio is None:
                    return
                await publish_voice(self.room, caption, result.audio)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Swallow non-cancellation TTS/publish failures, never log user text.
                return

        self._task = asyncio.create_task(run())

    async def stop(self) -> None:
        self._generation += 1
        previous, caption = self._task, self._caption
        self._task = None
        self._caption = None
        if previous is not None:
            previous.cancel()
            await asyncio.gather(previous, return_exceptions=True)
        if caption is not None:
            try:
                await cancel_voice(self.room, caption)
            except Exception:
                pass
