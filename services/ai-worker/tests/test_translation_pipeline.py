"""Offline NAS-19 acceptance simulations; no real voice/model quality claims."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from nastya_worker.providers.tts import OptionalTts, SynthesizedSpeech
from nastya_worker.providers.types import Transcript, Translation
from nastya_worker.speaker_pipeline import Segment
from nastya_worker.translation_pipeline import (
    STATUS_TOPIC,
    TranslationPipeline,
    publish_status,
)
from nastya_worker.voice_transport import VOICE_TOPIC

ROOM = "nastya_" + "a" * 32


class Publisher:
    identity = "interpreter"

    def __init__(self):
        self.packets = []
        self.fail_final = 0
        self.fail_partial = False

    async def publish_data(self, payload, **kwargs):
        data = json.loads(payload)
        if data["type"] == "caption.upsert":
            if not data["isFinal"] and self.fail_partial:
                raise ConnectionError("partial lost")
            if data["isFinal"] and self.fail_final > 0:
                self.fail_final -= 1
                raise ConnectionError("transient network")
        self.packets.append((data, kwargs))


def room():
    owner = SimpleNamespace(identity="human:owner", attributes={"sourceLanguage": "vi"})
    guest = SimpleNamespace(identity="human:guest", attributes={"sourceLanguage": "ru"})
    return SimpleNamespace(
        name=ROOM,
        local_participant=Publisher(),
        remote_participants={"owner": owner, "guest": guest},
    )


def segment(speaker="human:owner", language="vi", serial=1, generation=1):
    return Segment(
        speaker, language, f"{speaker}:{generation}:{serial}",
        100, 850, b"\0" * 6400, generation, time.monotonic()
    )


def captions(target_room):
    return [
        (data, kwargs)
        for data, kwargs in target_room.local_participant.packets
        if data["type"] == "caption.upsert"
    ]


def statuses(target_room):
    return [
        (data, kwargs)
        for data, kwargs in target_room.local_participant.packets
        if data["type"] == "interpreter.status"
    ]


def run(coro):
    asyncio.run(coro)


class Translator:
    async def translate(self, text, source_language, target_language):
        return Translation(
            "Привет" if source_language == "vi" else "Xin chào",
            source_language, target_language
        )


def test_both_directions_publish_revision_zero_then_final_to_other_human_only():
    async def scenario():
        target_room = room()
        observations = []
        service = TranslationPipeline(
            target_room, Translator(), is_current=lambda _: True, observe=observations.append
        )
        await asyncio.gather(
            service.process(segment(), Transcript("Xin chào", "vi")),
            service.process(segment("human:guest", "ru"), Transcript("Привет", "ru")),
        )
        packets = captions(target_room)
        assert len(packets) == 4
        for who, dst in [("human:owner", "human:guest"), ("human:guest", "human:owner")]:
            revisions = [(data, meta) for data, meta in packets if data["speakerId"] == who]
            assert sorted(data["revision"] for data, _ in revisions) == [0, 1]
            partial = next((data, meta) for data, meta in revisions if not data["isFinal"])
            final = next((data, meta) for data, meta in revisions if data["isFinal"])
            assert partial[0]["translationState"] == "pending"
            assert partial[0]["translatedText"] == ""
            assert partial[1]["destination_identities"] == [dst]
            assert partial[1]["reliable"] is False
            assert final[0]["translationState"] == "translated"
            assert final[0]["sequence"] == partial[0]["sequence"] == 1
            assert final[0]["utteranceId"] == partial[0]["utteranceId"]
            assert final[1]["destination_identities"] == [dst]
            assert final[1]["reliable"] is True
        assert service.delivered == 2
        assert service.fallbacks == 0
        assert all(o.speech_end_unix_ms is None and o.clock_error_bound_ms is None
                   for o in observations)
        await service.shutdown()

    run(scenario())


@pytest.mark.parametrize("failure", ["oom", "timeout", "connection", "wrong_direction", "empty"])
def test_model_failure_falls_back_to_explicit_source_only_without_cutting_call(failure):
    async def scenario():
        class Broken:
            async def translate(self, text, src, dst):
                if failure == "oom":
                    raise RuntimeError("GPU OOM: private request content")
                if failure == "timeout":
                    await asyncio.sleep(60)
                if failure == "connection":
                    raise ConnectionError("upstream lost")
                if failure == "wrong_direction":
                    return Translation("bad", dst, src)
                return Translation(" ", src, dst)

        target_room = room()
        service = TranslationPipeline(
            target_room, Broken(), is_current=lambda _: True, timeout_seconds=0.01
        )
        await service.process(segment(), Transcript("Xin chào", "vi"))
        parts = captions(target_room)
        assert len(parts) == 2
        assert parts[0][0]["translationState"] == "pending"
        assert parts[1][0]["translationState"] == "source_only"
        assert parts[1][0]["translatedText"] == "Xin chào"
        assert parts[1][0]["isFinal"]
        assert statuses(target_room)[-1][0]["code"] == "translation_unavailable"
        assert service.fallbacks == 1
        assert service.delivered == 1
        await service.shutdown()

    run(scenario())


def test_mismatched_source_language_stt_error_and_empty_segments_never_fabricate_caption():
    async def scenario():
        target_room = room()
        service = TranslationPipeline(target_room, Translator(), is_current=lambda _: True)
        await service.process(segment(), Transcript("Привет", "ru"))
        assert not captions(target_room)
        assert statuses(target_room)[-1][0]["code"] == "language_mismatch"
        await service.process(segment(serial=2), Transcript("  ", "vi"))
        assert not captions(target_room)
        await service.stt_error(segment(serial=3))
        assert statuses(target_room)[-1][0]["code"] == "stt_unavailable"
        assert not captions(target_room)
        await service.shutdown()

    run(scenario())


def test_stale_network_epoch_must_not_publish_final_even_after_mt_returns():
    async def scenario():
        target_room = room()
        started = asyncio.Event()
        released = asyncio.Event()
        current = True

        class Slow:
            async def translate(self, text, src, dst):
                started.set()
                await released.wait()
                return Translation("Поздно", src, dst)

        service = TranslationPipeline(
            target_room, Slow(), is_current=lambda _: current
        )
        task = asyncio.create_task(service.process(segment(), Transcript("Xin chào", "vi")))
        await started.wait()
        current = False
        released.set()
        await task
        assert len(captions(target_room)) == 1
        assert not captions(target_room)[0][0]["isFinal"]
        await service.shutdown()

    run(scenario())


def test_lost_partial_and_retry_of_reliable_final_do_not_block_delivery():
    async def scenario():
        target_room = room()
        target_room.local_participant.fail_partial = True
        target_room.local_participant.fail_final = 1
        service = TranslationPipeline(target_room, Translator(), is_current=lambda _: True)
        await service.process(segment(), Transcript("Xin chào", "vi"))
        packets = captions(target_room)
        assert len(packets) == 1 and packets[0][0]["isFinal"]
        assert service.delivered == 1
        await service.shutdown()

    run(scenario())


def test_status_has_strict_recipient_routing_and_no_transcript_or_token():
    async def scenario():
        target_room = room()
        assert await publish_status(
            target_room, "human:owner", "vi", "stt_unavailable"
        ) == 1
        packet, metadata = statuses(target_room)[0]
        assert metadata["topic"] == STATUS_TOPIC
        assert metadata["destination_identities"] == ["human:guest"]
        assert "sourceText" not in packet
        with pytest.raises(ValueError):
            await publish_status(target_room, "interpreter", "vi", "ready")
        with pytest.raises(ValueError):
            await publish_status(target_room, "human:owner", "vi", "private-secret")
        target_room.remote_participants["guest"].attributes["sourceLanguage"] = "vi"
        assert await publish_status(target_room, "human:owner", "vi", "ready") == 0
        assert len(statuses(target_room)) == 1

    run(scenario())


def test_optional_tts_only_receives_successfully_delivered_translated_final():
    async def scenario():
        class Synth:
            async def synthesize(self, text, language, voice, uid):
                return SynthesizedSpeech(
                    uid, language, voice, "audio/mpeg", b"\xff\xfb\x01", 3, 6
                )

        target_room = room()
        service = TranslationPipeline(
            target_room, Translator(), is_current=lambda _: True,
            tts=OptionalTts(True, Synth())
        )
        await service.process(segment(), Transcript("Xin chào", "vi"))
        await asyncio.sleep(0.02)
        assert any(k["topic"] == VOICE_TOPIC
                   for _, k in target_room.local_participant.packets)
        await service.shutdown()

    run(scenario())
