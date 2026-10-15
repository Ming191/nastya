"""Opt-in real chat-provider comparison on the fixed synthetic NAS-8 corpus."""

import argparse
import asyncio
import hashlib
import json
import math
import os
import time
from contextvars import ContextVar
from pathlib import Path

import httpx

from nastya_worker.analyse_captions import summary
from nastya_worker.config import Settings
from nastya_worker.evaluate import _write_json, _write_jsonl
from nastya_worker.evaluation import CATEGORIES, EvaluationError, aggregate, load_corpus
from nastya_worker.providers.http import ChatCompletionTranslator, InferenceError
from nastya_worker.remote_benchmark import check_models, model_key

REQUEST_INFO: ContextVar[dict | None] = ContextVar("translation_comparison_request", default=None)
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
TIME_FIELDS = ("queue_time", "prompt_time", "completion_time", "total_time")


async def capture_response(response: httpx.Response) -> None:
    """Capture allowlisted metadata, never headers with credentials or reasoning text."""
    info = REQUEST_INFO.get()
    if info is None:
        return
    info["http_status"] = response.status_code
    info["quota"] = {
        key: response.headers[key]
        for key in (
            "x-ratelimit-limit-requests",
            "x-ratelimit-limit-tokens",
            "x-ratelimit-remaining-requests",
            "x-ratelimit-remaining-tokens",
            "retry-after",
        )
        if key in response.headers
    }
    await response.aread()
    if not response.is_success:
        return
    try:
        payload = response.json()
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    model = payload.get("model")
    info["reported_model"] = model if isinstance(model, str) and len(model) <= 160 else None
    usage = payload.get("usage")
    if isinstance(usage, dict):
        info["tokens"] = {
            key: usage[key]
            for key in TOKEN_FIELDS
            if type(usage.get(key)) is int and usage[key] >= 0
        }
        info["provider_seconds"] = {
            key: usage[key]
            for key in TIME_FIELDS
            if type(usage.get(key)) in (int, float)
            and math.isfinite(usage[key])
            and usage[key] >= 0
        }


def summarise(records: list[dict], rows: list[dict]) -> dict:
    expected = {row["id"] for row in rows}
    if len(expected) != len(rows) or len(records) != len(rows):
        raise EvaluationError("comparison must contain exactly one result per source")
    if {record["id"] for record in records} != expected:
        raise EvaluationError("missing, duplicate or unexpected comparison ID")
    by_id = {record["id"]: record for record in records}

    def group(items):
        values = [by_id[row["id"]] for row in items]
        valid = [item for item in values if item["error"] is None]
        if any(
            not isinstance(item["hypothesis"], str) or not item["hypothesis"].strip()
            for item in valid
        ):
            raise EvaluationError("successful translation must be nonempty")
        return {
            "scheduled": len(values),
            "succeeded": len(valid),
            "failed": len(values) - len(valid),
            "response_ms": summary([v["response_ms"] for v in valid]),
            "failed_response_ms": summary([v["response_ms"] for v in values if v["error"]]),
            "queue_ms": summary([v["queue_ms"] for v in values]),
            "reported_model_counts": {
                model: sum(v.get("reported_model") == model for v in valid)
                for model in sorted({v["reported_model"] for v in valid if v.get("reported_model")})
            },
            "tokens": {
                key: {
                    "measured": sum(key in v.get("tokens", {}) for v in valid),
                    "total": sum(v.get("tokens", {}).get(key, 0) for v in valid)
                    if any(key in v.get("tokens", {}) for v in valid)
                    else None,
                }
                for key in TOKEN_FIELDS
            },
            "provider_seconds": {
                key: summary([v.get("provider_seconds", {}).get(key) for v in valid])
                for key in TIME_FIELDS
            },
        }

    predictions = {
        r["id"]: {
            "id": r["id"],
            "hypothesis": r["hypothesis"],
            "latency_ms": r["response_ms"] if r["error"] is None else None,
        }
        for r in records
        if r["error"] is None
    }
    return {
        **group(rows),
        "by_direction": {
            language: group([r for r in rows if r["source_language"] == language])
            for language in ("vi", "ru")
        },
        "by_category": {
            category: group([r for r in rows if r["category"] == category])
            for category in sorted(CATEGORIES)
        },
        "automatic_diagnostics": aggregate(rows, predictions),
        "human_quality": None,
        "references_independently_reviewed": False,
        "peak_gpu_vram_mib": None,
        "server_beam_size": None,
        "server_precision": None,
        "first_partial_ms": None,
        "model_weights_independently_attested": False,
    }


def model_specs(values: list[str]) -> list[tuple[str, str]]:
    specs = []
    for value in values:
        model, separator, effort = value.rpartition("=")
        if not separator or effort not in {"none", "low", "medium", "high", "default"}:
            raise EvaluationError("models must use ID=none|low|medium|high|default")
        specs.append((model, "" if effort == "default" else effort))
    check_models([model for model, _ in specs])
    if len(specs) < 2:
        raise EvaluationError("comparison requires at least two distinct model IDs")
    return specs


async def compare(rows, specs, client, settings, interval, concurrency, output):
    # Alternate directions, rotate model order per batch. Pacing is global and
    # outside request latency; concurrency=2 launches one RU/VI pair together.
    vi = [row for row in rows if row["source_language"] == "vi"]
    ru = [row for row in rows if row["source_language"] == "ru"]
    ordered = [row for pair in zip(vi, ru, strict=True) for row in pair]
    records = {model: [] for model, _ in specs}
    providers = {
        model: ChatCompletionTranslator(client, settings.mt_url, settings.mt_api_key, model, effort)
        for model, effort in specs
    }
    last_start = None
    started = time.perf_counter()

    async def request(row, model, queued):
        began = time.perf_counter()
        info = {}
        context = REQUEST_INFO.set(info)
        try:
            result = await providers[model].translate(
                row["source_text"], row["source_language"], row["target_language"]
            )
            text, error = result.text, None
        except (InferenceError, httpx.HTTPError):
            text, error = "", "INFERENCE_FAILED"
        finally:
            REQUEST_INFO.reset(context)
        item = {
            "id": row["id"],
            "hypothesis": text,
            "error": error,
            "response_ms": round((time.perf_counter() - began) * 1000, 2),
            "queue_ms": round((began - queued) * 1000, 2),
            **info,
        }
        records[model].append(item)
        # Persist each completed batch so partial runs remain inspectable.
        with (output / (model_key(model) + "-requests.jsonl")).open("a", encoding="utf8") as stream:
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")
        print(json.dumps({"model": model, "id": row["id"], "ok": error is None}), flush=True)

    for index in range(0, len(ordered), concurrency):
        batch = ordered[index : index + concurrency]
        offset = (index // concurrency) % len(specs)
        for model, _ in specs[offset:] + specs[:offset]:
            queued = time.perf_counter()
            if last_start is not None:
                await asyncio.sleep(max(0, last_start + interval * concurrency - queued))
            last_start = time.perf_counter()
            await asyncio.gather(*(request(row, model, queued) for row in batch))
    elapsed = time.perf_counter() - started
    result = {}
    for model, effort in specs:
        result[model] = summarise(records[model], rows)
        result[model]["reasoning_effort"] = effort or "provider_default"
        _write_jsonl(
            output / (model_key(model) + "-predictions.jsonl"),
            [
                {
                    "id": r["id"],
                    "hypothesis": r["hypothesis"],
                    "latency_ms": r["response_ms"] if r["error"] is None else None,
                }
                for r in records[model]
            ],
        )
    return {
        "results": result,
        "wall_seconds": round(elapsed, 3),
        "paced_successes_per_second": round(
            sum(r["error"] is None for rs in records.values() for r in rs) / elapsed, 4
        ),
    }


async def run(args):
    specs = model_specs(args.model)
    settings = Settings.from_env()
    if settings.mt_provider != "chat" or not settings.mt_url:
        raise EvaluationError("configure NASTYA_MT_PROVIDER=chat and NASTYA_MT_URL")
    manifest, rows = load_corpus()
    if any(row["provenance"] != "synthetic_original" for row in rows):
        raise EvaluationError("only the original synthetic corpus is allowed")
    args.output.mkdir(parents=True, exist_ok=False)
    async with httpx.AsyncClient(
        timeout=settings.mt_timeout_seconds, event_hooks={"response": [capture_response]}
    ) as client:
        report = await compare(
            rows, specs, client, settings, args.interval, args.concurrency, args.output
        )
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf8")
    report.update(
        {
            "dataset_id": manifest["dataset_id"],
            "corpus_count": len(rows),
            "corpus_sha256": hashlib.sha256(encoded).hexdigest(),
            "measurement_scope": "client_observed_chat_translation_not_realtime_RTC",
            "concurrency": args.concurrency,
            "global_interval_seconds": args.interval,
            "p95_method": "nearest_rank_ceil_0.95_n",
            "warmup_requests": 0,
            "timeout_seconds": settings.mt_timeout_seconds,
            "decoding": {"temperature": 0, "max_completion_tokens": 2048, "format": "json_object"},
            "selection": "provisional_only_until_independent_bilingual_review",
            "warning": "Overlap is not semantic quality; throughput includes quota pacing.",
        }
    )
    _write_json(args.output / "summary.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new directory outside the repo")
    parser.add_argument(
        "--model", action="append", required=True, help="ID=effort; repeat per model"
    )
    parser.add_argument("--interval", type=float, default=0.75)
    parser.add_argument("--concurrency", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    try:
        model_specs(args.model)
        if not math.isfinite(args.interval) or not 0.1 <= args.interval <= 60:
            raise EvaluationError("interval must be 0.1..60 seconds")
        repo = Path(__file__).resolve().parents[4]
        if args.output.resolve().is_relative_to(repo):
            raise EvaluationError("write synthetic predictions outside the repository")
        for line in args.env_file.read_text(encoding="utf8").splitlines():
            if line.startswith("NASTYA_") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key, value)
        report = asyncio.run(run(args))
        print(
            json.dumps(
                {
                    "report": str(args.output / "summary.json"),
                    "requests": sum(r["scheduled"] for r in report["results"].values()),
                }
            )
        )
    except (EvaluationError, OSError, ValueError):
        parser.exit(2, "comparison failed; check configuration and partial output files\n")


if __name__ == "__main__":
    main()
