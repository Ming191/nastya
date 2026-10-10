"""Bilingual quality rubric/latency gates: no network or private speech in CI."""

import asyncio
import json
from pathlib import Path

import pytest

from nastya_worker.providers.tts import OptionalTts, SynthesizedSpeech
from nastya_worker.voice_qa import (
    SAMPLES_PATH,
    live_samples,
    load_events,
    prepare,
    review_summary,
    samples,
    summary,
    validate_event,
)


def event(**updates):
    data = {
        "sampleId": "synthetic_01",
        "direction": "vi-ru",
        "source": "synthetic",
        "outcome": "played",
        "queueAgeMs": 240,
        "speechEndUnixMs": 100000,
        "firstPlaybackUnixMs": 101250,
        "clockErrorBoundMs": 5,
    }
    data.update(updates)
    return data


def test_quality_pack_is_original_twenty_cases_evenly_balanced():
    assert SAMPLES_PATH.exists()
    fixture = samples()
    assert len(fixture) == 20
    assert {case["language"] for case in fixture} == {"ru", "vi"}
    assert sum(case["language"] == "ru" for case in fixture) == 10
    assert sum(case["language"] == "vi" for case in fixture) == 10
    assert len({case["category"] for case in fixture}) >= 8
    assert all(case["text"].strip() and len(case["text"]) <= 500 for case in fixture)


def test_prepare_creates_blank_reviewer_scorecard_with_no_fake_scores(tmp_path):
    output = prepare(tmp_path / "review")
    assert output == {"cases": 20, "ru": 10, "vi": 10, "reviewed": 0}
    values = json.loads(
        (tmp_path / "review" / "voice-review-blank.json").read_text(encoding="utf-8")
    )
    assert values["status"] == "NOT_REVIEWED"
    assert values["reviewer"] is None
    assert all(case["clarity"] is None and case["naturalness"] is None for case in values["cases"])
    assert not review_summary(values)["humanReviewComplete"]


def test_review_scores_require_real_human_entries_and_valid_ratings(tmp_path):
    prepare(tmp_path / "review")
    values = json.loads(
        (tmp_path / "review" / "voice-review-blank.json").read_text(encoding="utf-8")
    )
    values["reviewer"] = "RU-VI bilingual evaluator"
    values["reviewDate"] = "2026-10-10"
    for case in values["cases"]:
        case.update(clarity=4, naturalness=3, pronunciation=5)
    values["cases"][0]["issues"] = ["odd_stress", "robotic"]
    report = review_summary(values)
    assert report["humanReviewComplete"] is True
    assert report["verifiedBySoftware"] is False
    assert report["byLanguage"]["ru"]["meanClarity"] == 4
    assert report["byLanguage"]["vi"]["meanNaturalness"] == 3
    assert report["byLanguage"]["ru"]["issues"]["odd_stress"] == 1
    values["cases"][0]["clarity"] = 0
    with pytest.raises(ValueError, match="scores"):
        review_summary(values)
    values["cases"][0]["clarity"] = 5
    values["cases"][0]["issues"] = ["private name: do not store"]
    with pytest.raises(ValueError, match="issue"):
        review_summary(values)


def test_synthetic_session_cannot_pass_real_end_to_end_latency_gate():
    report = summary(
        [
            event(),
            event(
                sampleId="synthetic_02", outcome="timeout", firstPlaybackUnixMs=None, queueAgeMs=800
            ),
        ]
    )
    assert report["all"]["totalEvents"] == 2
    assert report["all"]["endOfSpeechToPlaybackP95Ms"] is None
    assert report["all"]["queueAgeP95Ms"] == 772
    assert report["all"]["errorRate"] == 0.5
    assert report["acceptanceReadyForLatencyReview"] is False


def test_live_timing_requires_bounded_clock_error_and_two_directions():
    data = []
    for direction in ("ru-vi", "vi-ru"):
        for index in range(20):
            data.append(
                event(
                    sampleId=f"{direction}_{index}",
                    direction=direction,
                    source="live",
                    firstPlaybackUnixMs=100200 + index * 25,
                    queueAgeMs=100 + index,
                )
            )
    report = summary(data)
    assert report["acceptanceReadyForLatencyReview"] is True
    assert report["all"]["validLiveLatencySamples"] == 40
    assert report["byDirection"]["ru-vi"]["endOfSpeechToPlaybackP50Ms"] == 437.5
    assert report["byDirection"]["vi-ru"]["endOfSpeechToPlaybackP95Ms"] == 651.25
    assert report["all"]["queueAgeP95Ms"] == 118.05
    data[-1]["clockErrorBoundMs"] = 90
    downgraded = summary(data)
    assert downgraded["acceptanceReadyForLatencyReview"] is False
    assert downgraded["all"]["validLiveLatencySamples"] == 39


def test_invalid_session_records_never_contain_text_or_invent_timestamps():
    for malformed in [
        event(text="actual private speech"),
        event(outcome="played", firstPlaybackUnixMs=None),
        event(outcome="caption_only", firstPlaybackUnixMs=101250),
        event(clockErrorBoundMs=-5),
        event(queueAgeMs=float("nan")),
        event(firstPlaybackUnixMs=99999),
        event(sampleId="../sensitive"),
        event(direction="en-vi"),
    ]:
        with pytest.raises(ValueError):
            validate_event(malformed)


def test_session_jsonl_size_and_input_validation(tmp_path):
    file = tmp_path / "trace.jsonl"
    file.write_text(
        json.dumps(event())
        + "\n"
        + json.dumps(event(sampleId="two", outcome="cancelled", firstPlaybackUnixMs=None))
        + "\n",
        encoding="utf-8",
    )
    assert len(load_events(file)) == 2
    file.write_text(json.dumps(event(content="forbidden")), encoding="utf-8")
    with pytest.raises(ValueError, match="trace fields"):
        load_events(file)


def test_opt_in_sampling_uses_fixed_pack_and_is_never_automatic(tmp_path, monkeypatch):
    monkeypatch.setenv("NASTYA_TTS_ENABLED", "false")
    with pytest.raises(ValueError, match="NASTYA_TTS_ENABLED"):
        asyncio.run(live_samples(tmp_path / "none", "ru"))
    assert not (tmp_path / "none").exists()

    class StubProvider:
        async def synthesize(self, text, language, voice, uid):
            assert text in {item["text"] for item in samples()}
            return SynthesizedSpeech(uid, language, voice, "audio/mpeg", b"\xff\xfbtest", 7, 15)

    class FakeSettings:
        enabled = True

        @staticmethod
        def from_env():
            return FakeSettings()

        def build(self):
            return OptionalTts(True, StubProvider())

    monkeypatch.setattr("nastya_worker.voice_qa.TtsSettings", FakeSettings)
    count = asyncio.run(live_samples(tmp_path / "sample", "both"))
    assert count == {"attempted": 20, "succeeded": 20}
    records = json.loads(
        (tmp_path / "sample" / "voice-synthesis-results.json").read_text(encoding="utf-8")
    )
    assert len(records["cases"]) == 20
    assert records["humanReview"] == "NOT_REVIEWED"
    assert all(case["firstAudioLatencyMs"] == 7 for case in records["cases"])
    assert all(
        (tmp_path / "sample" / case["file"]).read_bytes() == b"\xff\xfbtest"
        for case in records["cases"]
    )
    assert "Привет" not in json.dumps(records, ensure_ascii=False)


def test_live_sample_rejects_repository_relative_output():
    import nastya_worker.voice_qa as qa

    with pytest.raises(ValueError, match="outside the repository"):
        qa.prepare(Path(__file__).resolve().parents[1] / "evaluation" / "forbidden")
