"""Optional local GPU recognition. Model assets are never put in source control."""

import asyncio
import io
import os
import sys
import threading
from functools import lru_cache
from pathlib import Path

from nastya_worker.providers.http import MAX_WAV_BYTES, InferenceError, _check_language
from nastya_worker.providers.types import Transcript

DLL_HANDLES = []
MODEL_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def load_model(name: str, directory: str, compute_type: str = "float16"):
    model_directory = Path(directory).expanduser().resolve()
    cache_directory = model_directory / "hf-cache"
    # Keep auxiliary download caches beside the configured model assets.
    os.environ.setdefault("HF_HOME", str(cache_directory))
    os.environ.setdefault("HF_HUB_CACHE", str(model_directory))
    os.environ.setdefault("HF_XET_CACHE", str(cache_directory / "xet"))
    if os.name == "nt":
        packages = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
        for library in ("cublas", "cudnn", "cuda_nvrtc"):
            for suffix in ("bin", "lib"):
                location = packages / library / suffix
                if location.is_dir():
                    DLL_HANDLES.append(os.add_dll_directory(str(location)))
                    os.environ["PATH"] = str(location) + os.pathsep + os.environ.get("PATH", "")
    try:
        from faster_whisper import WhisperModel

        model = WhisperModel(
            name,
            device="cuda",
            compute_type=compute_type,
            download_root=str(model_directory),
            cpu_threads=4,
            num_workers=1,
        )
        import numpy as np

        # Initialize CUDA kernels before subscribing to real microphones.
        segments, _ = model.transcribe(
            np.zeros(16000, dtype=np.float32),
            language="vi",
            beam_size=1,
            best_of=1,
            temperature=0,
            condition_on_previous_text=False,
            vad_filter=False,
            without_timestamps=True,
        )
        list(segments)
        return model
    except (ImportError, RuntimeError, ValueError, OSError) as exc:
        raise InferenceError("local STT initialization failed; check model and CUDA setup") from exc


class LocalSpeechRecognizer:
    def __init__(self, model="turbo", directory="models", compute_type="float16"):
        self._model = load_model(model, directory, compute_type)
        self._lock = asyncio.Lock()

    async def transcribe(self, wav: bytes, language: str) -> Transcript:
        _check_language(language)
        if not wav or len(wav) > MAX_WAV_BYTES:
            raise ValueError("WAV audio must contain 1 to 8388608 bytes")

        cancelled = threading.Event()

        def recognize():
            with MODEL_LOCK:
                if cancelled.is_set():
                    raise InferenceError("local STT cancelled")
                return run_model()

        def run_model():
            try:
                segments, _ = self._model.transcribe(
                    io.BytesIO(wav),
                    language=language,
                    beam_size=1,
                    best_of=1,
                    temperature=0,
                    condition_on_previous_text=False,
                    vad_filter=False,
                    word_timestamps=False,
                    without_timestamps=True,
                )
                text = " ".join(segment.text.strip() for segment in segments).strip()
                if len(text) > 4000:
                    raise InferenceError("local STT response text is too long")
                return Transcript(text, language)
            except (RuntimeError, ValueError, OSError) as exc:
                raise InferenceError("local STT inference failed") from exc

        # The underlying CUDA call cannot be interrupted safely. Keep ownership
        # of the model until it finishes, even if the caller expires or leaves.
        async with self._lock:
            task = asyncio.create_task(asyncio.to_thread(recognize))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled.set()
                try:
                    await task
                except Exception:
                    pass
                raise
