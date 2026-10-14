import asyncio
import json
import time

import pytest

from nastya_worker.analyse_captions import analyse, canonical_numbers, summary
from nastya_worker.providers.types import Transcript
from nastya_worker.speaker_pipeline import Segment, SpeakerPipeline


def test_missing_metrics_stay_unavailable_and_zero_stays_measured():
    assert summary([])["median"] is None
    assert summary([None])["n"] == 0
    assert summary([0])["median"] == 0
    assert summary([0])["n"] == 1
    assert summary([1, 3])["median"] == 2
    assert summary([1, 3])["mean"] == 2


def test_number_canonicalization_is_separate_from_raw_wer():
    assert canonical_numbers("lúc chín giờ") == canonical_numbers("lúc 9 giờ")
    assert canonical_numbers("ещё два часа") == canonical_numbers("еще 2 часа")


def test_incomplete_benchmark_is_not_reported_as_complete(tmp_path):
    (tmp_path / "browser-results.json").write_text(json.dumps({"samples": [], "errors": []}))
    with pytest.raises(ValueError, match="incomplete"):
        analyse(tmp_path, expected=20)


def test_stale_inflight_recognition_is_cancelled_and_fresh_audio_continues():
    async def go():
        cancelled = []
        delivered = []

        class Recognizer:
            calls = 0

            async def transcribe(self, wav, language):
                self.calls += 1
                if self.calls == 1:
                    try:
                        await asyncio.sleep(1)
                    except asyncio.CancelledError:
                        cancelled.append(True)
                        raise
                return Transcript("synthetic", language)

        async def callback(segment, transcript):
            delivered.append(segment.utterance_id)

        pipeline = SpeakerPipeline(
            "human:owner", "vi", 1, Recognizer(), asyncio.Semaphore(1), callback, expiry_seconds=0.5
        )
        pipeline.start()
        pipeline.enqueue(
            Segment("human:owner", "vi", "stale", 0, 100, b"\0" * 3200, 1, time.monotonic() - 0.46)
        )
        await asyncio.sleep(0.08)
        assert cancelled == [True]
        assert pipeline.dropped == 1
        assert pipeline.failures == 0
        pipeline.enqueue(
            Segment("human:owner", "vi", "fresh", 0, 100, b"\0" * 3200, 1, time.monotonic())
        )
        await asyncio.sleep(0.03)
        assert delivered == ["fresh"]
        await pipeline.stop()

    asyncio.run(go())
