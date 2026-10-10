"""Run reproducible remote STT/MT comparisons over fixed inputs.

Only measures HTTP-observed response times and outputs. Model identity, GPU
memory, streaming partials and server compute settings are never inferred.
"""

import asyncio
import hashlib
import json
import time
from collections import defaultdict
from pathlib import Path

import httpx

from nastya_worker.audio_manifest import boundary_error_ms, validate_vad_response
from nastya_worker.benchmark_metrics import error_counts, percentile_stats, ratio
from nastya_worker.evaluation import EvaluationError, aggregate
from nastya_worker.providers.http import (
    HttpSpeechRecognizer,
    HttpTranslator,
    InferenceError,
    _post,
)


def model_key(model: str) -> str:
    return hashlib.sha256(model.encode("utf-8")).hexdigest()[:12]


def check_models(models: list[str]) -> list[str]:
    if not models or len(models) > 8 or len(set(models)) != len(models):
        raise EvaluationError("supply 1..8 distinct model IDs")
    if any(not item or len(item) > 160 or any(c.isspace() for c in item) for item in models):
        raise EvaluationError("model IDs must be short nonempty strings without whitespace")
    return models


async def _execute(requests: list[tuple[str, object]], concurrency: int, action):
    sem = asyncio.Semaphore(concurrency)
    created_at = time.perf_counter()

    async def task(uid: str, value: object):
        queued_at = time.perf_counter()
        async with sem:
            acquired = time.perf_counter()
            try:
                result = await action(value)
                return {
                    "id": uid, "result": result, "error": None,
                    "queue_ms": (acquired - queued_at) * 1000,
                    "latency_ms": (time.perf_counter() - acquired) * 1000,
                }
            except (InferenceError, ValueError, httpx.HTTPError, EvaluationError):
                return {
                    "id": uid, "result": None, "error": "INFERENCE_FAILED",
                    "queue_ms": (acquired - queued_at) * 1000,
                    "latency_ms": (time.perf_counter() - acquired) * 1000,
                }

    results = await asyncio.gather(*(task(uid, value) for uid, value in requests))
    elapsed_s = time.perf_counter() - created_at
    return results, elapsed_s


def _model_summary(samples: list[dict], elapsed: float, concurrency: int) -> dict:
    succeeded = [r for r in samples if r["error"] is None]
    return {
        "requests": len(samples),
        "succeeded": len(succeeded),
        "failed": len(samples) - len(succeeded),
        "http_latency_ms": percentile_stats([r["latency_ms"] for r in succeeded]),
        "queue_wait_ms": percentile_stats([r["queue_ms"] for r in samples]),
        "wall_seconds": round(elapsed, 3),
        "completed_requests_per_second": round(len(samples) / elapsed, 3) if elapsed else None,
        "concurrency": concurrency,
        "model_identity_attested": False,
        "peak_gpu_vram_mib": None,
        "server_precision": None,
        "server_beam_size": None,
        "server_queue_time_ms": None,
        "first_partial_latency_ms": None,
        "notes": "HTTP elapsed only; server model ID, GPU and decoding config not attested.",
    }


async def benchmark_mt(
    rows: list[dict],
    models: list[str],
    endpoint: str,
    api_key: str,
    timeout: float,
    concurrency: int = 1,
    rounds: int = 1,
    warmup: int = 0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[dict, dict[str, list[dict]]]:
    check_models(models)
    if not rows:
        raise EvaluationError("empty translation corpus")
    report = {"kind": "mt", "corpus_count": len(rows), "results": {}}
    predictions: dict[str, list[dict]] = {}
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        for model in models:
            provider = HttpTranslator(client, endpoint, api_key, model)
            for row in rows[:warmup]:
                await provider.translate(row["source_text"], row["source_language"],
                                         row["target_language"])
            inputs = [
                (f'{row["id"]}:{round_id}', row)
                for round_id in range(rounds) for row in rows
            ]

            async def translate(row: dict):
                return (await provider.translate(
                    row["source_text"], row["source_language"], row["target_language"]
                )).text

            samples, elapsed = await _execute(inputs, concurrency, translate)
            by_id: dict[str, list[dict]] = defaultdict(list)
            for sample in samples:
                by_id[sample["id"].rsplit(":", 1)[0]].append(sample)
            model_predictions = []
            for row in rows:
                valid = [item for item in by_id[row["id"]] if item["error"] is None]
                model_predictions.append({
                    "id": row["id"],
                    "hypothesis": valid[0]["result"] if valid else "",
                    "latency_ms": round(
                        sum(item["latency_ms"] for item in valid) / len(valid), 2
                    ) if valid else None,
                })
            predictions[model] = model_predictions
            count = {r["id"]: r for r in model_predictions}
            summary = _model_summary(samples, elapsed, concurrency)
            summary["repeat_count"] = rounds
            summary["automatic_translation"] = aggregate(rows, count)
            summary["human_quality"] = "not_evaluated"
            report["results"][model] = summary
    return report, predictions


async def benchmark_stt(
    samples: list[dict],
    models: list[str],
    endpoint: str,
    api_key: str,
    timeout: float,
    concurrency: int = 1,
    rounds: int = 1,
    warmup: int = 0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    check_models(models)
    if not samples:
        raise EvaluationError("empty audio fixtures")
    result: dict = {"kind": "stt", "fixture_count": len(samples), "results": {}}
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        for model in models:
            provider = HttpSpeechRecognizer(client, endpoint, api_key, model)
            for sample in samples[:warmup]:
                await provider.transcribe(sample["_path"].read_bytes(), sample["language"])
            inputs = [
                (f'{s["id"]}:{round_id}', s)
                for round_id in range(rounds) for s in samples
            ]

            async def transcribe(sample: dict):
                return (await provider.transcribe(
                    sample["_path"].read_bytes(), sample["language"]
                )).text

            records, elapsed = await _execute(inputs, concurrency, transcribe)
            count = defaultdict(lambda: {
                "word_errors": 0, "reference_words": 0,
                "character_errors": 0, "reference_characters": 0,
                "successes": 0, "failures": 0,
            })
            for record, (_, sample) in zip(records, inputs, strict=True):
                group = count[sample["language"]]
                if record["error"] is not None:
                    group["failures"] += 1
                    continue
                error = error_counts(sample["transcript"], record["result"])
                for name, value in error.items():
                    group[name] += value
                group["successes"] += 1
            summary = _model_summary(records, elapsed, concurrency)
            summary["repeat_count"] = rounds
            summary["error_rates_by_language"] = {
                lang: {
                    "successful": count[lang]["successes"],
                    "failed": count[lang]["failures"],
                    "wer_whitespace_proxy": ratio(
                        count[lang]["word_errors"], count[lang]["reference_words"]
                    ),
                    "cer": ratio(count[lang]["character_errors"],
                                 count[lang]["reference_characters"]),
                }
                for lang in ("vi", "ru")
            }
            summary["segmentation"] = "not_evaluated_without_vad_endpoint"
            result["results"][model] = summary
    return result


async def benchmark_vad(
    samples: list[dict],
    endpoint: str,
    api_key: str,
    timeout: float,
    model: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    measures = []
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        for sample in samples:
            data = {"model": model}
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            try:
                payload = await _post(client, endpoint, data=data, headers=headers,
                                      files={"file": ("speech.wav", sample["_path"].read_bytes(),
                                                      "audio/wav")})
                predicted = validate_vad_response(payload, sample["duration_ms"])
                metrics = boundary_error_ms(sample["speech_segments_ms"], predicted)
                measures.append({"scenario": sample["scenario"], **metrics, "failed": False})
            except (InferenceError, EvaluationError):
                measures.append({"scenario": sample["scenario"], "failed": True})
    valid = [item for item in measures if not item["failed"]]
    errors = [item["mean_boundary_error_ms"] for item in valid
              if item["mean_boundary_error_ms"] is not None]
    return {
        "status": "measured_api" if valid else "all_failed",
        "samples": len(samples),
        "successful": len(valid),
        "failed": len(measures) - len(valid),
        "missed_segments": sum(m["missed_segments"] for m in valid),
        "false_positive_segments": sum(m["false_positive_segments"] for m in valid),
        "mean_boundary_error_ms": round(sum(errors)/len(errors), 2) if errors else None,
        "by_scenario": {
            s: {
                "samples": sum(m["scenario"] == s for m in measures),
                "failures": sum(m["scenario"] == s and m["failed"] for m in measures),
                "false_positive_segments": sum(
                    m.get("false_positive_segments", 0) for m in measures if m["scenario"] == s
                ),
            }
            for s in ("speech", "noise", "interruption", "silence")
        },
        "first_partial_latency_ms": None,
        "warning": "Batch VAD endpoint; no partial timestamps or actual streaming segmentation.",
    }
