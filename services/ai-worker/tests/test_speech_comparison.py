import pytest

from nastya_worker.speech_comparison import summarise_stt


def fixture(uid="vi-clean", scenario="speech", transcript="xin chao"):
    return {"id": uid, "language": "vi", "scenario": scenario, "transcript": transcript}


def result(uid="vi-clean", error=None, text="xin chao", response=0):
    return {"id": uid, "error": error, "text": text, "responseMs": response, "queueMs": 0}


def test_failed_stt_is_not_scored_as_an_empty_success():
    report = summarise_stt([result(error="timeout", text=None)], [fixture()])
    group = report["vi"]["all"]
    assert group["failed"] == 1
    assert group["scored"] == 0
    assert group["wer_whitespace_proxy"] is None
    assert group["response_ms"]["median"] is None


def test_silence_hallucination_does_not_contaminate_speech_wer():
    report = summarise_stt(
        [result(), result("vi-silent", text="hallucination")],
        [fixture(), fixture("vi-silent", "silence", "")],
    )
    assert report["vi"]["all"]["wer_whitespace_proxy"] == 0
    assert report["vi"]["all"]["silence_hallucinations"] == 1
    assert report["vi"]["silence"]["wer_whitespace_proxy"] is None
    assert report["vi"]["speech"]["response_ms"]["median"] == 0
    assert report["ru"]["all"]["wer_whitespace_proxy"] is None


def test_missing_or_duplicate_comparison_rows_are_rejected():
    with pytest.raises(ValueError, match="incomplete or duplicated"):
        summarise_stt([], [fixture()])
    with pytest.raises(ValueError, match="incomplete or duplicated"):
        summarise_stt([result(), result()], [fixture(), fixture("vi-other")])


def test_each_scenario_keeps_its_own_denominator_and_latency_samples():
    report = summarise_stt(
        [result(response=10), result("vi-noise", text="xin", response=100)],
        [fixture(), fixture("vi-noise", "noise")],
    )
    assert report["vi"]["speech"]["wer_whitespace_proxy"] == 0
    assert report["vi"]["noise"]["wer_whitespace_proxy"] == 0.5
    assert report["vi"]["all"]["response_ms"]["mean"] == 55
    assert report["vi"]["all"]["response_ms"]["n"] == 2
