"""Opt-in comparison on an explicit synthetic audio manifest, never real calls."""

import argparse
import asyncio
import hashlib
import json
import os
import time
import wave
from collections import defaultdict
from pathlib import Path

import httpx

from nastya_worker.analyse_captions import canonical_numbers, summary
from nastya_worker.audio_manifest import boundary_error_ms, load_audio_manifest
from nastya_worker.benchmark_metrics import error_counts, ratio
from nastya_worker.config import Settings
from nastya_worker.providers.http import HttpSpeechRecognizer, InferenceError
from nastya_worker.remote_benchmark import check_models
from nastya_worker.speaker_pipeline import Segmenter, VadOptions


def pcm(path):
    with wave.open(str(path), "rb") as reader:
        return reader.readframes(reader.getnframes())


def summarise_stt(records, samples):
    expected = {sample["id"]: sample for sample in samples}
    if len(records) != len(samples) or {r["id"] for r in records} != set(expected):
        raise ValueError("incomplete or duplicated STT comparison")
    result = {}
    for language in ("vi", "ru"):
        groups = {}
        for scenario in ("all", "speech", "noise", "interruption", "silence"):
            rows = [
                r
                for r in records
                if expected[r["id"]]["language"] == language
                and (scenario == "all" or expected[r["id"]]["scenario"] == scenario)
            ]
            success = [r for r in rows if r["error"] is None]
            scored = [
                error_counts(expected[r["id"]]["transcript"], r["text"])
                for r in success
                if expected[r["id"]]["transcript"]
            ]
            canonical = [
                error_counts(
                    canonical_numbers(expected[r["id"]]["transcript"]), canonical_numbers(r["text"])
                )
                for r in success
                if expected[r["id"]]["transcript"]
            ]
            silent = [r for r in success if expected[r["id"]]["scenario"] == "silence"]
            groups[scenario] = {
                "scheduled": len(rows),
                "succeeded": len(success),
                "failed": len(rows) - len(success),
                "scored": len(scored),
                "response_ms": summary([r["responseMs"] for r in success]),
                "queue_ms": summary([r["queueMs"] for r in rows]),
                "wer_whitespace_proxy": ratio(
                    sum(c["word_errors"] for c in scored), sum(c["reference_words"] for c in scored)
                ),
                "cer": ratio(
                    sum(c["character_errors"] for c in scored),
                    sum(c["reference_characters"] for c in scored),
                ),
                "number_canonicalized_wer": ratio(
                    sum(c["word_errors"] for c in canonical),
                    sum(c["reference_words"] for c in canonical),
                ),
                "silence_hallucinations": sum(bool(r["text"].strip()) for r in silent),
                "silence_tested": len(silent),
                "non_speech_tested": sum(not expected[r["id"]]["transcript"] for r in success),
                "non_speech_hallucinations": sum(
                    bool(r["text"].strip()) for r in success if not expected[r["id"]]["transcript"]
                ),
            }
        result[language] = groups
    return result


def compare_vad(samples):
    import numpy as np
    from faster_whisper.vad import VadOptions as SileroOptions
    from faster_whisper.vad import get_speech_timestamps

    reports = {}
    for kind, silence in (("energy", 250), ("energy", 350), ("silero", 250), ("silero", 350)):
        rows = []
        for sample in samples:
            raw = pcm(sample["_path"])
            started = time.perf_counter()
            if kind == "energy":
                segmenter = Segmenter(
                    "human:owner", sample["language"], 1, VadOptions(silence_ms=silence)
                )
                segments = []
                for offset in range(0, len(raw), 640):
                    segment = segmenter.feed(raw[offset : offset + 640])
                    if segment is not None:
                        segments.append([segment.start_ms, segment.end_ms])
                tail = segmenter.flush()
                if tail is not None:
                    segments.append([tail.start_ms, tail.end_ms])
            else:
                audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
                intervals = get_speech_timestamps(
                    audio,
                    SileroOptions(
                        min_silence_duration_ms=silence,
                        speech_pad_ms=120,
                        min_speech_duration_ms=150,
                        max_speech_duration_s=6,
                    ),
                )
                segments = [[x["start"] / 16, x["end"] / 16] for x in intervals]
            elapsed = (time.perf_counter() - started) * 1000
            rows.append(
                {
                    "id": sample["id"],
                    "language": sample["language"],
                    "scenario": sample["scenario"],
                    "segments": segments,
                    "computeMs": elapsed,
                    **boundary_error_ms(sample["speech_segments_ms"], segments),
                }
            )
        reports[f"{kind}-{silence}"] = {
            "configuration": {
                "kind": kind,
                "silence_ms": silence,
                "max_segment_ms": 6000,
                "pre_roll_or_pad_ms": 120,
                "silero_threshold": 0.5 if kind == "silero" else None,
            },
            "scope": (
                "offline replay; boundaries compared with synthetic PCM activity proxies, "
                "not human annotations"
            ),
            "rows": rows,
            "compute_ms": summary([r["computeMs"] for r in rows]),
            "cold_first_call_ms": rows[0]["computeMs"],
            "warm_compute_ms": summary([r["computeMs"] for r in rows[1:]]),
            "missed_proxy_segments": sum(r["missed_segments"] for r in rows),
            "extra_proxy_segments": sum(r["false_positive_segments"] for r in rows),
        }
    return reports


async def run(args):
    samples = load_audio_manifest(args.manifest)
    if args.two_speakers:
        selected = {
            lang: [s for s in samples if s["language"] == lang and s["scenario"] == "speech"][:3]
            for lang in ("vi", "ru")
        }
        if any(len(rows) != 3 for rows in selected.values()):
            raise ValueError("paired run requires three speech fixtures per language")
        samples = [s for pair in zip(selected["vi"], selected["ru"], strict=True) for s in pair]
    if any(s["provenance"] != "synthetic" for s in samples):
        raise ValueError("this scoring command permits synthetic fixtures only")
    args.output.mkdir(parents=True, exist_ok=False)
    report = {
        "manifestSha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        "fixture_count": len(samples),
        "scope": "synthetic batch STT response, not RTC end-to-end",
        "first_partial_ms": None,
        "remote_vram_bytes": None,
        "backend_precision": None,
        "backend_beam_size": None,
        "human_quality": None,
        "p95_method": "nearest rank ceil(0.95*n); descriptive with small n",
        "results": {},
        "dispatch_concurrency": 2 if args.two_speakers else 1,
        "dispatch_minimum_interval_seconds": args.interval,
        "vad": compare_vad(samples),
    }
    (args.output / "vad.json").write_text(json.dumps(report["vad"], indent=2), encoding="utf8")
    settings = Settings.from_env()
    records = defaultdict(list)
    started = time.perf_counter()
    next_request = started
    if args.local_int8:
        from nastya_worker.providers.local import LocalSpeechRecognizer

        (args.output / "resource-target.json").write_text(json.dumps({"pid": os.getpid()}))
        init_started = time.perf_counter()
        provider = LocalSpeechRecognizer("turbo", settings.stt_model_directory, "int8_float16")
        report["model_init_with_warmup_ms"] = (time.perf_counter() - init_started) * 1000
        local_records = []
        for sample in samples:
            call_started = time.perf_counter()
            text, error = None, None
            try:
                text = (
                    await provider.transcribe(sample["_path"].read_bytes(), sample["language"])
                ).text
            except InferenceError as exc:
                error = str(exc)
            row = {
                "id": sample["id"],
                "text": text,
                "error": error,
                "responseMs": (time.perf_counter() - call_started) * 1000,
                "queueMs": 0,
            }
            local_records.append(row)
            with (args.output / "synthetic-stt.jsonl").open("a", encoding="utf8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"local-int8 {sample['id']}: {'error' if error else 'ok'}", flush=True)
        report["scope"] = "local warm CUDA batch STT, not RTC end-to-end"
        report["backend_precision"] = "int8_float16"
        report["backend_beam_size"] = 1
        report["results"]["faster-whisper-turbo-int8"] = summarise_stt(local_records, samples)
        report["total_elapsed_seconds"] = time.perf_counter() - started
        (args.output / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf8")
        return
    async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as client:
        providers = {
            m: HttpSpeechRecognizer(client, settings.stt_url, settings.stt_api_key, m)
            for m in args.models
        }
        # Alternate model order for each fixture to reduce order/time confounding.
        requests = []
        for index, sample in enumerate(samples):
            for model in args.models if index % 2 == 0 else list(reversed(args.models)):
                requests.append((sample, model))

        async def request(sample, model, queued):
            call_started = time.perf_counter()
            text, error = None, None
            try:
                transcript = await providers[model].transcribe(
                    sample["_path"].read_bytes(), sample["language"]
                )
                if transcript.language != sample["language"]:
                    raise InferenceError("language mismatch")
                text = transcript.text
            except InferenceError as exc:
                error = str(exc)  # Adapter only emits bounded sanitized codes.
            record = {
                "id": sample["id"],
                "model": model,
                "text": text,
                "error": error,
                "responseMs": (time.perf_counter() - call_started) * 1000,
                "queueMs": (call_started - queued) * 1000,
            }
            records[model].append(record)
            with (args.output / "synthetic-stt.jsonl").open("a", encoding="utf8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(f"{model} {sample['id']}: {'error' if error else 'ok'}", flush=True)

        if args.two_speakers:
            for index in range(0, len(samples), 2):
                for model in args.models:
                    queued = time.perf_counter()
                    await asyncio.sleep(max(0, next_request - queued))
                    pair_started = time.perf_counter()
                    next_request = pair_started + 2 * args.interval
                    await asyncio.gather(
                        *(request(sample, model, queued) for sample in samples[index : index + 2])
                    )
        else:
            for sample, model in requests:
                queued = time.perf_counter()
                await asyncio.sleep(max(0, next_request - queued))
                next_request = time.perf_counter() + args.interval
                await request(sample, model, queued)
    for model in args.models:
        report["results"][model] = summarise_stt(records[model], samples)
    report["total_elapsed_seconds"] = time.perf_counter() - started
    report["completed_requests_per_second_under_account_pacing"] = (
        sum(r["error"] is None for rows in records.values() for r in rows)
        / report["total_elapsed_seconds"]
    )
    (args.output / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument(
        "--models", nargs="+", default=["whisper-large-v3-turbo", "whisper-large-v3"]
    )
    parser.add_argument("--interval", type=float, default=3.25)
    parser.add_argument("--two-speakers", action="store_true")
    parser.add_argument("--local-int8", action="store_true")
    args = parser.parse_args()
    if not 3.2 <= args.interval <= 60 or len(set(args.models)) != len(args.models):
        parser.error("invalid pacing or duplicate model IDs")
    check_models(args.models)
    if len(args.models) < 2 and not args.local_int8:
        parser.error("comparison requires at least two distinct model IDs")
    if args.local_int8 and args.two_speakers:
        parser.error("local native GPU ownership is serial; paired mode is remote only")
    for line in args.env_file.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            if key.startswith("NASTYA_"):
                os.environ.setdefault(key, value)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
