"""Opt-in bilingual TTS listening pack and privacy-safe session measurement.

Does not capture participant speech, log recognized text, or contact Edge in CI.
Real end-of-speech -> audible voice timing needs synchronized endpoint clocks.
"""

import argparse
import asyncio
import json
import math
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from nastya_worker.providers.edge_tts import DEFAULT_VOICE
from nastya_worker.tts_config import TtsSettings

SAMPLES_PATH = Path(__file__).resolve().parents[2] / "evaluation" / "voice_quality_v1.json"
DIRECTIONS = frozenset({"ru-vi", "vi-ru"})
OUTCOMES = frozenset(
    {"played", "caption_only", "provider_error", "timeout", "autoplay_blocked", "cancelled"}
)
EVENT_FIELDS = frozenset({
    "sampleId", "direction", "source", "outcome", "queueAgeMs",
    "speechEndUnixMs", "firstPlaybackUnixMs", "clockErrorBoundMs",
})
MAX_TRACE_LINES = 10000
MAX_TRACE_BYTES = 2 * 1024 * 1024


def samples() -> list[dict[str, str]]:
    data = json.loads(SAMPLES_PATH.read_text(encoding="utf-8"))
    if data["version"] != 1 or len(data["samples"]) != 20:
        raise ValueError("the 20-case versioned RU/VI fixture is missing")
    cases: list[dict[str, str]] = data["samples"]
    if (len({case["id"] for case in cases}) != 20 or
            Counter(case["language"] for case in cases) != {"ru": 10, "vi": 10}):
        raise ValueError("expected ten unique cases per language")
    if any(not 1 <= len(case["text"]) <= 500 for case in cases):
        raise ValueError("speech scripts must fit the bounded TTS interface")
    return cases


def _local_output(folder: Path) -> Path:
    target = folder.resolve()
    worker_root = SAMPLES_PATH.parents[1].resolve()
    if target == worker_root or target.is_relative_to(worker_root):
        raise ValueError("choose an output directory outside the repository")
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    return target


def _exclusive_json(path: Path, payload: Any) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    path.chmod(0o600)


def prepare(folder: Path) -> dict:
    target = _local_output(folder)
    cases = samples()
    reviews = [{
        "sampleId": case["id"], "language": case["language"],
        "category": case["category"], "voice": DEFAULT_VOICE[case["language"]],
        "clarity": None, "naturalness": None, "pronunciation": None,
        "issues": [], "listeningDevice": None,
    } for case in cases]
    report = {
        "protocolVersion": 1,
        "type": "human-voice-quality-review",
        "reviewer": None,
        "reviewDate": None,
        "status": "NOT_REVIEWED",
        "scoreScale": "1 poor to 5 excellent; null means not rated",
        "notes": "Review Russian/Vietnamese audio with competent bilingual speakers.",
        "cases": reviews,
    }
    _exclusive_json(target / "voice-review-blank.json", report)
    return {"cases": len(cases), "ru": 10, "vi": 10, "reviewed": 0}


async def live_samples(folder: Path, language: str) -> dict:
    """Only explicitly called with --live and enabled feature flag."""
    settings = TtsSettings.from_env()
    if not settings.enabled:
        raise ValueError("NASTYA_TTS_ENABLED=true is required for network synthesis")
    if language not in ("ru", "vi", "both"):
        raise ValueError("language must be ru, vi, or both")
    output = _local_output(folder)
    synth = settings.build()
    cases = [
        case for case in samples()
        if language == "both" or case["language"] == language
    ]
    results: list[dict] = []
    for case in cases:
        lang = case["language"]
        voice = DEFAULT_VOICE[lang]
        outcome = await synth.synthesize(case["text"], lang, voice, case["id"])
        row: dict[str, Any] = {
            "sampleId": case["id"], "language": lang, "voice": voice,
            "status": "ok" if outcome.audio is not None else outcome.reason,
            "firstAudioLatencyMs": None, "totalLatencyMs": None,
            "audioBytes": 0, "file": None,
        }
        if outcome.audio is not None:
            file_name = case["id"] + ".mp3"
            with (output / file_name).open("xb") as handle:
                handle.write(outcome.audio.audio)
            (output / file_name).chmod(0o600)
            row.update(
                firstAudioLatencyMs=outcome.audio.first_audio_latency_ms,
                totalLatencyMs=outcome.audio.total_latency_ms,
                audioBytes=len(outcome.audio.audio),
                file=file_name,
            )
        results.append(row)
    _exclusive_json(output / "voice-synthesis-results.json", {
        "protocolVersion": 1,
        "source": "optional-real-Edge-service",
        "provider": "edge-read-aloud-unofficial",
        "cases": results, "humanReview": "NOT_REVIEWED",
        "note": "First audio is the Edge client response, NOT end-of-speech to browser playback.",
    })
    return {"attempted": len(results), "succeeded": sum(x["status"] == "ok" for x in results)}



REVIEW_ISSUES = frozenset({
    "clipped", "mispronunciation", "robotic", "odd_stress",
    "wrong_language", "too_fast", "too_slow", "other",
})


def review_summary(value: Any) -> dict:
    """Never equate an unfilled template with a human listening evaluation."""
    if not isinstance(value, dict) or value.get("protocolVersion") != 1:
        raise ValueError("invalid review protocol")
    cases = value.get("cases")
    if not isinstance(cases, list) or len(cases) != 20:
        raise ValueError("human listening review requires all 20 cases")
    fixture = {item["id"]: item for item in samples()}
    seen: set[str] = set()
    by_language = {}
    for case in cases:
        if not isinstance(case, dict) or case.get("sampleId") not in fixture:
            raise ValueError("unknown review sample")
        identity = case["sampleId"]
        if identity in seen or case.get("language") != fixture[identity]["language"]:
            raise ValueError("duplicate review ID or invalid language")
        seen.add(identity)
        for key in ("clarity", "naturalness", "pronunciation"):
            if case.get(key) is not None and (
                type(case[key]) is not int or not 1 <= case[key] <= 5
            ):
                raise ValueError("scores must be integers 1..5 or null")
        issues = case.get("issues")
        if not isinstance(issues, list) or any(
            not isinstance(x, str) or x not in REVIEW_ISSUES for x in issues
        ):
            raise ValueError("use known listening issue codes")
    complete = (
        len(seen) == 20
        and all(all(type(case.get(x)) is int for x in
                    ("clarity", "naturalness", "pronunciation")) for case in cases)
        and isinstance(value.get("reviewer"), str)
        and len(value["reviewer"].strip()) >= 2
        and isinstance(value.get("reviewDate"), str)
        and len(value["reviewDate"]) == 10
    )
    for lang in ("ru", "vi"):
        subset = [c for c in cases if c["language"] == lang]
        by_language[lang] = {
            "cases": len(subset),
            "rated": sum(all(type(c.get(key)) is int for key in
                             ("clarity", "naturalness", "pronunciation")) for c in subset),
            "meanClarity": round(statistics.mean(
                c["clarity"] for c in subset if type(c.get("clarity")) is int
            ), 2) if any(type(c.get("clarity")) is int for c in subset) else None,
            "meanNaturalness": round(statistics.mean(
                c["naturalness"] for c in subset if type(c.get("naturalness")) is int
            ), 2) if any(type(c.get("naturalness")) is int for c in subset) else None,
            "issues": dict(sorted(Counter(
                issue for c in subset for issue in c["issues"]
            ).items())),
        }
    return {
        "protocolVersion": 1, "humanReviewComplete": complete,
        "verifiedBySoftware": False,
        "byLanguage": by_language,
        "note": "Software validates rubric completeness, not reviewer identity or audio quality.",
    }


def _num(value: Any, label: str, max_value: int = 10**15) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= max_value:
        raise ValueError(f"invalid {label} value")
    return float(value)


def validate_event(event: Any) -> dict:
    """Allowlisted measurement metadata only. Never accepts raw speech or PII."""
    if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
        raise ValueError("unknown or missing trace fields")
    if not isinstance(event["sampleId"], str) or not (
        1 <= len(event["sampleId"]) <= 64
    ) or not all(ch.isalnum() or ch in "_-:" for ch in event["sampleId"]):
        raise ValueError("invalid pseudonymous sampleId")
    if event["direction"] not in DIRECTIONS or event["source"] not in (
        "live", "synthetic"
    ) or event["outcome"] not in OUTCOMES:
        raise ValueError("unknown trace direction, provenance or outcome")
    _num(event["queueAgeMs"], "queueAgeMs", 120000)
    _num(event["clockErrorBoundMs"], "clockErrorBoundMs", 60000)
    end = event["speechEndUnixMs"]
    first = event["firstPlaybackUnixMs"]
    if end is not None:
        _num(end, "speechEndUnixMs")
    if first is not None:
        _num(first, "firstPlaybackUnixMs")
    if (event["outcome"] == "played" and (end is None or first is None)) or (
        event["outcome"] != "played" and first is not None
    ):
        raise ValueError("played events need both end and playback timestamps")
    if end is not None and first is not None and (
        first < end or first - end > 120000
    ):
        raise ValueError("invalid end-of-speech -> playback interval")
    return event


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    low = math.floor(index)
    high = math.ceil(index)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (index - low), 2)


def summary(rows: list[dict]) -> dict:
    events = [validate_event(x) for x in rows]
    def report(items: list[dict]) -> dict:
        valid = [e for e in items if e["outcome"] == "played" and
                 e["clockErrorBoundMs"] <= 50 and e["source"] == "live"]
        latencies = [
            float(e["firstPlaybackUnixMs"] - e["speechEndUnixMs"]) for e in valid
        ]
        ages = [float(e["queueAgeMs"]) for e in items]
        errors = sum(e["outcome"] in {"provider_error", "timeout", "autoplay_blocked"}
                     for e in items)
        return {
            "totalEvents": len(items),
            "played": sum(e["outcome"] == "played" for e in items),
            "errors": errors,
            "errorRate": round(errors / len(items), 4) if items else None,
            "queueAgeP50Ms": percentile(ages, .5),
            "queueAgeP95Ms": percentile(ages, .95),
            "endOfSpeechToPlaybackP50Ms": percentile(latencies, .5),
            "endOfSpeechToPlaybackP95Ms": percentile(latencies, .95),
            "validLiveLatencySamples": len(latencies),
            "clockBoundMaxMs": 50,
        }
    distinct = len({e["sampleId"] for e in events})
    accepted = (
        all(e["source"] == "live" and e["clockErrorBoundMs"] <= 50 for e in events)
        and all(
            sum(e["direction"] == direction and e["outcome"] == "played"
                for e in events) >= 20
            for direction in DIRECTIONS
        )
        and distinct >= 20
    )
    return {
        "protocolVersion": 1,
        "provenance": "session metadata supplied externally; no implicit model/RTC measurement",
        "all": report(events),
        "byDirection": {direction: report([
            e for e in events if e["direction"] == direction
        ]) for direction in sorted(DIRECTIONS)},
        "acceptanceReadyForLatencyReview": bool(events) and accepted,
        "reason": "True only with >=20 live playbacks per direction, clock bound <=50 ms, and >=20 IDs. Human review is separate.",
    }


def load_events(path: Path) -> list[dict]:
    if path.stat().st_size > MAX_TRACE_BYTES:
        raise ValueError("trace is too large")
    items = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            if len(items) >= MAX_TRACE_LINES:
                raise ValueError("too many trace events")
            items.append(validate_event(json.loads(line)))
    return items


def main() -> None:
    parser = argparse.ArgumentParser(description="Bilingual voice evaluation, explicit opt-in")
    parser.add_argument("mode", choices=["prepare", "live", "summarize", "review"])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--language", choices=["ru", "vi", "both"], default="both")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--session-jsonl", type=Path)
    parser.add_argument("--review-json", type=Path)
    args = parser.parse_args()
    try:
        if args.mode == "prepare":
            result = prepare(args.output_dir)
        elif args.mode == "live":
            if not args.live:
                parser.error("network speech requires explicit --live")
            result = asyncio.run(live_samples(args.output_dir, args.language))
        elif args.mode == "summarize":
            if args.session_jsonl is None:
                parser.error("summarize requires --session-jsonl")
            result = summary(load_events(args.session_jsonl))
            output = _local_output(args.output_dir) / "voice-session-report.json"
            _exclusive_json(output, result)
        else:
            if args.review_json is None:
                parser.error("review requires --review-json")
            result = review_summary(json.loads(args.review_json.read_text(encoding="utf-8")))
            output = _local_output(args.output_dir) / "voice-review-report.json"
            _exclusive_json(output, result)
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.exit(2, f"Voice evaluation unavailable: {exc}\n")


if __name__ == "__main__":
    main()
