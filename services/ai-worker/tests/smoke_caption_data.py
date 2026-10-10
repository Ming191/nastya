"""CI smoke: real LiveKit targeted caption data packets, zero model usage."""

import asyncio
import json
from datetime import timedelta

from livekit import api, rtc

from nastya_worker.caption_protocol import TOPIC, publish_caption

URL = "ws://127.0.0.1:7880"
KEY, SECRET = "devkey", "secret"
ROOM = "nastya_" + "c" * 32


def token(identity: str, language: str = "") -> str:
    builder = (
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
        builder = builder.with_attributes({"sourceLanguage": language})
    return builder.to_jwt()


async def join(identity: str, language: str = "") -> rtc.Room:
    room = rtc.Room()
    await room.connect(URL, token(identity, language))
    return room


async def main() -> None:
    worker = owner = guest = None
    received: dict[str, list[rtc.DataPacket]] = {"owner": [], "guest": []}
    try:
        worker = await join("interpreter")
        owner = rtc.Room()
        guest = rtc.Room()

        @owner.on("data_received")
        def on_owner(packet: rtc.DataPacket) -> None:
            received["owner"].append(packet)

        @guest.on("data_received")
        def on_guest(packet: rtc.DataPacket) -> None:
            received["guest"].append(packet)

        await owner.connect(URL, token("human:owner", "vi"))
        await guest.connect(URL, token("human:guest", "ru"))
        expected = {"human:owner", "human:guest"}
        for _ in range(60):
            if expected.issubset({p.identity for p in worker.remote_participants.values()}):
                break
            await asyncio.sleep(0.1)
        assert expected.issubset({p.identity for p in worker.remote_participants.values()})

        caption = {
            "version": 1,
            "type": "caption.upsert",
            "roomId": ROOM,
            "speakerId": "human:owner",
            "utteranceId": "human:owner:1:1",
            "revision": 2,
            "sequence": 1,
            "sourceLanguage": "vi",
            "targetLanguage": "ru",
            "sourceText": "Xin chào",
            "translatedText": "Привет",
            "isFinal": True,
            "startOffsetMs": 50,
            "endOffsetMs": 350,
        }
        assert await publish_caption(worker, caption) == 1
        for _ in range(50):
            if received["guest"]:
                break
            await asyncio.sleep(0.1)
        assert len(received["guest"]) == 1
        assert not received["owner"], "unexpected caption leak to source participant"
        packet = received["guest"][0]
        assert packet.topic == TOPIC
        assert packet.participant and packet.participant.identity == "interpreter"
        assert json.loads(packet.data)["translatedText"] == "Привет"
        print("PASS: real LiveKit recipient-scoped caption packet")
    finally:
        for room in (owner, guest, worker):
            if room is not None:
                await room.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
