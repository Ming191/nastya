"""Strict caption v1 payload validation and target-scoped LiveKit packet delivery.

Intended for NAS-19 orchestration. Does not log or persist transcript content.
"""
import json
import re
from typing import Any

TOPIC = "nastya.caption.v1"
MAX_BYTES = 12_000
ROOM_RE = re.compile(r"nastya_[a-f0-9]{32}\Z")
UTTERANCE_RE = re.compile(r"[A-Za-z0-9:_-]{1,96}\Z")
HUMANS = frozenset({"human:owner", "human:guest"})
FIELDS = frozenset({
    "version", "type", "roomId", "speakerId", "utteranceId",
    "revision", "sequence", "sourceLanguage", "targetLanguage",
    "sourceText", "translatedText", "isFinal", "startOffsetMs", "endOffsetMs",
})


def _int(value: Any, limit: int) -> bool:
    return type(value) is int and 0 <= value <= limit


def validate_caption(event: Any) -> dict:
    if not isinstance(event, dict) or set(event) != FIELDS:
        raise ValueError("invalid caption fields")
    if event["version"] != 1 or event["type"] != "caption.upsert":
        raise ValueError("invalid caption version")
    if not isinstance(event["roomId"], str) or not ROOM_RE.fullmatch(event["roomId"]):
        raise ValueError("invalid caption room")
    if event["speakerId"] not in HUMANS:
        raise ValueError("invalid caption speaker")
    if not isinstance(event["utteranceId"], str) or not UTTERANCE_RE.fullmatch(
        event["utteranceId"]
    ):
        raise ValueError("invalid utterance ID")
    if not _int(event["revision"], 1_000_000) or not _int(event["sequence"], 1_000_000_000):
        raise ValueError("invalid revision/sequence")
    src, dst = event["sourceLanguage"], event["targetLanguage"]
    if src not in ("ru", "vi") or dst not in ("ru", "vi") or src == dst:
        raise ValueError("invalid translation direction")
    if (not isinstance(event["sourceText"], str) or
            not 1 <= len(event["sourceText"]) <= 2000):
        raise ValueError("invalid source text")
    if (not isinstance(event["translatedText"], str) or
            len(event["translatedText"]) > 2000):
        raise ValueError("invalid translation text")
    if type(event["isFinal"]) is not bool or (
        event["isFinal"] and not event["translatedText"]
    ):
        raise ValueError("invalid caption final flag")
    start, end = event["startOffsetMs"], event["endOffsetMs"]
    if (not _int(start, 86_400_000) or not _int(end, 86_400_000) or
            end < start or end - start > 30_000):
        raise ValueError("invalid time range")
    size = len(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size > MAX_BYTES:
        raise ValueError("caption packet too large")
    return event


def recipient_identities(room: Any, event: dict) -> list[str]:
    validate_caption(event)
    return sorted(
        p.identity for p in room.remote_participants.values()
        if p.identity in HUMANS and p.identity != event["speakerId"]
        and p.attributes.get("sourceLanguage") == event["targetLanguage"]
    )


async def publish_caption(room: Any, event: dict) -> int:
    validate_caption(event)
    if room.name != event["roomId"]:
        raise ValueError("caption room mismatch")
    if room.local_participant.identity != "interpreter":
        raise ValueError("caption sender must be interpreter")
    recipients = recipient_identities(room, event)
    if not recipients:
        return 0
    await room.local_participant.publish_data(
        json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        topic=TOPIC, reliable=event["isFinal"], destination_identities=recipients,
    )
    return len(recipients)
