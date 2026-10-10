"""Explicit opt-in RU/VI Edge TTS sample experiment. Never runs in normal CI.

Writes synthetic speech clips ONLY when --live and NASTYA_TTS_ENABLED=1.
No user call transcripts, service credentials or private audio are collected.
"""

import argparse
import asyncio
import json
from pathlib import Path

from nastya_worker.providers.edge_tts import DEFAULT_VOICE, VOICES
from nastya_worker.tts_config import TtsSettings

SAMPLES = {
    "ru": (
        "Привет! Как прошёл твой день?",
        "Можешь, пожалуйста, говорить немного медленнее?",
        "Давай созвонимся завтра в семь часов.",
    ),
    "vi": (
        "Chào bạn! Hôm nay bạn thế nào?",
        "Bạn có thể nói chậm hơn một chút được không?",
        "Ngày mai chúng ta gặp nhau lúc bảy giờ nhé.",
    ),
}


async def experiment(language: str, voice: str, folder: Path) -> dict:
    settings = TtsSettings.from_env()
    if not settings.enabled:
        raise ValueError("TTS disabled: explicitly set NASTYA_TTS_ENABLED=true")
    if language not in SAMPLES or voice not in VOICES[language]:
        raise ValueError("unsupported sample language or voice")
    root = (Path(__file__).resolve().parents[2] / "evaluation").resolve()
    output = folder.resolve()
    if output.is_relative_to(root):
        raise ValueError("TTS experiment output must be outside evaluation corpus")
    output.mkdir(parents=True, exist_ok=True)
    synth = settings.build()
    records = []
    for index, text in enumerate(SAMPLES[language], 1):
        uid = f"tts-sample-{language}-{index}"
        response = await synth.synthesize(text, language, voice, uid)
        record = {
            "id": uid,
            "language": language,
            "voice": voice,
            "status": "ok" if response.audio is not None else response.reason,
            "first_audio_latency_ms": None,
            "total_latency_ms": None,
            "bytes": 0,
            "file": None,
        }
        if response.audio is not None:
            speech = response.audio
            name = uid + ".mp3"
            destination = output / name
            with destination.open("xb") as f:
                f.write(speech.audio)
            destination.chmod(0o600)
            record.update(
                first_audio_latency_ms=speech.first_audio_latency_ms,
                total_latency_ms=speech.total_latency_ms,
                bytes=len(speech.audio),
                file=name,
            )
        records.append(record)
    result = {
        "provider": "edge-read-aloud-unofficial",
        "experiment_only": True,
        "samples": records,
        "human_review": "pending",
        "intelligibility_verified": False,
        "privacy": "Only fixed synthetic text; no real conversation data",
        "warning": "Sample output requires a bilingual listener and usage-rights verification",
    }
    report_path = output / ("tts-" + language + "-report.json")
    with report_path.open("x", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    report_path.chmod(0o600)
    return {"report": str(report_path), "success": sum(x["status"] == "ok" for x in records)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Opt-in offline review of Edge TTS experiment")
    parser.add_argument(
        "--live", action="store_true", help="explicitly send synthetic samples to Edge"
    )
    parser.add_argument("--language", choices=("ru", "vi"), required=True)
    parser.add_argument("--voice", default="")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not args.live:
        parser.error("Refusing network TTS without explicit --live")
    try:
        voice = args.voice or DEFAULT_VOICE[args.language]
        print(json.dumps(asyncio.run(experiment(args.language, voice, args.output_dir))))
    except (ValueError, OSError) as exc:
        parser.exit(2, f"TTS experiment unavailable: {exc}\n")


if __name__ == "__main__":
    main()
