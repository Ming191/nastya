"""Real local LiveKit SFU smoke: recipient-scoped chunked MP3 data, no Edge/model."""

import asyncio
import json
from datetime import timedelta

from livekit import api, rtc

from nastya_worker.providers.tts import SynthesizedSpeech
from nastya_worker.voice_transport import VOICE_TOPIC, cancel_voice, publish_voice

URL, ROOM = "ws://127.0.0.1:7880", "nastya_" + "e" * 32
KEY, SECRET = "devkey", "secret"


def jwt(identity: str, language: str = "") -> str:
    token = (
        api.AccessToken(KEY, SECRET)
        .with_identity(identity)
        .with_ttl(timedelta(minutes=5))
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=ROOM,
                can_subscribe=True,
                can_publish=False,
                can_publish_data=identity == "interpreter",
            )
        )
    )
    if language:
        token = token.with_attributes({"sourceLanguage": language})
    return token.to_jwt()


def caption(speaker: str, language: str) -> dict:
    return {
        "version": 1,
        "type": "caption.upsert",
        "roomId": ROOM,
        "speakerId": speaker,
        "utteranceId": speaker + ":1:1",
        "sequence": 1,
        "revision": 1,
        "sourceLanguage": "vi" if language == "ru" else "ru",
        "targetLanguage": language,
        "sourceText": "Synthetic test",
        "translatedText": "Synthetic MP3 test",
        "isFinal": True,
        "startOffsetMs": 0,
        "endOffsetMs": 750,
    }


async def join(identity: str, lang: str = "") -> rtc.Room:
    room = rtc.Room()
    await room.connect(URL, jwt(identity, lang))
    return room


async def main() -> None:
    worker = owner = guest = None
    received: dict[str, list[dict]] = {"owner": [], "guest": []}
    try:
        worker = await join("interpreter")
        owner, guest = rtc.Room(), rtc.Room()

        @owner.on("data_received")
        def owner_data(packet: rtc.DataPacket) -> None:
            if packet.topic == VOICE_TOPIC:
                received["owner"].append(json.loads(packet.data))

        @guest.on("data_received")
        def guest_data(packet: rtc.DataPacket) -> None:
            if packet.topic == VOICE_TOPIC:
                received["guest"].append(json.loads(packet.data))

        await owner.connect(URL, jwt("human:owner", "vi"))
        await guest.connect(URL, jwt("human:guest", "ru"))
        for _ in range(80):
            if {"human:owner", "human:guest"}.issubset(
                {person.identity for person in worker.remote_participants.values()}
            ):
                break
            await asyncio.sleep(0.1)
        assert len(worker.remote_participants) == 2

        cap = caption("human:owner", "ru")
        fake_mp3 = b"\xff\xfb" + bytes([25] * 20000)
        synthesized = SynthesizedSpeech(
            cap["utteranceId"],
            "ru",
            "ru-RU-SvetlanaNeural",
            "audio/mpeg",
            fake_mp3,
            6,
            10,
        )
        assert await publish_voice(worker, cap, synthesized) == 1
        for _ in range(100):
            if received["guest"] and received["guest"][-1]["type"] == "voice.end":
                break
            await asyncio.sleep(0.1)
        assert [packet["type"] for packet in received["guest"]] == [
            "voice.begin",
            "voice.chunk",
            "voice.chunk",
            "voice.chunk",
            "voice.end",
        ]
        assert received["owner"] == []
        assert all(p["utteranceId"] == cap["utteranceId"] for p in received["guest"])
        assert await cancel_voice(worker, cap) == 1
        for _ in range(50):
            if received["guest"][-1]["type"] == "voice.cancel":
                break
            await asyncio.sleep(0.1)
        assert received["guest"][-1]["type"] == "voice.cancel"
        assert received["owner"] == []

        second = caption("human:guest", "vi")
        synthesized2 = SynthesizedSpeech(
            second["utteranceId"],
            "vi",
            "vi-VN-HoaiMyNeural",
            "audio/mpeg",
            b"\xff\xfb" + bytes([7] * 60),
            5,
            9,
        )
        assert await publish_voice(worker, second, synthesized2) == 1
        for _ in range(50):
            if received["owner"] and received["owner"][-1]["type"] == "voice.end":
                break
            await asyncio.sleep(0.1)
        assert [p["type"] for p in received["owner"]] == [
            "voice.begin",
            "voice.chunk",
            "voice.end",
        ]
        assert received["owner"][-1]["targetLanguage"] == "vi"
        print("PASS: real SFU targeted chunked MP3 in both directions and cancel")
    finally:
        for room in (owner, guest, worker):
            if room is not None:
                await room.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
