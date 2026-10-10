import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

from nastya_worker.providers.tts import OptionalTts, SynthesizedSpeech
from nastya_worker.voice_transport import (
    CHUNK_BYTES, MAX_AUDIO_BYTES, VOICE_TOPIC, VoiceDelivery,
    cancel_voice, publish_voice,
)

ROOM = "nastya_" + "a" * 32


def caption(speaker="human:owner", target="ru", number=1):
    return {
        "version": 1, "type": "caption.upsert", "roomId": ROOM,
        "speakerId": speaker, "utteranceId": f"{speaker}:1:{number}",
        "revision": 1, "sequence": number,
        "sourceLanguage": "vi" if target == "ru" else "ru",
        "targetLanguage": target, "sourceText": "Synthetic", "translatedText": "Test",
        "isFinal": True, "startOffsetMs": 0, "endOffsetMs": 1000,
    }


class MockLocal:
    identity = "interpreter"

    def __init__(self):
        self.sent = []

    async def publish_data(self, payload, **kwargs):
        self.sent.append((json.loads(payload), kwargs))
        await asyncio.sleep(0)


def room():
    owner = SimpleNamespace(identity="human:owner", attributes={"sourceLanguage": "vi"})
    guest = SimpleNamespace(identity="human:guest", attributes={"sourceLanguage": "ru"})
    intruder = SimpleNamespace(identity="interpreter-dup", attributes={"sourceLanguage": "ru"})
    return SimpleNamespace(
        name=ROOM, local_participant=MockLocal(),
        remote_participants={"owner": owner, "guest": guest, "intruder": intruder},
    )


def speech(c, data=b"\xff\xfb\x80test"):
    return SynthesizedSpeech(
        c["utteranceId"], c["targetLanguage"], "ru-RU-SvetlanaNeural",
        "audio/mpeg", data, 20, 45,
    )


def run(task):
    return asyncio.run(task)


def test_targeted_voice_is_bounded_chunked_and_matches_caption_identifiers():
    async def scenario():
        target_room = room()
        cap = caption()
        data = bytes([i % 256 for i in range(CHUNK_BYTES * 2 + 17)])
        assert await publish_voice(target_room, cap, speech(cap, data)) == 1
        packets = target_room.local_participant.sent
        assert [x[0]["type"] for x in packets] == [
            "voice.begin", "voice.chunk", "voice.chunk", "voice.chunk", "voice.end",
        ]
        for value, meta in packets:
            assert meta == {
                "reliable": True, "destination_identities": ["human:guest"],
                "topic": VOICE_TOPIC,
            }
            assert value["utteranceId"] == cap["utteranceId"]
            assert value["sequence"] == cap["sequence"]
        assert packets[0][0]["byteLength"] == len(data)
        assert packets[0][0]["chunkCount"] == 3
        assert b"".join(base64.b64decode(value["data"]) for value, _ in packets[1:-1]) == data

        opposite = caption("human:guest", "vi")
        assert await publish_voice(target_room, opposite, speech(opposite)) == 1
        assert target_room.local_participant.sent[-1][1]["destination_identities"] == [
            "human:owner",
        ]
        assert await cancel_voice(target_room, opposite) == 1
        assert target_room.local_participant.sent[-1][0]["type"] == "voice.cancel"

    run(scenario())


@pytest.mark.parametrize("change", [
    lambda c: {**c, "isFinal": False},
    lambda c: {**c, "roomId": "nastya_" + "b" * 32},
    lambda c: {**c, "translatedText": ""},
])
def test_no_voice_for_untrusted_caption(change):
    async def scenario():
        target_room = room()
        cap = caption()
        with pytest.raises(ValueError):
            await publish_voice(target_room, change(cap), speech(cap))
        assert not target_room.local_participant.sent
    run(scenario())


def test_deny_bad_identity_wrong_language_and_oversized_audio():
    async def scenario():
        target_room = room()
        cap = caption()
        with pytest.raises(ValueError, match="bounded"):
            await publish_voice(target_room, cap, speech(cap, b"x" * (MAX_AUDIO_BYTES + 1)))
        with pytest.raises(ValueError, match="mismatch"):
            await publish_voice(target_room, cap, speech(caption(number=2)))
        target_room.local_participant.identity = "human:owner"
        with pytest.raises(ValueError, match="interpreter"):
            await publish_voice(target_room, cap, speech(cap))
        target_room.local_participant.identity = "interpreter"
        target_room.remote_participants["guest"].attributes["sourceLanguage"] = "vi"
        assert await publish_voice(target_room, cap, speech(cap)) == 0
        assert not target_room.local_participant.sent
    run(scenario())


def test_latest_speech_cancels_older_synthesis_and_never_replays():
    async def scenario():
        target_room = room()
        started = asyncio.Event()
        was_cancelled = asyncio.Event()

        class Synth:
            async def synthesize(self, text, lang, voice, uid):
                if uid.endswith(":1"):
                    started.set()
                    try:
                        await asyncio.sleep(60)
                    except asyncio.CancelledError:
                        was_cancelled.set()
                        raise
                return speech(caption(number=2))

        delivery = VoiceDelivery(target_room, OptionalTts(True, Synth()))
        first = caption()
        second = caption(number=2)
        await delivery.replace(first, "ru-RU-SvetlanaNeural")
        await started.wait()
        await delivery.replace(second, "ru-RU-SvetlanaNeural")
        await asyncio.sleep(.05)
        assert was_cancelled.is_set()
        messages = [p["type"] for p, _ in target_room.local_participant.sent]
        assert messages == ["voice.cancel", "voice.begin", "voice.chunk", "voice.end"]
        assert all(p["utteranceId"] != first["utteranceId"]
                   for p, _ in target_room.local_participant.sent if p["type"] != "voice.cancel")
        await delivery.stop()
        assert target_room.local_participant.sent[-1][0]["type"] == "voice.cancel"

    run(scenario())


def test_failed_tts_keeps_caption_only_without_voice_data():
    async def scenario():
        class Failing:
            async def synthesize(self, *args):
                raise RuntimeError("private message")
        target_room = room()
        delivery = VoiceDelivery(target_room, OptionalTts(True, Failing()))
        await delivery.replace(caption(), "ru-RU-SvetlanaNeural")
        await asyncio.sleep(.01)
        assert not target_room.local_participant.sent
        await delivery.stop()
    run(scenario())
