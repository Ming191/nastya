import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nastya_worker.caption_protocol import FIELDS, TOPIC, publish_caption, validate_caption

ROOM = "nastya_" + "a" * 32


@pytest.fixture()
def caption():
    return {
        "version": 1,
        "type": "caption.upsert",
        "roomId": ROOM,
        "speakerId": "human:owner",
        "utteranceId": "human:owner:4:1",
        "revision": 0,
        "sequence": 1,
        "sourceLanguage": "vi",
        "targetLanguage": "ru",
        "sourceText": "Xin chào",
        "translatedText": "",
        "isFinal": False,
        "startOffsetMs": 10,
        "endOffsetMs": 1000,
    }


def test_protocol_schema_matches_python_publisher(caption):
    path = Path(__file__).resolve().parents[3] / "protocol" / "caption.v1.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    assert set(schema["required"]) == FIELDS
    assert schema["additionalProperties"] is False
    assert schema["properties"]["sourceText"]["maxLength"] == 2000
    assert schema["properties"]["revision"]["maximum"] == 1_000_000
    assert validate_caption(caption) == caption


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", True),
        ("sequence", -1),
        ("version", 2),
        ("type", "other"),
        ("speakerId", "interpreter"),
        ("sourceLanguage", "ru"),
        ("roomId", "private-other"),
        ("utteranceId", "../anything"),
        ("sourceText", ""),
        ("sourceText", "a" * 2001),
        ("translatedText", "a" * 2001),
        ("startOffsetMs", -1),
        ("endOffsetMs", 60000),
    ],
)
def test_reject_invalid_caption_payload(caption, field, value):
    with pytest.raises(ValueError):
        validate_caption({**caption, field: value})


def test_final_cannot_be_empty_and_unknown_fields_forbidden(caption):
    with pytest.raises(ValueError):
        validate_caption({**caption, "isFinal": True})
    with pytest.raises(ValueError):
        validate_caption({**caption, "extra": "data"})
    with pytest.raises(ValueError):
        validate_caption({**caption, "endOffsetMs": 0})


def fake_room():
    class Publisher:
        identity = "interpreter"

        def __init__(self):
            self.calls = []

        async def publish_data(self, payload, **kwargs):
            self.calls.append((payload, kwargs))

    owner = SimpleNamespace(identity="human:owner", attributes={"sourceLanguage": "vi"})
    guest = SimpleNamespace(identity="human:guest", attributes={"sourceLanguage": "ru"})
    system = SimpleNamespace(identity="other-service", attributes={"sourceLanguage": "ru"})
    pub = Publisher()
    room = SimpleNamespace(
        name=ROOM,
        local_participant=pub,
        remote_participants={"o": owner, "g": guest, "ai": system},
    )
    return room, pub


def test_send_only_to_matching_other_human_and_reliable_final(caption):
    async def run():
        room, pub = fake_room()
        assert await publish_caption(room, caption) == 1
        payload, args = pub.calls[0]
        assert json.loads(payload)["translatedText"] == ""
        assert args == {
            "topic": TOPIC,
            "reliable": False,
            "destination_identities": ["human:guest"],
        }
        final = {**caption, "translatedText": "Привет", "isFinal": True, "revision": 1}
        assert await publish_caption(room, final) == 1
        assert pub.calls[1][1]["reliable"] is True

        room.remote_participants["g"].attributes["sourceLanguage"] = "vi"
        assert await publish_caption(room, final) == 0
        assert len(pub.calls) == 2

        room.local_participant.identity = "human:owner"
        with pytest.raises(ValueError, match="interpreter"):
            await publish_caption(room, final)
        room.local_participant.identity = "interpreter"
        room.name = "nastya_" + "b" * 32
        with pytest.raises(ValueError, match="room"):
            await publish_caption(room, final)

    asyncio.run(run())
