"""CI-only real LiveKit SFU microphone ingress integration smoke.

Starts with a LiveKit Docker --dev server on localhost:7880. Synthetic sine-like
PCM is generated in memory, never saved. No cloud, credentials, or model needed.
"""

import asyncio
import time
from array import array
from datetime import timedelta

from livekit import api, rtc

from nastya_worker.livekit_ingest import RoomAudioReceiver
from nastya_worker.providers.types import Transcript

URL = "ws://127.0.0.1:7880"
API_KEY = "devkey"
API_SECRET = "secret"
ROOM = "nastya_" + "f" * 32


def token(identity: str, language: str = "") -> str:
    builder = (
        api.AccessToken(API_KEY, API_SECRET)
        .with_identity(identity)
        .with_ttl(timedelta(minutes=5))
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=ROOM,
                can_publish=identity != "interpreter",
                can_subscribe=identity == "interpreter",
                can_publish_data=False,
            )
        )
    )
    if language:
        builder = builder.with_attributes({"sourceLanguage": language})
    return builder.to_jwt()


async def send_audio(source: rtc.AudioSource, amplitude: int, frames: int) -> None:
    for _ in range(frames):
        frame = rtc.AudioFrame.create(16000, 1, 320)
        raw = array("h", [amplitude] * 320).tobytes()
        memoryview(frame.data).cast("B")[:] = raw
        await source.capture_frame(frame)
        await asyncio.sleep(0.02)


async def connect(identity: str, language: str = "") -> rtc.Room:
    room = rtc.Room()
    await room.connect(URL, token(identity, language))
    return room


async def main() -> None:
    completed = []

    class FakeStt:
        async def transcribe(self, wav: bytes, language: str) -> Transcript:
            assert wav[:4] == b"RIFF"
            return Transcript("synthetic-RTC-only", language)

    worker = rtc.Room()
    receiver = RoomAudioReceiver(worker, rtc, FakeStt())
    receiver.register()
    owner = guest = None
    try:
        await worker.connect(
            URL, token("interpreter"), options=rtc.RoomOptions(auto_subscribe=False)
        )
        owner = await connect("human:owner", "vi")
        guest = await connect("human:guest", "ru")
        owner_source = rtc.AudioSource(16000, 1)
        guest_source = rtc.AudioSource(16000, 1)
        owner_track = rtc.LocalAudioTrack.create_audio_track("owner-microphone", owner_source)
        guest_track = rtc.LocalAudioTrack.create_audio_track("guest-microphone", guest_source)
        opts = rtc.TrackPublishOptions()
        opts.source = rtc.TrackSource.SOURCE_MICROPHONE
        await asyncio.gather(
            owner.local_participant.publish_track(owner_track, opts),
            guest.local_participant.publish_track(guest_track, opts),
        )
        end = time.monotonic() + 12
        while time.monotonic() < end and set(receiver.readers) != {"human:owner", "human:guest"}:
            await asyncio.sleep(0.1)
        assert set(receiver.readers) == {"human:owner", "human:guest"}, (
            "worker did not subscribe two distinct human microphone tracks"
        )
        await asyncio.gather(
            send_audio(owner_source, 11000, 20),
            send_audio(guest_source, 12000, 20),
        )
        await asyncio.gather(
            send_audio(owner_source, 0, 27),
            send_audio(guest_source, 0, 27),
        )
        end = time.monotonic() + 10
        while time.monotonic() < end and receiver.processed < 2:
            await asyncio.sleep(0.1)
        assert receiver.processed >= 2, "no complete per-speaker speech segments reached STT"
        assert receiver.failures == 0
        assert receiver.inference._value == 1
        completed.append("real SFU two-microphone STT ingest")
        print("PASS:", ", ".join(completed))
    finally:
        await receiver.shutdown()
        if owner is not None:
            await owner.disconnect()
        if guest is not None:
            await guest.disconnect()
        await worker.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
