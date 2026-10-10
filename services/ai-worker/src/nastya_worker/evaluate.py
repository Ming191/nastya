"""CLI to validate the synthetic corpus and score RU/VI model predictions.

Example: python -m nastya_worker.evaluate validate
"""

import argparse
import json
from pathlib import Path

from nastya_worker.evaluation import (
    DATASET_DIR,
    EvaluationError,
    load_corpus,
    make_report,
    validate_predictions,
    validate_reviews,
)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    if path.resolve().is_relative_to(DATASET_DIR.resolve()):
        raise EvaluationError("generated files must be written outside the versioned corpus")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in records),
        encoding="utf-8",
    )


def _write_json(path: Path, data: dict) -> None:
    if path.resolve().is_relative_to(DATASET_DIR.resolve()):
        raise EvaluationError("reports must be written outside the versioned corpus")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Synthetic RU/VI translation evaluation; not a translation model"
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DATASET_DIR,
        help="directory with manifest.json and versioned JSONL shards",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("validate", help="validate corpus schema and distribution")
    baseline = subcommands.add_parser(
        "baseline", help="write a SOURCE-COPY control, not a translated output"
    )
    baseline.add_argument("--output", type=Path, required=True)
    scoring = subcommands.add_parser("score", help="evaluate predictions and optional human scores")
    scoring.add_argument("--predictions", type=Path, required=True)
    scoring.add_argument("--reviews", type=Path, default=None)
    scoring.add_argument("--system", required=True, help="human-readable model or control name")
    scoring.add_argument("--output", type=Path, required=True)
    template = subcommands.add_parser(
        "review-template", help="export a blank form for independent human reviewers"
    )
    template.add_argument("--predictions", type=Path, required=True)
    template.add_argument("--reviewer", required=True)
    template.add_argument("--output", type=Path, required=True)
    return parser


def run(args: argparse.Namespace) -> dict:
    manifest, rows = load_corpus(args.dataset)
    if args.command == "validate":
        return {
            "dataset_id": manifest["dataset_id"],
            "review_status": manifest["review_status"],
            "total": len(rows),
            "vi_to_ru": sum(r["source_language"] == "vi" for r in rows),
            "ru_to_vi": sum(r["source_language"] == "ru" for r in rows),
            "categories": len(manifest["categories"]),
            "audio_files": sum(r["audio_path"] is not None for r in rows),
        }
    if args.command == "baseline":
        _write_jsonl(
            args.output,
            [{"id": r["id"], "hypothesis": r["source_text"], "latency_ms": None} for r in rows],
        )
        return {
            "written": len(rows),
            "output": str(args.output),
            "warning": "SOURCE-COPY SANITY CONTROL; NOT A TRANSLATION MODEL",
        }
    predictions = validate_predictions(rows, args.predictions)
    if args.command == "review-template":
        if not args.reviewer.strip() or len(args.reviewer) > 100:
            raise EvaluationError("reviewer identifier must be 1..100 characters")
        _write_jsonl(
            args.output,
            [
                {
                    "id": r["id"],
                    "reviewer": args.reviewer,
                    "meaning": None,
                    "fluency": None,
                    "entities": None,
                    "negation": None,
                    "severity": None,
                    "notes": "",
                }
                for r in rows
                if r["id"] in predictions and predictions[r["id"]]["hypothesis"].strip()
            ],
        )
        return {
            "reviewer": args.reviewer,
            "output": str(args.output),
            "warning": "Complete all score fields before passing to --reviews",
        }
    if args.command == "score":
        reviews = validate_reviews(rows, args.reviews, predictions) if args.reviews else []
        report = make_report(manifest, rows, predictions, reviews, args.system)
        _write_json(args.output, report)
        return {
            "report": str(args.output),
            "prediction_records": len(predictions),
            "human_status": report["human"]["status"],
        }
    raise EvaluationError("unknown evaluation command")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        print(json.dumps(run(args), ensure_ascii=False, sort_keys=True))
    except (EvaluationError, OSError) as exc:
        parser.exit(2, f"evaluation error: {exc}\n")


if __name__ == "__main__":
    main()
