"""Summarize opt-in synthetic caption benchmarks without inventing missing metrics."""

import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

from nastya_worker.benchmark_metrics import error_counts, normalise, ratio
from nastya_worker.evaluation import overlap_f1


def summary(values):
    values = sorted(x for x in values if x is not None and math.isfinite(x))
    if not values:
        return dict(n=0, median=None, mean=None, p95=None, min=None, max=None)
    return {
        "n": len(values),
        "median": round(statistics.median(values), 2),
        "mean": round(statistics.mean(values), 2),
        "p95": round(values[math.ceil(len(values) * 0.95) - 1], 2),
        "min": round(values[0], 2),
        "max": round(values[-1], 2),
    }


def canonical_numbers(text):
    mapping = {
        "chín": "9",
        "девять": "9",
        "ba": "3",
        "три": "3",
        "hai": "2",
        "два": "2",
        "две": "2",
        "năm": "5",
        "пять": "5",
    }
    return " ".join(mapping.get(word, word) for word in normalise(text).replace("ё", "е").split())


def analyse(directory: Path, expected: int):
    report = json.loads((directory / "browser-results.json").read_text(encoding="utf8"))
    samples = report["samples"]
    if len(samples) != expected or len({s["id"] for s in samples}) != expected:
        raise ValueError("benchmark row count is incomplete or contains duplicates")
    if report["errors"]:
        raise ValueError("benchmark has execution errors")
    events = [
        json.loads(line) for line in (directory / "worker-events.jsonl").read_text().splitlines()
    ]
    grouped = defaultdict(dict)
    for event in events:
        grouped[event["sample"]][event["stage"]] = event
    stages = defaultdict(list)
    # API stage summaries cover every observed call, including cancelled calls.
    for event in events:
        if event["stage"] in {"stt_http", "stt_local", "mt_http"}:
            stages[event["stage"]].append(event["durationMs"])
    for data in grouped.values():
        if "stt_with_pacing" in data:
            stt = data["stt_with_pacing"]
            queue = stt["unixMs"] - stt["durationMs"] - data["enqueued"]["unixMs"]
            if queue >= 0:
                stages["queue_wait"].append(queue)
            if "stt_http" in data:
                stages["pacing_and_adapter"].append(
                    max(0, stt["durationMs"] - data["stt_http"]["durationMs"])
                )
        elif "stt_local" in data:
            stt = data["stt_local"]
            queue = stt["unixMs"] - stt["durationMs"] - data["enqueued"]["unixMs"]
            if queue >= 0:
                stages["queue_wait"].append(queue)
    outcomes = []
    for sample in samples:
        language = "ru" if sample["direction"] == "vi-ru" else "vi"
        success = sample["finalLanguage"] == language
        # Older harness versions called a source-only final a success. Normalize
        # using the rendered language, never its previously stored boolean.
        sample["success"] = success
        outcomes.append(success)
        role = "human:owner:" if language == "ru" else "human:guest:"
        eligible = [
            data
            for uid, data in grouped.items()
            if uid.startswith(role)
            and sample["startUnixMs"]
            <= data["enqueued"]["unixMs"]
            <= sample["speechEndUnixMs"] + 1500
        ]
        if eligible:
            data = max(eligible, key=lambda d: d["enqueued"]["unixMs"])
            vad = data["enqueued"]["unixMs"] - sample["speechEndUnixMs"]
            if vad >= 0:
                stages["capture_ingress_vad"].append(vad)
            if data.get("final_publish") and sample["finalUnixMs"] is not None:
                final = data["final_publish"]
                transport = sample["finalUnixMs"] - (final["unixMs"] - final["durationMs"])
                if transport >= 0:
                    stages["publish_to_dom"].append(transport)
    result = {
        "profile": report["profile"],
        "samples": len(samples),
        "translated": sum(outcomes),
        "not_translated": len(samples) - sum(outcomes),
        "translated_rate": ratio(sum(outcomes), len(samples)),
        "speech_end_to_any_final_ms": summary([s["speechEndToFinalMs"] for s in samples]),
        "speech_end_to_translated_final_ms": summary(
            [s["speechEndToFinalMs"] for s in samples if s["success"]]
        ),
        "clock_check_ms": summary([s.get("clockErrorBoundMs") for s in samples]),
        "stages_ms": {stage: summary(values) for stage, values in stages.items()},
        "maximum_queue_depth": max(
            (
                e["queueDepth"]
                if e.get("queueDepthTiming") == "after_enqueue"
                else min(2, e["queueDepth"] + 1)
                for e in events
                if "queueDepth" in e
            ),
            default=None,
        ),
        "segmented_utterances": len(grouped),
        "segments_without_final": sum("final_publish" not in data for data in grouped.values()),
        "human_quality_score": None,
        "remote_gpu_vram_bytes": None,
        "directions": {},
    }
    for direction in ("vi-ru", "ru-vi"):
        selected = [s for s in samples if s["direction"] == direction]
        counts = [error_counts(s["sourceReference"], s["recognizedText"]) for s in selected]
        canonical = [
            error_counts(
                canonical_numbers(s["sourceReference"]), canonical_numbers(s["recognizedText"])
            )
            for s in selected
        ]
        result["directions"][direction] = {
            "samples": len(selected),
            "translated": sum(s["success"] for s in selected),
            "latency_ms": summary([s["speechEndToFinalMs"] for s in selected]),
            "wer": ratio(
                sum(c["word_errors"] for c in counts), sum(c["reference_words"] for c in counts)
            ),
            "cer": ratio(
                sum(c["character_errors"] for c in counts),
                sum(c["reference_characters"] for c in counts),
            ),
            "number_canonicalized_wer": ratio(
                sum(c["word_errors"] for c in canonical),
                sum(c["reference_words"] for c in canonical),
            ),
            "translation_character_overlap_f1_proxy": summary(
                [overlap_f1(s["reference"], s["translatedText"]) for s in selected if s["success"]]
            ),
        }
    rss = [e["rssBytes"] for e in events if e.get("rssBytes") is not None]
    result["maximum_sampled_rss_bytes"] = max(rss) if rss else None
    cpu = [e for e in events if "processCpuMs" in e]
    result["average_cpu_percent_of_one_core"] = (
        round(
            (cpu[-1]["processCpuMs"] - cpu[0]["processCpuMs"])
            / (cpu[-1]["unixMs"] - cpu[0]["unixMs"])
            * 100,
            2,
        )
        if len(cpu) > 1 and cpu[-1]["unixMs"] > cpu[0]["unixMs"]
        else None
    )
    gpu = report.get("gpuSamples", [])
    result["whole_gpu_baseline_used_mib"] = gpu[0]["usedMiB"] if gpu else None
    result["maximum_sampled_whole_gpu_used_mib"] = max((g["usedMiB"] for g in gpu), default=None)
    result["gpu_measurement_scope"] = "whole device, includes other applications"
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--expected", type=int, required=True)
    args = parser.parse_args()
    result = analyse(args.directory, args.expected)
    (args.directory / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
