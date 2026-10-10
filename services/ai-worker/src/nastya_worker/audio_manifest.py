"""Strict opt-in audio fixture manifest validation.

The NAS-8 corpus contains no audio. This manifest always refers to external
licensed/consented fixtures; nothing is recorded from LiveKit or bundled in Git.
"""

import json
import re
import wave
from pathlib import Path

from nastya_worker.evaluation import EvaluationError

MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_DURATION_MS = 30_000


def load_audio_manifest(path: Path) -> list[dict]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvaluationError("audio manifest must be valid JSON") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise EvaluationError("unsupported audio manifest")
    samples = document.get("samples")
    if not isinstance(samples, list) or not samples:
        raise EvaluationError("audio manifest must contain samples")
    if len(samples) > 2000:
        raise EvaluationError("audio manifest has too many samples")
    seen: set[str] = set()
    root = path.parent.resolve()
    results = []
    for sample in samples:
        if not isinstance(sample, dict) or set(sample) != {
            "id",
            "language",
            "audio_path",
            "transcript",
            "speech_segments_ms",
            "provenance",
            "rights_basis",
            "scenario",
        }:
            raise EvaluationError("audio sample has missing or unknown fields")
        uid = sample["id"]
        if not isinstance(uid, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,60}", uid):
            raise EvaluationError("invalid audio sample ID")
        if uid in seen:
            raise EvaluationError("duplicate audio sample ID")
        seen.add(uid)
        if sample["language"] not in ("vi", "ru"):
            raise EvaluationError(f"{uid}: unsupported language")
        if sample["provenance"] not in ("synthetic", "consented", "public_licensed"):
            raise EvaluationError(f"{uid}: unsupported provenance")
        rights = sample["rights_basis"]
        if not isinstance(rights, str) or not 8 <= len(rights) <= 300:
            raise EvaluationError(f"{uid}: rights_basis/consent/license required")
        if not isinstance(sample["transcript"], str) or len(sample["transcript"]) > 2000:
            raise EvaluationError(f"{uid}: invalid transcript")
        if sample["scenario"] not in ("speech", "noise", "interruption", "silence"):
            raise EvaluationError(f"{uid}: invalid scenario")
        filename = sample["audio_path"]
        if not isinstance(filename, str) or not filename.endswith(".wav"):
            raise EvaluationError(f"{uid}: WAV path required")
        relative = Path(filename)
        if relative.is_absolute() or ".." in relative.parts or len(relative.parts) > 8:
            raise EvaluationError(f"{uid}: unsafe WAV path")
        real_path = (root / relative).resolve()
        if not real_path.is_relative_to(root) or not real_path.is_file():
            raise EvaluationError(f"{uid}: WAV must be inside manifest directory")
        if real_path.stat().st_size > MAX_AUDIO_BYTES:
            raise EvaluationError(f"{uid}: audio exceeds size limit")
        try:
            with wave.open(str(real_path), "rb") as reader:
                if reader.getnchannels() != 1 or reader.getsampwidth() != 2:
                    raise EvaluationError(f"{uid}: WAV must be mono PCM16")
                if reader.getframerate() != 16000 or reader.getcomptype() != "NONE":
                    raise EvaluationError(f"{uid}: WAV must be 16 kHz uncompressed PCM")
                duration_ms = round(reader.getnframes() / 16, 2)
        except (wave.Error, EOFError) as exc:
            raise EvaluationError(f"{uid}: unreadable WAV") from exc
        if duration_ms < 100 or duration_ms > MAX_DURATION_MS:
            raise EvaluationError(f"{uid}: WAV duration must be 100 ms to 30 seconds")
        segments = sample["speech_segments_ms"]
        if not isinstance(segments, list):
            raise EvaluationError(f"{uid}: expected speech segment intervals")
        cursor = 0
        for item in segments:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or any(type(v) not in (int, float) for v in item)
                or not (cursor <= item[0] < item[1] <= duration_ms)
            ):
                raise EvaluationError(f"{uid}: bad/overlapping speech intervals")
            cursor = item[1]
        if sample["scenario"] == "silence" and segments:
            raise EvaluationError(f"{uid}: silence has speech segments")
        if sample["scenario"] in ("speech", "interruption") and not segments:
            raise EvaluationError(f"{uid}: annotated speech boundaries required")
        results.append({**sample, "duration_ms": duration_ms, "_path": real_path})
    return results


def boundary_error_ms(expected: list[list], predicted: list[list]) -> dict:
    """Closest onset/offset distances; misses/false positives are explicit."""
    if not expected and not predicted:
        return {"missed_segments": 0, "false_positive_segments": 0, "mean_boundary_error_ms": None}
    if not expected:
        return {
            "missed_segments": 0,
            "false_positive_segments": len(predicted),
            "mean_boundary_error_ms": None,
        }
    if not predicted:
        return {
            "missed_segments": len(expected),
            "false_positive_segments": 0,
            "mean_boundary_error_ms": None,
        }
    # Match only intervals which overlap; do not pair unrelated pauses.
    used: set[int] = set()
    deviations: list[float] = []
    missed = 0
    for left, right in expected:
        possible = [
            (i, segment)
            for i, segment in enumerate(predicted)
            if i not in used and min(right, segment[1]) > max(left, segment[0])
        ]
        if not possible:
            missed += 1
            continue
        index, best = min(
            possible,
            key=lambda p: abs(left - p[1][0]) + abs(right - p[1][1]),
        )
        used.add(index)
        deviations.extend([abs(left - best[0]), abs(right - best[1])])
    return {
        "missed_segments": missed,
        "false_positive_segments": len(predicted) - len(used),
        "mean_boundary_error_ms": round(sum(deviations) / len(deviations), 2)
        if deviations
        else None,
    }


def validate_vad_response(payload: object, duration_ms: float) -> list[list[float]]:
    if not isinstance(payload, dict) or set(payload) != {"segments"}:
        raise EvaluationError("VAD API must return an object with segments")
    segments = payload["segments"]
    if not isinstance(segments, list) or len(segments) > 128:
        raise EvaluationError("VAD API returned invalid segments")
    result = []
    end = 0.0
    for interval in segments:
        if not isinstance(interval, dict) or set(interval) != {"start_ms", "end_ms"}:
            raise EvaluationError("VAD interval must contain start_ms/end_ms")
        start, stop = interval["start_ms"], interval["end_ms"]
        if type(start) not in (int, float) or type(stop) not in (int, float):
            raise EvaluationError("VAD interval must be numeric")
        if not (end <= start < stop <= duration_ms + 20):
            raise EvaluationError("VAD returned unordered/out-of-range segments")
        end = float(stop)
        result.append([float(start), float(stop)])
    return result
