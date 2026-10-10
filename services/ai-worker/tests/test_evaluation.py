import json
from collections import Counter
from pathlib import Path

import pytest

from nastya_worker.evaluate import build_parser, run
from nastya_worker.evaluation import (
    DATASET_DIR,
    EvaluationError,
    aggregate,
    load_corpus,
    make_report,
    overlap_f1,
    summarize_reviews,
    validate_predictions,
    validate_reviews,
)


@pytest.fixture(scope="module")
def corpus():
    return load_corpus()


def test_versioned_synthetic_corpus_has_120_balanced_unique_items(corpus):
    manifest, rows = corpus
    assert manifest["dataset_id"] == "nastya-ru-vi-conversation-v1"
    assert manifest["review_status"] == "draft_bilingual_review_pending"
    assert len(rows) == len({r["id"] for r in rows}) == 120
    assert Counter(r["source_language"] for r in rows) == {"ru": 60, "vi": 60}
    assert all(r["source_language"] != r["target_language"] for r in rows)
    assert all(r["audio_path"] is None for r in rows)
    assert all(r["provenance"] == "synthetic_original" for r in rows)
    assert len({(r["source_language"], r["source_text"]) for r in rows}) == 120
    assert len({(r["source_language"], r["category"]) for r in rows}) == 20
    for language in ("ru", "vi"):
        assert all(
            sum(r["source_language"] == language and r["category"] == cat for r in rows) == 6
            for cat in manifest["categories"]
        )


def test_corpus_validation_rejects_duplicate_ids(tmp_path: Path, corpus):
    _, rows = corpus
    (tmp_path / "manifest.json").write_text(
        (DATASET_DIR / "manifest.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    for filename in ("ru_vi_conversation_v1_vi.jsonl", "ru_vi_conversation_v1_ru.jsonl"):
        (tmp_path / filename).write_text(
            (DATASET_DIR / filename).read_text(encoding="utf-8"), encoding="utf-8"
        )
    file = tmp_path / "ru_vi_conversation_v1_ru.jsonl"
    lines = file.read_text(encoding="utf-8").splitlines()
    damaged = json.loads(lines[1])
    damaged["id"] = rows[0]["id"]
    lines[1] = json.dumps(damaged, ensure_ascii=False)
    file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(EvaluationError, match="duplicate ID"):
        load_corpus(tmp_path)


def test_v1_rejects_recorded_audio_without_consent_metadata(tmp_path: Path):
    for filename in ("manifest.json", "ru_vi_conversation_v1_vi.jsonl",
                     "ru_vi_conversation_v1_ru.jsonl"):
        (tmp_path / filename).write_text(
            (DATASET_DIR / filename).read_text(encoding="utf-8"), encoding="utf-8"
        )
    file = tmp_path / "ru_vi_conversation_v1_vi.jsonl"
    lines = file.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["audio_path"] = "private_conversation.wav"
    lines[0] = json.dumps(first, ensure_ascii=False)
    file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(EvaluationError, match="must not contain audio"):
        load_corpus(tmp_path)


def test_validate_cli_and_deterministic_source_copy_baseline(tmp_path: Path):
    parser = build_parser()
    validation = run(parser.parse_args(["validate"]))
    assert validation["total"] == 120
    assert validation["audio_files"] == 0

    path = tmp_path / "baseline.jsonl"
    args = parser.parse_args(["baseline", "--output", str(path)])
    run(args)
    previous = path.read_bytes()
    run(args)
    assert path.read_bytes() == previous
    _, rows = load_corpus()
    predictions = validate_predictions(rows, path)
    assert all(predictions[r["id"]]["hypothesis"] == r["source_text"] for r in rows)


def test_prediction_validation_rejects_duplicate_bad_latency_and_unknown_id(tmp_path, corpus):
    _, rows = corpus
    data = [{"id": rows[0]["id"], "hypothesis": "Тест", "latency_ms": 12}]
    path = tmp_path / "predictions.jsonl"

    def write(values):
        path.write_text("".join(json.dumps(x) + "\n" for x in values), encoding="utf-8")

    write(data + data)
    with pytest.raises(EvaluationError, match="unknown or duplicate"):
        validate_predictions(rows, path)
    write([{**data[0], "latency_ms": -1}])
    with pytest.raises(EvaluationError, match="latency"):
        validate_predictions(rows, path)
    write([{**data[0], "id": "vi-999"}])
    with pytest.raises(EvaluationError, match="unknown"):
        validate_predictions(rows, path)


def test_automatic_metric_is_a_reference_overlap_proxy_not_semantic_quality(corpus):
    manifest, rows = corpus
    predictions = {r["id"]: {"id": r["id"], "hypothesis": r["reference_text"],
                             "latency_ms": 100.0} for r in rows}
    report = make_report(manifest, rows, predictions, [], "reference-echo-for-cli-sanity")
    assert report["automatic"]["coverage"] == 1
    assert report["automatic"]["character_overlap_f1_proxy"] == 1
    assert report["automatic"]["latency_ms"] == {"samples": 120, "p50": 100.0, "p95": 100.0}
    assert report["human"] == {"status": "not_provided", "rating_count": 0}
    assert report["model_verified"] is False
    assert "NOT semantic fidelity" in report["automatic_metrics_warning"]
    assert overlap_f1("Xin chào", "Xin chào") == 1
    assert overlap_f1("abc", "XYZ") == 0


def test_missing_predictions_explicitly_reduce_coverage(corpus):
    _, rows = corpus
    example = rows[0]
    result = aggregate(rows, {
        example["id"]: {"id": example["id"], "hypothesis": "", "latency_ms": None},
    })
    assert result["prediction_records"] == 1
    assert result["nonempty_predictions"] == 0
    assert result["coverage"] == 0
    assert result["all_present"] is False
    assert result["character_overlap_f1_proxy"] is None


def test_scoring_writes_stable_machine_readable_report(tmp_path: Path):
    parser = build_parser()
    baseline = tmp_path / "copied.jsonl"
    report = tmp_path / "report.json"
    run(parser.parse_args(["baseline", "--output", str(baseline)]))
    args = parser.parse_args([
        "score", "--predictions", str(baseline), "--system", "source-copy-control",
        "--output", str(report),
    ])
    result = run(args)
    assert result["human_status"] == "not_provided"
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["automatic"]["total"] == 120
    assert data["automatic"]["coverage"] == 1
    assert data["automatic"]["latency_ms"]["samples"] == 0
    assert data["system"] == "source-copy-control"
    assert "reference_text" not in report.read_text(encoding="utf-8")


def test_review_template_is_blank_and_requires_real_scores(tmp_path: Path, corpus):
    _, rows = corpus
    parser = build_parser()
    baseline = tmp_path / "predictions.jsonl"
    template = tmp_path / "review.jsonl"
    run(parser.parse_args(["baseline", "--output", str(baseline)]))
    run(parser.parse_args([
        "review-template", "--predictions", str(baseline),
        "--reviewer", "r1", "--output", str(template),
    ]))
    record = json.loads(template.read_text(encoding="utf-8").splitlines()[0])
    assert record["meaning"] is None
    with pytest.raises(EvaluationError, match="meaning"):
        validate_reviews(rows, template, validate_predictions(rows, baseline))


def test_completed_reviews_and_critical_error_summary(tmp_path: Path, corpus):
    _, rows = corpus
    predictions = {r["id"]: {"id": r["id"], "hypothesis": "abc", "latency_ms": None}
                   for r in rows}
    review = {
        "id": rows[0]["id"], "reviewer": "reviewer-a",
        "meaning": 3, "fluency": 4, "entities": 4,
        "negation": 2, "severity": 3, "notes": "Negation inverted",
    }
    path = tmp_path / "reviews.jsonl"
    path.write_text(json.dumps(review) + "\n", encoding="utf-8")
    checked = validate_reviews(rows, path, predictions)
    summary = summarize_reviews(checked, predictions)
    assert summary["status"] == "provisional"
    assert summary["critical_error_ids"] == [rows[0]["id"]]
    assert summary["double_reviewed_items"] == 0

    path.write_text(json.dumps(review) + "\n" + json.dumps(review) + "\n",
                    encoding="utf-8")
    with pytest.raises(EvaluationError, match="duplicate reviewer"):
        validate_reviews(rows, path, predictions)


def test_no_implicit_generated_corpus_overwrite(tmp_path: Path):
    parser = build_parser()
    with pytest.raises(EvaluationError, match="outside the versioned corpus"):
        run(parser.parse_args([
            "baseline", "--output", str(DATASET_DIR / "generated-control.jsonl")
        ]))
    assert not (DATASET_DIR / "generated-control.jsonl").exists()
