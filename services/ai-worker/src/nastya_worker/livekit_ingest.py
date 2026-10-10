"""LiveKit RTC reader for one configured, operator-authorized private room.

No AI output is published here; STT -> MT/caption events belong to NAS-19.
"""

import asyncio
import logging
import os
import re
from datetime import timedelta
from typing import Any

import httpx

from nastya_worker.config import Settings
from nastya_worker.providers.http import HttpSpeechRecognizer
from nastya_worker.speaker_pipeline import HUMANS, SpeakerPipeline, source_language

LOGGER = logging.getLogger(__name__)
ROOM_ID = re.compile(r"^nastya_[0-9a-f]{32}$")


def room_settings(room_id: str, env: dict[str, str] | None = None) -> dict[str, str]:
    e = os.environ if env is None else env
    if not ROOM_ID.fullmatch(room_id):
        raise ValueError("invalid Nastya room ID")
    keys = ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
    if any(not e.get(key) for key in keys):
        raise ValueError("LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET required")
    url = e["LIVEKIT_URL"]
    if not url.startswith("wss://") and not (
        url.startswith("ws://127.0.0.1:") or url.startswith("ws://localhost:")
    ):
        raise ValueError("LiveKit URL must use WSS (WS localhost is development only)")
    return {key: e[key] for key in keys}


def worker_token(room_id: str, keys: dict[str, str]) -> str:
    from livekit import api

    return (
        api.AccessToken(keys["LIVEKIT_API_KEY"], keys["LIVEKIT_API_SECRET"])
        .with_identity("interpreter")
        .with_name("Nastya Interpreter")
        .with_ttl(timedelta(minutes=5))
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_id,
                can_subscribe=True,
                can_publish=False,
                can_publish_data=False,
                can_update_own_metadata=False,
            )
        )
        .to_jwt()
    )


class RoomAudioReceiver:
    """Owns exactly one LiveKit room, two isolated microphone capture tasks."""

    def __init__(self, room: Any, rtc: Any, recognizer: HttpSpeechRecognizer):
        self.room = room
        self.rtc = rtc
        self.recognizer = recognizer
        self.inference = asyncio.Semaphore(1)
        self.stop_event = asyncio.Event()
        self.readers: dict[str, tuple[str, asyncio.Task]] = {}
        self.epochs: dict[str, int] = dict.fromkeys(HUMANS, 0)
        self.processed = 0
        self.failures = 0
        self._active = True

    def accepts(self, publication: Any, participant: Any) -> bool:
        if participant.identity not in HUMANS:
            return False
        return (
            publication.kind == self.rtc.TrackKind.KIND_AUDIO
            and publication.source == self.rtc.TrackSource.SOURCE_MICROPHONE
            and source_language(participant.attributes) is not None
        )

    def _try_subscribe(self, publication: Any, participant: Any) -> None:
        if not self._active or not self.accepts(publication, participant):
            return
        if not publication.subscribed:
            publication.set_subscribed(True)

    def register(self) -> None:
        @self.room.on("track_published")
        def on_published(publication: Any, participant: Any) -> None:
            self._try_subscribe(publication, participant)

        @self.room.on("participant_attributes_changed")
        def on_attributes(_attributes: dict, participant: Any) -> None:
            if participant.identity in HUMANS:
                for publication in participant.track_publications.values():
                    self._try_subscribe(publication, participant)

        @self.room.on("track_subscribed")
        def on_subscribed(track: Any, publication: Any, participant: Any) -> None:
            if self._active and self.accepts(publication, participant):
                self._attach(track, publication, participant)

        @self.room.on("track_unsubscribed")
        def on_unsubscribed(_track: Any, publication: Any, participant: Any) -> None:
            self._detach(participant.identity, publication.sid)

        @self.room.on("participant_disconnected")
        def on_departure(participant: Any) -> None:
            self._detach(participant.identity)

        @self.room.on("disconnected")
        def on_disconnected() -> None:
            self.stop_event.set()

    def discover(self) -> None:
        for participant in self.room.remote_participants.values():
            for publication in participant.track_publications.values():
                self._try_subscribe(publication, participant)

    def _detach(self, identity: str, sid: str | None = None) -> None:
        current = self.readers.get(identity)
        if current and (sid is None or current[0] == sid):
            self.readers.pop(identity)
            self.epochs[identity] += 1
            current[1].cancel()

    def _attach(self, track: Any, publication: Any, participant: Any) -> None:
        identity = participant.identity
        if identity not in HUMANS:
            return
        current = self.readers.get(identity)
        if current and current[0] == publication.sid and not current[1].done():
            return
        self._detach(identity)
        self.epochs[identity] += 1
        epoch = self.epochs[identity]
        language = source_language(participant.attributes)
        if language is None:
            return
        task = asyncio.create_task(self._capture(identity, epoch, language, track))
        self.readers[identity] = (publication.sid, task)

    async def _on_transcript(self, segment: Any, transcript: Any) -> None:
        identity = segment.speaker
        if not self._active or self.epochs[identity] != segment.generation:
            return
        # No content logs; caption publication will be added in NAS-19.
        self.processed += 1
        LOGGER.info(
            "stt_ok speaker=%s utterance=%s start_ms=%d end_ms=%d",
            identity,
            segment.utterance_id,
            segment.start_ms,
            segment.end_ms,
        )

    async def _capture(self, identity: str, epoch: int, language: str, track: Any) -> None:
        stream = None
        pipeline = SpeakerPipeline(
            identity,
            language,
            epoch,
            self.recognizer,
            self.inference,
            self._on_transcript,
        )
        pipeline.start()
        try:
            stream = self.rtc.AudioStream.from_track(
                track=track, sample_rate=16000, num_channels=1, capacity=50
            )
            async for event in stream:
                if not self._active or self.epochs[identity] != epoch:
                    break
                frame = event.frame
                if frame.sample_rate != 16000 or frame.num_channels != 1:
                    # Explicit frame format contract; no unsafe interpretation.
                    self.failures += 1
                    break
                pipeline.feed(bytes(frame.data))
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failures += 1
            LOGGER.warning("audio stream failed speaker=%s", identity)
        finally:
            await pipeline.stop()
            if stream is not None:
                await stream.aclose()

    async def shutdown(self) -> None:
        self._active = False
        tasks = [task for _, task in self.readers.values()]
        for identity in list(self.readers):
            self._detach(identity)
        self.readers.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def serve_room(room_id: str, settings: Settings) -> None:
    """One worker instance / one room, reconnect with fresh epoch after disconnect."""
    if not settings.stt_url:
        raise ValueError("NASTYA_STT_URL is required for RTC ingestion")
    keys = room_settings(room_id)
    from livekit import rtc

    backoff_seconds = 1
    async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as client:
        recognizer = HttpSpeechRecognizer(
            client, settings.stt_url, settings.stt_api_key, settings.stt_model
        )
        while True:
            room = rtc.Room()
            receiver = RoomAudioReceiver(room, rtc, recognizer)
            receiver.register()
            try:
                await room.connect(
                    keys["LIVEKIT_URL"],
                    worker_token(room_id, keys),
                    options=rtc.RoomOptions(auto_subscribe=False),
                )
                backoff_seconds = 1
                LOGGER.info("interpreter connected room=%s", room_id)
                receiver.discover()
                await receiver.stop_event.wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Any RTC failure is isolated from the human video/audio connection.
                LOGGER.warning("interpreter RTC unavailable; will retry")
            finally:
                await receiver.shutdown()
                try:
                    await room.disconnect()
                except Exception:
                    LOGGER.warning("interpreter RTC close failed")
            await asyncio.sleep(backoff_seconds)
            backoff_seconds = min(10, backoff_seconds * 2)
