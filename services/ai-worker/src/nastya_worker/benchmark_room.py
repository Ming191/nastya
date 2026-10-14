"""Opt-in synthetic-room metrics; synthetic STT text goes to a separate scoring file."""

import argparse
import asyncio
import ctypes
import json
import os
import time
from pathlib import Path

from nastya_worker.config import Settings
from nastya_worker.livekit_ingest import RoomAudioReceiver, serve_room
from nastya_worker.providers import http
from nastya_worker.providers.local import LocalSpeechRecognizer
from nastya_worker.speaker_pipeline import PROCESSING_UTTERANCE_ID, SpeakerPipeline
from nastya_worker.translation_pipeline import TranslationPipeline

SAMPLE = PROCESSING_UTTERANCE_ID


def resident_bytes():
    if os.name != "nt":
        return None

    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [
            (name, ctypes.c_size_t)
            for name in (
                "peak_working_set",
                "working_set",
                "peak_paged_pool",
                "paged_pool",
                "peak_nonpaged_pool",
                "nonpaged_pool",
                "pagefile",
                "peak_pagefile",
            )
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.windll.kernel32
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    process = kernel.GetCurrentProcess()
    query = ctypes.windll.psapi.GetProcessMemoryInfo
    query.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    if not query(process, ctypes.byref(counters), counters.cb):
        return None
    return counters.working_set


def instrument(path: Path, registry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    def emit(stage, start, **values):
        sample = SAMPLE.get()
        if sample is None:
            return
        event = {
            "sample": sample,
            "fixtureId": registry.segments.get(sample),
            "stage": stage,
            "unixMs": time.time() * 1000,
            "durationMs": (time.perf_counter() - start) * 1000,
            **values,
        }
        event["rssBytes"] = resident_bytes()
        event["processCpuMs"] = time.process_time() * 1000
        with path.open("a", encoding="utf8") as handle:
            handle.write(json.dumps(event) + "\n")

    original_transcribe = http.HttpSpeechRecognizer.transcribe
    original_post = http._post
    original_process = TranslationPipeline.process
    original_publish = TranslationPipeline._publish
    original_enqueue = SpeakerPipeline.enqueue
    original_local = LocalSpeechRecognizer.transcribe
    original_discover = RoomAudioReceiver.discover

    def discover(self):
        result = original_discover(self)
        path.with_suffix(".ready").write_text("ready")
        return result

    async def local_transcribe(self, wav, language):
        start = time.perf_counter()
        try:
            return await original_local(self, wav, language)
        finally:
            emit("stt_local", start)

    def enqueue(self, segment):
        registry.segments[segment.utterance_id] = registry.active.get(segment.speaker)
        mark = SAMPLE.set(segment.utterance_id)
        try:
            result = original_enqueue(self, segment)
            emit(
                "enqueued",
                time.perf_counter(),
                direction=segment.language,
                audioDurationMs=len(segment.pcm) / 32,
                queueDepth=self.queue.qsize(),
                queueDepthTiming="after_enqueue",
                vadEndOffsetMs=segment.end_ms,
            )
        finally:
            SAMPLE.reset(mark)
        return result

    async def transcribe(self, wav, language, **kwargs):
        start = time.perf_counter()
        try:
            return await original_transcribe(self, wav, language, **kwargs)
        finally:
            emit("stt_with_pacing", start)

    async def post(client, endpoint, **kwargs):
        start = time.perf_counter()
        error = None
        try:
            return await original_post(client, endpoint, **kwargs)
        except http.InferenceError as exc:
            cause = exc.__cause__
            error = (
                "http_" + str(cause.response.status_code)
                if hasattr(cause, "response")
                else "inference_error"
            )
            raise
        except asyncio.CancelledError:
            error = "cancelled"
            raise
        finally:
            emit("stt_http" if "files" in kwargs else "mt_http", start, error=error)

    async def process(self, segment, transcript):
        start = time.perf_counter()
        try:
            return await original_process(self, segment, transcript)
        finally:
            emit("caption_process", start)

    async def publish(self, segment, caption, **kwargs):
        start = time.perf_counter()
        result = await original_publish(self, segment, caption, **kwargs)
        emit(
            "final_publish" if caption["isFinal"] else "partial_publish",
            start,
            recipients=result,
            outcome=caption.get("translationState"),
        )
        return result

    # A recognizer call inherits the speaker task's context. Wrap the queue's
    # callback and recognizer per pipeline so measurements retain attribution.
    original_init = SpeakerPipeline.__init__

    def initialize(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        registry.pipelines[self.segmenter.speaker] = self
        recognizer = self.recognizer

        class ScopedRecognizer:
            async def transcribe(_, wav, language):
                # wait_for inherits the explicit dequeue context, independent
                # of audio contents, queue ordering, or concurrent speakers.
                sample = SAMPLE.get()
                mark = SAMPLE.set(sample)
                try:
                    result = await recognizer.transcribe(wav, language)
                    fixture = registry.segments.get(sample)
                    if fixture is not None:
                        with path.with_name("synthetic-transcripts.jsonl").open(
                            "a", encoding="utf8"
                        ) as handle:
                            handle.write(
                                json.dumps(
                                    {
                                        "sample": sample,
                                        "fixtureId": fixture,
                                        "language": language,
                                        "text": result.text,
                                    }
                                )
                                + "\n"
                            )
                    return result
                finally:
                    SAMPLE.reset(mark)

        self.recognizer = ScopedRecognizer()
        original_callback = self.on_transcript

        async def callback(segment, transcript):
            mark = SAMPLE.set(segment.utterance_id)
            try:
                return await original_callback(segment, transcript)
            finally:
                SAMPLE.reset(mark)

        self.on_transcript = callback

    SpeakerPipeline.__init__ = initialize
    SpeakerPipeline.enqueue = enqueue
    http.HttpSpeechRecognizer.transcribe = transcribe
    LocalSpeechRecognizer.transcribe = local_transcribe
    RoomAudioReceiver.discover = discover
    http._post = post
    TranslationPipeline.process = process
    TranslationPipeline._publish = publish


class FixtureRegistry:
    """Explicit fixture boundaries, never timestamp or text-similarity matching."""

    def __init__(self):
        self.active = {}
        self.segments = {}
        self.pipelines = {}

    def command(self, payload):
        speaker = payload.get("speaker")
        if speaker not in {"human:owner", "human:guest"}:
            raise ValueError("invalid speaker")
        pipeline = self.pipelines.get(speaker)
        if pipeline is None or not pipeline.running:
            raise ValueError("microphone_unavailable")
        if pipeline.segmenter.frames:
            raise ValueError("boundary_busy")
        if payload.get("action") == "begin":
            fixture = payload.get("fixtureId")
            if not isinstance(fixture, str) or not fixture.startswith(("vi-", "ru-")):
                raise ValueError("invalid synthetic fixture")
            if speaker in self.active:
                raise ValueError("fixture already active")
            self.active[speaker] = fixture
        elif payload.get("action") == "end":
            if self.active.get(speaker) != payload.get("fixtureId"):
                raise ValueError("fixture mismatch")
            self.active.pop(speaker)
        else:
            raise ValueError("invalid action")


async def benchmark(args):
    registry = FixtureRegistry()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_name("synthetic-transcripts.jsonl").touch(exist_ok=False)
    instrument(args.output, registry)

    async def control(reader, writer):
        try:
            payload = json.loads(await asyncio.wait_for(reader.readline(), 2))
            registry.command(payload)
            result = {"ok": True}
        except (ValueError, TimeoutError) as exc:
            code = str(exc)
            result = {
                "ok": False,
                "code": code
                if code in {"microphone_unavailable", "boundary_busy"}
                else "control_rejected",
            }
        writer.write((json.dumps(result) + "\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(control, "127.0.0.1", 0, limit=4096) as server:
        port = server.sockets[0].getsockname()[1]
        args.output.with_suffix(".control").write_text(str(port))
        await serve_room(args.room, Settings.from_env())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--room", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    for line in args.env_file.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            if key.startswith(("NASTYA_", "LIVEKIT_")):
                os.environ.setdefault(key, value)
    if args.output.exists():
        raise ValueError("use a fresh benchmark output directory")
    asyncio.run(benchmark(args))


if __name__ == "__main__":
    main()
