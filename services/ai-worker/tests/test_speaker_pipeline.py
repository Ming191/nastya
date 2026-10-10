import asyncio
import time
from array import array
from types import SimpleNamespace

import pytest

from nastya_worker.livekit_ingest import RoomAudioReceiver, room_settings, worker_token
from nastya_worker.providers.types import Transcript
from nastya_worker.speaker_pipeline import (
    HUMANS,
    Segment,
    Segmenter,
    SpeakerPipeline,
    VadOptions,
    is_human_microphone,
    pcm_rms,
    source_language,
)

FRAME_SAMPLES = 320
LOUD = array("h", [11000] * FRAME_SAMPLES).tobytes()
QUIET = bytes(FRAME_SAMPLES * 2)


def frames(segmenter, frames, pcm):
    return [result for _ in range(frames) if (result := segmenter.feed(pcm)) is not None]


def test_audio_policy_excludes_ai_video_screen_share_and_other_roles():
    assert HUMANS == {"human:owner", "human:guest"}
    assert is_human_microphone("human:guest", "audio", "microphone")
    for identity, kind, src in [
        ("interpreter", "audio", "microphone"),
        ("human:owner", "video", "camera"),
        ("human:guest", "audio", "screen_share_audio"),
        ("human:other", "audio", "microphone"),
        ("tts:ru", "audio", "microphone"),
    ]:
        assert not is_human_microphone(identity, kind, src)
    assert source_language({"sourceLanguage": "ru"}) == "ru"
    assert source_language({"sourceLanguage": "vi"}) == "vi"
    assert source_language({"sourceLanguage": "xx"}) is None
    assert source_language({}) is None


def test_pcm_energy_and_vad_configuration_validation():
    assert pcm_rms(QUIET) == 0
    assert pcm_rms(LOUD) > 0.3
    with pytest.raises(ValueError, match="aligned"):
        pcm_rms(b"x")
    with pytest.raises(ValueError, match="threshold"):
        VadOptions(threshold=0.0)


def test_vad_separates_consecutive_utterances_and_keeps_offsets():
    vad = Segmenter("human:owner", "vi", generation=9)
    first = frames(vad, 14, LOUD) + frames(vad, 22, QUIET)
    second = frames(vad, 14, LOUD) + frames(vad, 22, QUIET)
    assert len(first) == len(second) == 1
    a, b = first[0], second[0]
    assert a.start_ms >= 0
    assert a.end_ms < b.start_ms
    assert a.language == "vi"
    assert a.speaker == b.speaker == "human:owner"
    assert a.utterance_id != b.utterance_id
    assert ":9:" in a.utterance_id
    assert a.wav().startswith(b"RIFF")
    assert a.end_ms - a.start_ms <= 1200


def test_silence_and_short_noise_not_enqueued():
    vad = Segmenter("human:guest", "ru", generation=2)
    assert not frames(vad, 60, QUIET)
    assert not frames(vad, 3, LOUD)
    assert not frames(vad, 23, QUIET)
    assert not frames(vad, 30, QUIET)


def test_vad_forces_end_of_long_phrase_with_bounded_memory():
    vad = Segmenter("human:guest", "ru", generation=1, vad=VadOptions(max_segment_ms=1000))
    emitted = frames(vad, 70, LOUD)
    assert len(emitted) >= 1
    assert emitted[0].end_ms <= 1200
    assert len(emitted[0].pcm) <= 1000 * 32 + 640
    vad.reset()
    assert vad.flush() is None


def test_independent_simultaneous_speakers_and_stt_failure_isolation():
    async def scenario():
        results = []

        class Recognizer:
            async def transcribe(self, wav, language):
                assert wav.startswith(b"RIFF")
                await asyncio.sleep(0.01)
                if language == "ru":
                    raise RuntimeError("remote inference failed")
                return Transcript(text="private-should-not-log", language=language)

        async def store(segment, transcript):
            results.append((segment.speaker, segment.language, segment.utterance_id))

        sem = asyncio.Semaphore(1)
        owner = SpeakerPipeline("human:owner", "vi", 1, Recognizer(), sem, store)
        guest = SpeakerPipeline("human:guest", "ru", 1, Recognizer(), sem, store)
        owner.start()
        guest.start()
        for _ in range(14):
            owner.feed(LOUD)
            guest.feed(LOUD)
        for _ in range(22):
            owner.feed(QUIET)
            guest.feed(QUIET)
        await asyncio.sleep(0.2)
        assert owner.processed == 1
        assert guest.failures == 1
        assert len(results) == 1 and results[0][0] == "human:owner"
        await asyncio.gather(owner.stop(), guest.stop())

    asyncio.run(scenario())


def test_queue_backpressure_staleness_and_epoch_invalidation():
    async def scenario():
        seen = []

        class Recognizer:
            async def transcribe(self, wav, language):
                await asyncio.sleep(0.01)
                return Transcript(text="ok", language=language)

        async def store(segment, transcript):
            seen.append(segment.utterance_id)

        pipe = SpeakerPipeline(
            "human:owner", "vi", 3, Recognizer(), asyncio.Semaphore(1), store, queue_limit=1
        )
        pipe.start()
        for serial in range(3):
            pipe.enqueue(
                Segment("human:owner", "vi", f"n:{serial}", 0, 200, LOUD, 3, time.monotonic())
            )
        assert pipe.dropped == 2
        pipe.enqueue(Segment("human:owner", "vi", "old-epoch", 0, 200, LOUD, 2, time.monotonic()))
        assert pipe.dropped == 3
        await asyncio.sleep(0.04)
        assert seen == ["n:2"]
        await pipe.stop()
        pipe.enqueue(Segment("human:owner", "vi", "after-stop", 0, 100, LOUD, 3, time.monotonic()))
        assert seen == ["n:2"]
        stale = SpeakerPipeline(
            "human:guest", "ru", 5, Recognizer(), asyncio.Semaphore(1), store, expiry_seconds=1
        )
        stale.enqueue(
            Segment("human:guest", "ru", "expired", 0, 100, LOUD, 5, time.monotonic() - 2)
        )
        stale.start()
        await asyncio.sleep(0.02)
        assert stale.dropped == 1
        await stale.stop()

    asyncio.run(scenario())


def test_room_keys_validation_never_accepts_arbitrary_target():
    room_id = "nastya_" + "a" * 32
    values = {
        "LIVEKIT_URL": "wss://example.livekit.cloud",
        "LIVEKIT_API_KEY": "public-key",
        "LIVEKIT_API_SECRET": "server-secret",
    }
    assert room_settings(room_id, values)["LIVEKIT_URL"] == values["LIVEKIT_URL"]
    with pytest.raises(ValueError, match="room ID"):
        room_settings("other-room", values)
    with pytest.raises(ValueError, match="WSS"):
        room_settings(room_id, {**values, "LIVEKIT_URL": "ws://remote.example"})
    with pytest.raises(ValueError, match="required"):
        room_settings(room_id, {"LIVEKIT_URL": "wss://example.livekit.cloud"})


def test_worker_token_subscribes_but_can_never_publish():
    from livekit import api

    claims = api.TokenVerifier("public-key", "secret-key").verify(
        worker_token(
            "nastya_" + "a" * 32,
            {
                "LIVEKIT_URL": "ws://localhost:7880",
                "LIVEKIT_API_KEY": "public-key",
                "LIVEKIT_API_SECRET": "secret-key",
            },
        )
    )
    assert claims.identity == "interpreter"
    assert claims.video.room == "nastya_" + "a" * 32
    assert claims.video.can_subscribe is True
    assert claims.video.can_publish is False
    assert claims.video.can_publish_data is False
    assert not claims.video.room_admin


def test_receiver_selective_subscription_and_generation():
    class Pub:
        def __init__(self, source, kind):
            self.source, self.kind = source, kind
            self.subscribed = False
            self.sid = source

        def set_subscribed(self, value):
            self.subscribed = value

    class Rtc:
        class TrackKind:
            KIND_AUDIO = 1
            KIND_VIDEO = 2

        class TrackSource:
            SOURCE_MICROPHONE = 1
            SOURCE_SCREEN_SHARE_AUDIO = 2

    class Room:
        def __init__(self):
            self.events = {}
            self.remote_participants = {}

        def on(self, event):
            def bind(fn):
                self.events[event] = fn
                return fn

            return bind

    room = Room()
    r = RoomAudioReceiver(room, Rtc(), SimpleNamespace())
    r.register()
    allowed = Pub(Rtc.TrackSource.SOURCE_MICROPHONE, Rtc.TrackKind.KIND_AUDIO)
    ignored = Pub(Rtc.TrackSource.SOURCE_SCREEN_SHARE_AUDIO, Rtc.TrackKind.KIND_AUDIO)
    guest = SimpleNamespace(
        identity="human:guest",
        attributes={"sourceLanguage": "ru"},
        track_publications={"a": allowed, "b": ignored},
    )
    robot = SimpleNamespace(
        identity="interpreter",
        attributes={"sourceLanguage": "ru"},
        track_publications={"a": allowed},
    )
    room.events["track_published"](ignored, guest)
    assert not ignored.subscribed
    room.events["track_published"](allowed, robot)
    assert not allowed.subscribed
    room.events["track_published"](allowed, guest)
    assert allowed.subscribed
    r._detach("human:guest")
    assert r.epochs["human:guest"] == 0  # Nothing to detach yet.
    assert "participant_attributes_changed" in room.events
    room.events["disconnected"]()
    assert r.stop_event.is_set()
