"""Reproducible, deliberately conservative RU/VI evaluation data and metrics.

No model is loaded. Character overlap is a diagnostic proxy, NOT semantic fidelity.
"""

import json
import math
import re
import unicodedata
from collections import Counter
from pathlib import Path

DATASET_DIR = Path(__file__).resolve().parents[2] / "evaluation"
CATEGORIES = frozenset({
    "informal", "questions", "negation", "names", "fillers",
    "numbers", "code_switch", "logistics", "emotion", "clarification",
})
FIELDS = frozenset({
    "id", "source_language", "target_language", "source_text",
    "reference_text", "reference_meaning", "category", "audio_path", "provenance",
})
PREDICTION_FIELDS = frozenset({"id", "hypothesis", "latency_ms"})
REVIEW_FIELDS = frozenset({
    "id", "reviewer", "meaning", "fluency", "entities",
    "negation", "severity", "notes",
})


class EvaluationError(ValueError):
    """Public-facing validation error with safe, file/line-level context."""


def _read_jsonl(path: Path) -> list[dict]:
    try:
        with path.open(encoding="utf-8") as stream:
            rows = []
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    raise EvaluationError(f"{path.name}:{line_number}: empty JSONL line")
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise EvaluationError(
                        f"{path.name}:{line_number}: invalid JSON"
                    ) from exc
                if not isinstance(record, dict):
                    raise EvaluationError(f"{path.name}:{line_number}: expected object")
                rows.append(record)
    except OSError as exc:
        raise EvaluationError(f"cannot read {path.name}") from exc
    return rows


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise EvaluationError(f"{field}: expected nonempty string up to 2000 characters")
    if any(ord(ch) < 32 and ch not in "\t\n" for ch in value):
        raise EvaluationError(f"{field}: contains control characters")
    return unicodedata.normalize("NFC", value)


def load_corpus(directory: Path = DATASET_DIR) -> tuple[dict, list[dict]]:
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise EvaluationError("invalid or missing manifest.json") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise EvaluationError("unsupported manifest schema")
    files = manifest.get("files")
    if files != ["ru_vi_conversation_v1_vi.jsonl", "ru_vi_conversation_v1_ru.jsonl"]:
        raise EvaluationError("manifest must list the two expected dataset shards")

    rows: list[dict] = []
    seen_ids: set[str] = set()
    seen_sources: set[tuple[str, str]] = set()
    distribution: Counter[tuple[str, str]] = Counter()
    for filename in files:
        for record in _read_jsonl(directory / filename):
            if set(record) != FIELDS:
                raise EvaluationError(f"{filename}: dataset fields do not match schema")
            sample_id = _string(record["id"], "id")
            src = record["source_language"]
            dst = record["target_language"]
            if src not in ("vi", "ru") or dst not in ("vi", "ru") or src == dst:
                raise EvaluationError(f"{sample_id}: invalid translation direction")
            if not re.fullmatch(rf"{src}-[0-9]{{3}}", sample_id):
                raise EvaluationError(f"{sample_id}: invalid utterance ID")
            if sample_id in seen_ids:
                raise EvaluationError(f"{sample_id}: duplicate ID")
            seen_ids.add(sample_id)
            source = _string(record["source_text"], "source_text")
            _string(record["reference_text"], "reference_text")
            _string(record["reference_meaning"], "reference_meaning")
            category = record["category"]
            if category not in CATEGORIES:
                raise EvaluationError(f"{sample_id}: unsupported category")
            if record["provenance"] != "synthetic_original":
                raise EvaluationError(f"{sample_id}: non-synthetic provenance not permitted in v1")
            if record["audio_path"] is not None:
                raise EvaluationError(f"{sample_id}: v1 corpus must not contain audio")
            source_key = (src, source.casefold())
            if source_key in seen_sources:
                raise EvaluationError(f"{sample_id}: duplicate source utterance")
            seen_sources.add(source_key)
            if src == "vi" and not filename.endswith("_vi.jsonl"):
                raise EvaluationError(f"{sample_id}: unexpected file direction")
            if src == "ru" and not filename.endswith("_ru.jsonl"):
                raise EvaluationError(f"{sample_id}: unexpected file direction")
            distribution[(src, category)] += 1
            rows.append(record)
    expected_count = manifest.get("expected_count")
    expected_per_direction = manifest.get("expected_per_direction")
    if len(rows) != expected_count or expected_count != 120 or expected_per_direction != 60:
        raise EvaluationError("manifest count must match 120 balanced examples")
    if any(distribution[(language, category)] != 6
           for language in ("vi", "ru") for category in CATEGORIES):
        raise EvaluationError("need six distinct examples per direction and category")
    return manifest, sorted(rows, key=lambda row: row["id"])


def validate_predictions(rows: list[dict], path: Path) -> dict[str, dict]:
    allowed = {row["id"] for row in rows}
    predictions: dict[str, dict] = {}
    for record in _read_jsonl(path):
        if set(record) != PREDICTION_FIELDS:
            raise EvaluationError("prediction must have id, hypothesis and latency_ms")
        sample_id = _string(record["id"], "id")
        if sample_id not in allowed or sample_id in predictions:
            raise EvaluationError(f"{sample_id}: unknown or duplicate prediction")
        text = record["hypothesis"]
        if not isinstance(text, str) or len(text) > 4000:
            raise EvaluationError(f"{sample_id}: invalid hypothesis")
        latency = record["latency_ms"]
        if latency is not None and (
            isinstance(latency, bool) or not isinstance(latency, int | float)
            or not math.isfinite(latency) or latency < 0
        ):
            raise EvaluationError(f"{sample_id}: invalid latency_ms")
        predictions[sample_id] = record
    return predictions


def validate_reviews(rows: list[dict], path: Path, predictions: dict[str, dict]) -> list[dict]:
    allowed = {row["id"] for row in rows}
    reviews: list[dict] = []
    unique: set[tuple[str, str]] = set()
    for review in _read_jsonl(path):
        if set(review) != REVIEW_FIELDS:
            raise EvaluationError("review must have all rubric fields")
        sample_id = _string(review["id"], "review id")
        reviewer = _string(review["reviewer"], "reviewer")
        if sample_id not in allowed or sample_id not in predictions:
            raise EvaluationError(f"{sample_id}: review without prediction")
        if (sample_id, reviewer) in unique:
            raise EvaluationError(f"{sample_id}: duplicate reviewer assessment")
        unique.add((sample_id, reviewer))
        for field in ("meaning", "fluency", "entities", "negation"):
            val = review[field]
            if type(val) is not int or not 0 <= val <= 4:
                raise EvaluationError(f"{sample_id}: {field} must be an integer from 0 to 4")
        severity = review["severity"]
        if type(severity) is not int or not 0 <= severity <= 3:
            raise EvaluationError(f"{sample_id}: severity must be 0..3")
        if not isinstance(review["notes"], str) or len(review["notes"]) > 1500:
            raise EvaluationError(f"{sample_id}: invalid notes")
        reviews.append(review)
    return reviews


def overlap_f1(reference: str, hypothesis: str) -> float:
    """Character 1-3gram F1 diagnostic, not chrF/BLEU or human judgment."""
    def grams(text: str, n: int) -> Counter[str]:
        normalized = unicodedata.normalize("NFC", text).casefold().strip()
        normalized = re.sub(r"\s+", " ", normalized)
        return Counter(normalized[i:i+n] for i in range(max(0, len(normalized)-n+1)))

    scores = []
    for n in (1, 2, 3):
        gold, predicted = grams(reference, n), grams(hypothesis, n)
        if not gold or not predicted:
            scores.append(0.0)
            continue
        matches = sum((gold & predicted).values())
        p = matches / sum(predicted.values())
        r = matches / sum(gold.values())
        scores.append(2 * p * r / (p + r) if p + r else 0.0)
    return sum(scores) / len(scores)


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * percent
    low, high = math.floor(index), math.ceil(index)
    return ordered[low] * (high - index) + ordered[high] * (index - low) if low != high else ordered[low]


def aggregate(rows: list[dict], predictions: dict[str, dict]) -> dict:
    count = len(rows)
    scored = [r for r in rows if r["id"] in predictions and predictions[r["id"]]["hypothesis"].strip()]
    latencies = [float(x["latency_ms"]) for x in predictions.values()
                 if x["latency_ms"] is not None]

    def summary(items: list[dict]) -> dict:
        usable = [r for r in items if r["id"] in predictions
                  and predictions[r["id"]]["hypothesis"].strip()]
        return {
            "total": len(items), "nonempty_predictions": len(usable),
            "coverage": round(len(usable) / len(items), 4) if items else 0,
            "character_overlap_f1_proxy": round(sum(
                overlap_f1(r["reference_text"], predictions[r["id"]]["hypothesis"])
                for r in usable
            ) / len(usable), 4) if usable else None,
        }

    return {
        "total": count,
        "prediction_records": len(predictions),
        "nonempty_predictions": len(scored),
        "coverage": round(len(scored) / count, 4) if count else 0,
        "all_present": len(predictions) == count,
        "character_overlap_f1_proxy": summary(rows)["character_overlap_f1_proxy"],
        "latency_ms": {
            "samples": len(latencies),
            "p50": round(percentile(latencies, 0.5), 1) if latencies else None,
            "p95": round(percentile(latencies, 0.95), 1) if latencies else None,
        },
        "by_direction": {
            direction: summary([r for r in rows if r["source_language"] == direction])
            for direction in ("vi", "ru")
        },
        "by_category": {
            category: summary([r for r in rows if r["category"] == category])
            for category in sorted(CATEGORIES)
        },
    }


def summarize_reviews(reviews: list[dict], predictions: dict[str, dict]) -> dict:
    if not reviews:
        return {"status": "not_provided", "rating_count": 0}
    counts: Counter[str] = Counter(r["id"] for r in reviews)
    scores = {field: round(sum(r[field] for r in reviews)/len(reviews), 3)
              for field in ("meaning", "fluency", "entities", "negation", "severity")}
    critical = sorted({r["id"] for r in reviews if r["severity"] == 3})
    return {
        "status": "double_reviewed" if len(counts) == len(predictions)
                  and min(counts.values()) >= 2 else "provisional",
        "rating_count": len(reviews),
        "reviewed_items": len(counts),
        "double_reviewed_items": sum(n >= 2 for n in counts.values()),
        "reviewer_count": len({r["reviewer"] for r in reviews}),
        "mean_scores": scores,
        "critical_error_ids": critical,
        "critical_error_count": len(critical),
    }


def make_report(
    manifest: dict, rows: list[dict], predictions: dict[str, dict],
    reviews: list[dict], system: str,
) -> dict:
    if not system.strip() or len(system) > 100:
        raise EvaluationError("system name must be 1..100 characters")
    return {
        "schema_version": 1,
        "dataset_id": manifest["dataset_id"],
        "dataset_review_status": manifest["review_status"],
        "system": system,
        "model_verified": False,
        "automatic_metrics_warning": "Character overlap proxy is NOT semantic fidelity.",
        "automatic": aggregate(rows, predictions),
        "human": summarize_reviews(reviews, predictions),
    }
