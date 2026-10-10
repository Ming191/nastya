"""Low-dependency latency and speech recognition metrics.

Whitespace WER is a token-error proxy for Vietnamese (syllables often use spaces).
Do not use it alone to declare speech quality.
"""

import math
import unicodedata
from collections.abc import Sequence

from nastya_worker.evaluation import percentile


def normalise(text: str) -> str:
    cleaned = "".join(
        " " if unicodedata.category(character).startswith(("P", "S")) else character
        for character in unicodedata.normalize("NFC", text.casefold())
    )
    return " ".join(cleaned.split())


def edit_distance(a: Sequence[str], b: Sequence[str]) -> int:
    previous = list(range(len(b) + 1))
    for i, left in enumerate(a, 1):
        current = [i]
        for j, right in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (left != right)))
        previous = current
    return previous[-1]


def error_counts(reference: str, hypothesis: str) -> dict[str, int]:
    a, b = normalise(reference), normalise(hypothesis)
    reference_tokens, hypothesis_tokens = a.split(), b.split()
    return {
        "word_errors": edit_distance(reference_tokens, hypothesis_tokens),
        "reference_words": len(reference_tokens),
        "character_errors": edit_distance(list(a.replace(" ", "")), list(b.replace(" ", ""))),
        "reference_characters": len(a.replace(" ", "")),
    }


def percentile_stats(values: list[float]) -> dict:
    return {
        "samples": len(values),
        "p50": round(percentile(values, 0.5), 2) if values else None,
        "p95": round(percentile(values, 0.95), 2) if values else None,
    }


def ratio(errors: int, denominator: int) -> float | None:
    return round(errors / denominator, 4) if denominator else None


def finite_nonnegative(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0
