import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from nastya_worker.analyse_captions import analyse, canonical_numbers, summary
from nastya_worker.benchmark_room import FixtureRegistry
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


def test_fixture_boundary_refuses_overlapping_or_undrained_audio():
    registry = FixtureRegistry()
    pipeline = SimpleNamespace(running=True, segmenter=SimpleNamespace(frames=[]))
    registry.pipelines["human:owner"] = pipeline
    begin = {"speaker": "human:owner", "action": "begin", "fixtureId": "vi-0"}
    registry.command(begin)
    with pytest.raises(ValueError, match="already active"):
        registry.command({**begin, "fixtureId": "vi-1"})
    pipeline.segmenter.frames = [b"pcm"]
    with pytest.raises(ValueError, match="boundary_busy"):
        registry.command({**begin, "action": "end"})
    pipeline.segmenter.frames = []
    registry.command({**begin, "action": "end"})
    assert not registry.active


def test_old_timestamp_matched_measurements_are_rejected(tmp_path):
    (tmp_path / "browser-results.json").write_text(
        json.dumps({"samples": [{"id": "vi-0"}], "errors": []})
    )
    with pytest.raises(ValueError, match="provenance v2"):
        analyse(tmp_path, expected=1)


def test_missing_provenance_is_not_invented_as_wer_or_latency(tmp_path):
    sample = {
        "id": "vi-0",
        "direction": "vi-ru",
        "provenanceValid": False,
        "segmentIds": [],
        "finalLanguage": None,
        "finalUnixMs": None,
        "speechEndToFinalMs": 123,
        "sourceReference": "synthetic",
        "recognizedText": None,
        "translatedText": None,
        "reference": "synthetic",
    }
    (tmp_path / "browser-results.json").write_text(
        json.dumps({"samples": [sample], "errors": [], "profile": "test", "correlationVersion": 2})
    )
    (tmp_path / "worker-events.jsonl").write_text("")
    result = analyse(tmp_path, expected=1)
    assert result["speech_end_to_any_final_ms"]["n"] == 0
    assert result["directions"]["vi-ru"]["wer"] is None
    assert result["fixtures_without_stt_provenance"] == 1


def test_wer_uses_worker_output_even_when_dom_partial_is_missing(tmp_path):
    uid = "human:owner:1:1"
    sample = {
        "id": "vi-0",
        "direction": "vi-ru",
        "provenanceValid": True,
        "segmentIds": [uid],
        "finalLanguage": "ru",
        "finalUnixMs": 1400,
        "speechEndUnixMs": 1000,
        "speechEndToFinalMs": 400,
        "sourceReference": "xin chao",
        "recognizedText": "wrong DOM partial",
        "translatedText": "synthetic",
        "reference": "synthetic",
    }
    (tmp_path / "browser-results.json").write_text(
        json.dumps({"samples": [sample], "errors": [], "profile": "test", "correlationVersion": 2})
    )
    event = {
        "sample": uid,
        "fixtureId": "vi-0",
        "stage": "enqueued",
        "unixMs": 1100,
        "durationMs": 0,
        "direction": "vi",
    }
    (tmp_path / "worker-events.jsonl").write_text(json.dumps(event) + "\n")
    (tmp_path / "synthetic-transcripts.jsonl").write_text(
        json.dumps({"sample": uid, "fixtureId": "vi-0", "language": "vi", "text": "xin chao"})
        + "\n"
    )
    result = analyse(tmp_path, expected=1)
    assert result["directions"]["vi-ru"]["wer"] == 0
    assert result["directions"]["vi-ru"]["stt_scored_samples"] == 1


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
