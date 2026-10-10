"""Independent bounded PCM16 speaker segmentation and STT scheduling.

Energy-based VAD is a provisional heuristic, not a benchmarked model.
No microphone buffers, transcripts, or tokens are persisted/logged.
"""

import asyncio
import io
import math
import sys
import time
import wave
from array import array
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from nastya_worker.providers.types import Language, SpeechRecognizer, Transcript

SAMPLE_RATE = 16000
HUMANS = frozenset({"human:owner", "human:guest"})


@dataclass(frozen=True, slots=True)
class Segment:
    speaker: str
    language: Language
    utterance_id: str
    start_ms: int
    end_ms: int
    pcm: bytes
    generation: int
    created_at: float

    def wav(self) -> bytes:
        output = io.BytesIO()
        with wave.open(output, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(self.pcm)
        return output.getvalue()


def is_human_microphone(identity: str, kind: str, source: str) -> bool:
    return identity in HUMANS and kind == "audio" and source == "microphone"


def source_language(attributes: dict[str, str]) -> Language | None:
    value = attributes.get("sourceLanguage")
    return value if value in ("ru", "vi") else None


def pcm_rms(pcm: bytes) -> float:
    if len(pcm) % 2:
        raise ValueError("PCM16 requires aligned samples")
    if not pcm:
        return 0.0
    values = array("h")
    values.frombytes(pcm)
    if sys.byteorder != "little":
        values.byteswap()
    return math.sqrt(sum(v * v for v in values) / len(values)) / 32768


@dataclass(frozen=True, slots=True)
class VadOptions:
    threshold: float = 0.018
    onset_ms: int = 60
    pre_roll_ms: int = 120
    silence_ms: int = 350
    min_speech_ms: int = 150
    max_segment_ms: int = 6000

    def __post_init__(self) -> None:
        if not 0.001 <= self.threshold <= 0.3:
            raise ValueError("VAD threshold outside 0.001..0.3")
        if not 30 <= self.onset_ms <= 500:
            raise ValueError("VAD onset outside 30..500ms")
        if not 30 <= self.pre_roll_ms <= 400:
            raise ValueError("VAD pre-roll outside 30..400ms")
        if not 100 <= self.silence_ms <= 1500:
            raise ValueError("VAD silence outside 100..1500ms")
        if not 50 <= self.min_speech_ms <= 1500:
            raise ValueError("VAD minimum speech outside 50..1500ms")
        if not 1000 <= self.max_segment_ms <= 10000:
            raise ValueError("VAD max duration outside 1000..10000ms")


class Segmenter:
    """One monotonic audio sample clock per human publication."""

    def __init__(
        self,
        speaker: str,
        language: Language,
        generation: int,
        vad: VadOptions | None = None,
    ):
        if speaker not in HUMANS:
            raise ValueError("not a human role")
        self.speaker, self.language, self.generation = speaker, language, generation
        self.vad = vad or VadOptions()
        self.clock = 0
        self.start = 0
        self.frames: list[bytes] = []
        self.preroll: deque[tuple[int, bytes]] = deque()
        self.onset = 0.0
        self.voiced = 0.0
        self.quiet = 0.0
        self.serial = 0

    def feed(self, pcm: bytes) -> Segment | None:
        if not pcm or len(pcm) % 2 or len(pcm) > SAMPLE_RATE * 2:
            raise ValueError("expected PCM16 frame up to one second")
        frame_start = self.clock
        self.clock += len(pcm) // 2
        duration = len(pcm) / 2 * 1000 / SAMPLE_RATE
        active = pcm_rms(pcm) >= self.vad.threshold

        if not self.frames:
            self.preroll.append((frame_start, pcm))
            self.onset = self.onset + duration if active else 0.0
            while (
                self.preroll
                and (self.clock - self.preroll[0][0]) * 1000 / SAMPLE_RATE > self.vad.pre_roll_ms
            ):
                self.preroll.popleft()
            if self.onset < self.vad.onset_ms:
                return None
            self.start = self.preroll[0][0]
            self.frames = [frame for _, frame in self.preroll]
            self.preroll.clear()
            self.voiced = self.onset
            self.quiet = 0.0
        else:
            self.frames.append(pcm)
            if active:
                self.voiced += duration
                self.quiet = 0
            else:
                self.quiet += duration

        span = (self.clock - self.start) * 1000 / SAMPLE_RATE
        if self.quiet >= self.vad.silence_ms or span >= self.vad.max_segment_ms:
            return self.flush()
        return None

    def flush(self) -> Segment | None:
        if not self.frames:
            return None
        self.serial += 1
        segment = (
            Segment(
                speaker=self.speaker,
                language=self.language,
                utterance_id=f"{self.speaker}:{self.generation}:{self.serial}",
                start_ms=round(self.start * 1000 / SAMPLE_RATE),
                end_ms=round(self.clock * 1000 / SAMPLE_RATE),
                pcm=b"".join(self.frames),
                generation=self.generation,
                created_at=time.monotonic(),
            )
            if self.voiced >= self.vad.min_speech_ms
            else None
        )
        self.reset()
        return segment

    def reset(self) -> None:
        self.frames.clear()
        self.preroll.clear()
        self.onset = self.voiced = self.quiet = 0.0


class SpeakerPipeline:
    """Per-speaker bounded inference queue, with a shared GPU/API semaphore."""

    def __init__(
        self,
        speaker: str,
        language: Language,
        generation: int,
        recognizer: SpeechRecognizer,
        semaphore: asyncio.Semaphore,
        on_transcript: Callable[[Segment, Transcript], Awaitable[None]],
        on_stt_error: Callable[[Segment], Awaitable[None]] | None = None,
        queue_limit: int = 2,
        expiry_seconds: float = 8.0,
        vad: VadOptions | None = None,
    ):
        if not 1 <= queue_limit <= 8 or not 0.5 <= expiry_seconds <= 30:
            raise ValueError("invalid memory or age bound")
        self.segmenter = Segmenter(speaker, language, generation, vad)
        self.recognizer, self.semaphore = recognizer, semaphore
        self.on_transcript = on_transcript
        self.on_stt_error = on_stt_error
        self.queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=queue_limit)
        self.expiry_seconds = expiry_seconds
        self.dropped = self.failures = self.processed = 0
        self.running = True
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        if self.task is not None:
            raise RuntimeError("pipeline already running")
        self.task = asyncio.create_task(self._process())

    def feed(self, pcm: bytes) -> None:
        if not self.running:
            return
        segment = self.segmenter.feed(pcm)
        if segment is not None:
            self.enqueue(segment)

    def enqueue(self, segment: Segment) -> None:
        if not self.running or segment.generation != self.segmenter.generation:
            self.dropped += 1
            return
        if self.queue.full():
            self.queue.get_nowait()
            self.queue.task_done()
            self.dropped += 1
        self.queue.put_nowait(segment)

    async def _process(self) -> None:
        while self.running:
            segment = await self.queue.get()
            try:
                if time.monotonic() - segment.created_at > self.expiry_seconds:
                    self.dropped += 1
                    continue
                async with self.semaphore:
                    if not self.running or (
                        time.monotonic() - segment.created_at > self.expiry_seconds
                    ):
                        self.dropped += 1
                        continue
                    try:
                        transcript = await self.recognizer.transcribe(
                            segment.wav(), segment.language
                        )
                    except Exception:
                        self.failures += 1
                        if self.running and self.on_stt_error:
                            try:
                                await self.on_stt_error(segment)
                            except Exception:
                                pass
                        continue
                # MT and caption publication do not hold the STT semaphore.
                if self.running and time.monotonic() - segment.created_at <= self.expiry_seconds:
                    try:
                        await self.on_transcript(segment, transcript)
                        self.processed += 1
                    except Exception:
                        self.failures += 1
                else:
                    self.dropped += 1
            finally:
                self.queue.task_done()

    async def stop(self) -> None:
        self.running = False
        self.segmenter.reset()
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
            self.dropped += 1
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
