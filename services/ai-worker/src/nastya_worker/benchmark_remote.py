"""Run NAS remote-provider performance harnesses without hosting GPU models."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from nastya_worker.audio_manifest import load_audio_manifest
from nastya_worker.config import Settings, _endpoint
from nastya_worker.evaluate import _write_json, _write_jsonl
from nastya_worker.evaluation import EvaluationError, load_corpus
from nastya_worker.remote_benchmark import (
    benchmark_mt,
    benchmark_stt,
    benchmark_vad,
    check_models,
    model_key,
)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Remote API benchmarking; requires real endpoints")
    sub = p.add_subparsers(dest="kind", required=True)
    for name in ("mt", "stt"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--models", required=True, help="comma-separated server model IDs")
        cmd.add_argument("--output-dir", type=Path, required=True)
        cmd.add_argument("--concurrency", type=int, default=1)
        cmd.add_argument("--rounds", type=int, default=1)
        cmd.add_argument("--warmup", type=int, default=0)
        if name == "stt":
            cmd.add_argument("--audio-manifest", type=Path, required=True)
            cmd.add_argument("--vad-model", default="")
        else:
            cmd.add_argument("--dataset", type=Path, default=None)
    return p


def _valid_args(args: argparse.Namespace) -> list[str]:
    models = check_models(args.models.split(","))
    if not 1 <= args.concurrency <= 16 or not 1 <= args.rounds <= 10:
        raise EvaluationError("concurrency must be 1..16 and rounds must be 1..10")
    if not 0 <= args.warmup <= 3:
        raise EvaluationError("warmup must be 0..3")
    if args.output_dir.resolve().is_relative_to(
        (Path(__file__).resolve().parents[2] / "evaluation").resolve()
    ):
        raise EvaluationError("benchmark outputs must be outside the source corpus")
    if args.kind == "mt" and len(models) < 2:
        raise EvaluationError("MT comparison must request at least two model IDs")
    return models


async def run(args: argparse.Namespace) -> dict:
    models = _valid_args(args)
    settings = Settings.from_env()
    if args.kind == "mt":
        if not settings.mt_url:
            raise EvaluationError("NASTYA_MT_URL is required")
        _, rows = load_corpus(args.dataset) if args.dataset else load_corpus()
        result, predictions = await benchmark_mt(
            rows, models, settings.mt_url, settings.mt_api_key,
            settings.http_timeout_seconds, args.concurrency, args.rounds, args.warmup,
        )
        outputs = {}
        for model, records in predictions.items():
            file = args.output_dir / ("mt-" + model_key(model) + "-predictions.jsonl")
            _write_jsonl(file, records)
            outputs[model] = file.name
        result["prediction_files"] = outputs
        result["dataset_id"] = "nastya-ru-vi-conversation-v1"
        result["references_reviewed"] = False
    else:
        if not settings.stt_url:
            raise EvaluationError("NASTYA_STT_URL is required")
        audio = load_audio_manifest(args.audio_manifest)
        result = await benchmark_stt(
            audio, models, settings.stt_url, settings.stt_api_key,
            settings.http_timeout_seconds, args.concurrency, args.rounds, args.warmup,
        )
        vad = os.getenv("NASTYA_VAD_URL", "")
        if vad:
            url = _endpoint(vad, "NASTYA_VAD_URL")
            result["vad"] = await benchmark_vad(
                audio, url, os.getenv("NASTYA_VAD_API_KEY", ""),
                settings.http_timeout_seconds, args.vad_model,
            )
        else:
            result["vad"] = {
                "status": "not_measured",
                "reason": "Set NASTYA_VAD_URL to a compatible endpoint",
            }
        result["audio_provenance"] = "external_manifest_rights_attested_by_operator"
    result["measurement_scope"] = "client_observed_http_batch_inference"
    result["backend_gpu_metrics"] = "unavailable_without_host_instrumentation"
    result["first_partial_latency"] = "not_measured_batch_api"
    result["selection_decision"] = "not_established_by_automatic_metrics"
    result["configuration"] = {
        "models_requested": models,
        "rounds": args.rounds,
        "concurrency": args.concurrency,
        "warmup_per_model": args.warmup,
        "http_timeout_seconds": settings.http_timeout_seconds,
    }
    summary_path = args.output_dir / (args.kind + "-benchmark-report.json")
    _write_json(summary_path, result)
    return {"report": str(summary_path), "models_requested": models,
            "warning": "No server model attestation, peak VRAM or streaming partial measurements"}


def main() -> None:
    cli = parser()
    args = cli.parse_args()
    try:
        print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, sort_keys=True))
    except (EvaluationError, ValueError, OSError) as exc:
        cli.exit(2, f"benchmark error: {exc}\n")


if __name__ == "__main__":
    main()
