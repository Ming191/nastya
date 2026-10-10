import asyncio
import json
import wave
from pathlib import Path

import httpx
import pytest

from nastya_worker.audio_manifest import (
    boundary_error_ms,
    load_audio_manifest,
    validate_vad_response,
)
from nastya_worker.benchmark_metrics import edit_distance, error_counts, normalise
from nastya_worker.benchmark_remote import parser, run
from nastya_worker.evaluation import EvaluationError, load_corpus
from nastya_worker.remote_benchmark import benchmark_mt, benchmark_stt, benchmark_vad, model_key


def create_audio(tmp_path: Path, language="vi", scenario="speech") -> Path:
    wav = tmp_path / "sample.wav"
    with wave.open(str(wav), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(bytes(16000))
    payload = {
        "schema_version": 1,
        "samples": [
            {
                "id": "synthetic_001",
                "language": language,
                "audio_path": "sample.wav",
                "transcript": "xin chào" if scenario == "speech" else "",
                "speech_segments_ms": [[100, 350]] if scenario == "speech" else [],
                "scenario": scenario,
                "provenance": "synthetic",
                "rights_basis": "locally generated PCM silence/noise for test only",
            }
        ],
    }
    path = tmp_path / "fixtures.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_audio_manifest_checks_rights_wav_format_and_annotation(tmp_path):
    path = create_audio(tmp_path)
    fixtures = load_audio_manifest(path)
    assert len(fixtures) == 1
    assert fixtures[0]["duration_ms"] == 500
    obj = json.loads(path.read_text(encoding="utf-8"))
    obj["samples"][0]["rights_basis"] = ""
    path.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(EvaluationError, match="rights_basis"):
        load_audio_manifest(path)
    obj["samples"][0]["rights_basis"] = "explicit operator attestation"
    obj["samples"][0]["audio_path"] = "../unauthorized.wav"
    path.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(EvaluationError, match="unsafe WAV path"):
        load_audio_manifest(path)


def test_audio_manifest_does_not_infer_speech_from_non_speech(tmp_path):
    path = create_audio(tmp_path, scenario="silence")
    sample = load_audio_manifest(path)[0]
    assert sample["speech_segments_ms"] == []
    assert sample["scenario"] == "silence"


def test_vad_boundaries_and_false_positives_are_explicit():
    assert boundary_error_ms([[100, 300]], [[110, 290]]) == {
        "missed_segments": 0,
        "false_positive_segments": 0,
        "mean_boundary_error_ms": 10.0,
    }
    assert boundary_error_ms([], [[0, 200]])["false_positive_segments"] == 1
    assert boundary_error_ms([[200, 400]], [])["missed_segments"] == 1
    with pytest.raises(EvaluationError, match="out-of-range"):
        validate_vad_response({"segments": [{"start_ms": 100, "end_ms": 800}]}, 500)


def test_unicode_normalization_and_token_error_counts():
    assert normalise("Xin, CHÀO!") == "xin chào"
    assert edit_distance(["a", "b"], ["a", "x", "b"]) == 1
    zero = error_counts("xin chào", "Xin, chào!")
    assert zero["word_errors"] == 0
    assert zero["character_errors"] == 0
    assert error_counts("không", "có")["word_errors"] == 1


def test_two_model_translation_comparison_uses_all_120_sources_with_bounded_concurrency():
    _, rows = load_corpus()
    requests = []

    def respond(req: httpx.Request):
        payload = json.loads(req.content)
        requests.append(payload)
        assert payload["model"] in ("nllb-small-test", "nllb-large-test")
        assert payload["source_language"] in ("ru", "vi")
        assert payload["source_language"] != payload["target_language"]
        return httpx.Response(200, json={"translated_text": "synthetic-mock-not-translation"})

    report, preds = asyncio.run(
        benchmark_mt(
            rows,
            ["nllb-small-test", "nllb-large-test"],
            "https://models.test/v1/translate",
            "",
            2,
            concurrency=3,
            rounds=1,
            transport=httpx.MockTransport(respond),
        )
    )
    assert len(requests) == 240
    assert len(preds["nllb-small-test"]) == len(preds["nllb-large-test"]) == 120
    assert set(report["results"]) == {"nllb-small-test", "nllb-large-test"}
    for item in report["results"].values():
        assert item["requests"] == item["succeeded"] == 120
        assert item["failed"] == 0
        assert item["concurrency"] == 3
        assert item["peak_gpu_vram_mib"] is None
        assert item["model_identity_attested"] is False
        assert item["first_partial_latency_ms"] is None
        assert item["automatic_translation"]["coverage"] == 1
        assert item["human_quality"] == "not_evaluated"


def test_stt_audio_batch_and_vad_mock_are_independent(tmp_path):
    fixture = load_audio_manifest(create_audio(tmp_path))

    def respond(req: httpx.Request):
        assert req.headers.get("authorization") == "Bearer test-secret"
        assert b"speech.wav" in req.content
        if req.url.path == "/v1/audio/transcriptions":
            return httpx.Response(200, json={"text": "xin chào"})
        if req.url.path == "/v1/vad":
            return httpx.Response(
                200,
                json={
                    "segments": [{"start_ms": 100, "end_ms": 350}],
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(respond)
    stt = asyncio.run(
        benchmark_stt(
            fixture,
            ["whisper-tiny-test", "whisper-large-test"],
            "https://models.test/v1/audio/transcriptions",
            "test-secret",
            2,
            concurrency=2,
            transport=transport,
        )
    )
    assert stt["fixture_count"] == 1
    for result in stt["results"].values():
        assert result["error_rates_by_language"]["vi"]["cer"] == 0
        assert result["error_rates_by_language"]["vi"]["wer_whitespace_proxy"] == 0
        assert result["error_rates_by_language"]["ru"]["successful"] == 0
        assert result["segmentation"] == "not_evaluated_without_vad_endpoint"
        assert result["first_partial_latency_ms"] is None
    vad = asyncio.run(
        benchmark_vad(
            fixture,
            "https://models.test/v1/vad",
            "test-secret",
            2,
            "vad-test",
            transport=transport,
        )
    )
    assert vad["status"] == "measured_api"
    assert vad["missed_segments"] == 0
    assert vad["false_positive_segments"] == 0
    assert vad["mean_boundary_error_ms"] == 0


def test_backend_failure_does_not_leak_remote_message_or_fake_throughput(tmp_path):
    fixtures = load_audio_manifest(create_audio(tmp_path))

    def fail(req: httpx.Request):
        return httpx.Response(503, text="SECRET_INTERNAL_TOKEN")

    stt = asyncio.run(
        benchmark_stt(
            fixtures,
            ["test-model"],
            "https://models.test/v1/audio/transcriptions",
            "",
            1,
            transport=httpx.MockTransport(fail),
        )
    )
    summary = stt["results"]["test-model"]
    assert summary["failed"] == 1 and summary["succeeded"] == 0
    assert summary["http_latency_ms"]["samples"] == 0
    assert summary["peak_gpu_vram_mib"] is None
    assert "SECRET_INTERNAL_TOKEN" not in json.dumps(stt)


def test_empty_stt_response_valid_for_silence(tmp_path):
    fixture = load_audio_manifest(create_audio(tmp_path, scenario="silence"))

    def respond(req: httpx.Request):
        return httpx.Response(200, json={"text": ""})

    result = asyncio.run(
        benchmark_stt(
            fixture,
            ["fake"],
            "https://models.test/v1/audio/transcriptions",
            "",
            1,
            transport=httpx.MockTransport(respond),
        )
    )
    assert result["results"]["fake"]["error_rates_by_language"]["vi"]["successful"] == 1
    assert result["results"]["fake"]["error_rates_by_language"]["vi"]["cer"] is None


def test_cli_requires_real_endpoints_and_blocks_corpus_overwrite(tmp_path, monkeypatch):
    args = parser().parse_args(
        [
            "mt",
            "--models",
            "a,b",
            "--output-dir",
            str(tmp_path),
        ]
    )
    monkeypatch.delenv("NASTYA_MT_URL", raising=False)
    with pytest.raises(EvaluationError, match="NASTYA_MT_URL"):
        asyncio.run(run(args))
    args = parser().parse_args(
        [
            "mt",
            "--models",
            "a,b",
            "--output-dir",
            str(Path(__file__).resolve().parents[1] / "evaluation"),
        ]
    )
    with pytest.raises(EvaluationError, match="outside the source corpus"):
        asyncio.run(run(args))


def test_model_filenames_stable_and_non_sensitive():
    assert model_key("facebook/nllb-200-distilled-600M") == model_key(
        "facebook/nllb-200-distilled-600M"
    )
    assert len(model_key("x")) == 12
