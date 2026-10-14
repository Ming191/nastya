"""Configuration validation for future remote model endpoints."""

import logging
import os
from dataclasses import dataclass
from urllib.parse import urlsplit


def _endpoint(value: str, variable: str) -> str:
    if not value:
        return ""
    try:
        url = urlsplit(value)
        hostname = url.hostname
        port = url.port
    except ValueError as exc:
        raise ValueError(f"{variable} has an invalid URL") from exc
    if not hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError(f"{variable} must be a URL without credentials, query, or fragment")
    local = hostname in {"localhost", "127.0.0.1", "::1"}
    if url.scheme != "https" and not (url.scheme == "http" and local):
        raise ValueError(f"{variable} must use HTTPS (HTTP only for localhost)")
    if port == 0:
        raise ValueError(f"{variable} cannot use port 0")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    log_level: str = "INFO"
    stt_url: str = ""
    mt_url: str = ""
    stt_api_key: str = ""
    mt_api_key: str = ""
    stt_model: str = "whisper-1"
    stt_provider: str = "http"
    stt_local_model: str = "turbo"
    stt_model_directory: str = "models"
    stt_local_compute_type: str = "float16"
    mt_model: str = ""
    mt_provider: str = "http"
    mt_reasoning_effort: str = ""
    stt_min_interval_seconds: float = 0
    stt_burst_requests: int = 1
    stt_concurrency: int = 1
    vad_silence_ms: int = 350
    http_timeout_seconds: float = 20.0
    mt_timeout_seconds: float = 8.0

    @property
    def api_ready(self) -> bool:
        return bool((self.stt_url or self.stt_provider == "local") and self.mt_url)

    @classmethod
    def from_env(cls) -> "Settings":
        level = os.getenv("NASTYA_LOG_LEVEL", "INFO").upper()
        if level not in logging.getLevelNamesMapping():
            raise ValueError("NASTYA_LOG_LEVEL must name a Python logging level")
        try:
            timeout = float(os.getenv("NASTYA_HTTP_TIMEOUT_SECONDS", "20"))
        except ValueError as exc:
            raise ValueError("NASTYA_HTTP_TIMEOUT_SECONDS must be numeric") from exc
        if not 0.1 <= timeout <= 120:
            raise ValueError("NASTYA_HTTP_TIMEOUT_SECONDS must be between 0.1 and 120")
        try:
            mt_timeout = float(os.getenv("NASTYA_MT_TIMEOUT_SECONDS", "8"))
        except ValueError as exc:
            raise ValueError("NASTYA_MT_TIMEOUT_SECONDS must be numeric") from exc
        if not 0.1 <= mt_timeout <= 30:
            raise ValueError("NASTYA_MT_TIMEOUT_SECONDS must be between 0.1 and 30")
        provider = os.getenv("NASTYA_MT_PROVIDER", "http")
        stt_provider = os.getenv("NASTYA_STT_PROVIDER", "http")
        if stt_provider not in {"http", "local", "hybrid"}:
            raise ValueError("NASTYA_STT_PROVIDER must be http, local or hybrid")
        compute_type = os.getenv("NASTYA_STT_LOCAL_COMPUTE_TYPE", "float16")
        if compute_type not in {"float16", "int8_float16"}:
            raise ValueError("NASTYA_STT_LOCAL_COMPUTE_TYPE must be float16 or int8_float16")
        if provider not in {"http", "chat"}:
            raise ValueError("NASTYA_MT_PROVIDER must be http or chat")
        effort = os.getenv("NASTYA_MT_REASONING_EFFORT", "")
        if effort not in {"", "none", "low", "medium", "high"}:
            raise ValueError("invalid NASTYA_MT_REASONING_EFFORT")
        try:
            interval = float(os.getenv("NASTYA_STT_MIN_INTERVAL_SECONDS", "0"))
        except ValueError as exc:
            raise ValueError("NASTYA_STT_MIN_INTERVAL_SECONDS must be numeric") from exc
        if not 0 <= interval <= 60:
            raise ValueError("NASTYA_STT_MIN_INTERVAL_SECONDS must be between 0 and 60")
        if provider == "chat" and not os.getenv("NASTYA_MT_MODEL", "").strip():
            raise ValueError("NASTYA_MT_MODEL is required for chat translation")
        integers = {}
        for variable, default, lower, upper in (
            ("NASTYA_STT_BURST_REQUESTS", 1, 1, 2),
            ("NASTYA_STT_CONCURRENCY", 1, 1, 2),
            ("NASTYA_VAD_SILENCE_MS", 350, 100, 1500),
        ):
            try:
                value = int(os.getenv(variable, str(default)))
            except ValueError as exc:
                raise ValueError(f"{variable} must be an integer") from exc
            if not lower <= value <= upper:
                raise ValueError(f"{variable} must be between {lower} and {upper}")
            integers[variable] = value
        return cls(
            log_level=level,
            stt_url=_endpoint(os.getenv("NASTYA_STT_URL", ""), "NASTYA_STT_URL"),
            mt_url=_endpoint(os.getenv("NASTYA_MT_URL", ""), "NASTYA_MT_URL"),
            stt_api_key=os.getenv("NASTYA_STT_API_KEY", ""),
            mt_api_key=os.getenv("NASTYA_MT_API_KEY", ""),
            stt_model=os.getenv("NASTYA_STT_MODEL", "whisper-1"),
            stt_provider=stt_provider,
            stt_local_model=os.getenv("NASTYA_STT_LOCAL_MODEL", "turbo"),
            stt_model_directory=os.getenv("NASTYA_STT_MODEL_DIRECTORY", "models"),
            stt_local_compute_type=compute_type,
            mt_model=os.getenv("NASTYA_MT_MODEL", ""),
            mt_provider=provider,
            mt_reasoning_effort=effort,
            stt_min_interval_seconds=interval,
            stt_burst_requests=integers["NASTYA_STT_BURST_REQUESTS"],
            stt_concurrency=integers["NASTYA_STT_CONCURRENCY"],
            vad_silence_ms=integers["NASTYA_VAD_SILENCE_MS"],
            http_timeout_seconds=timeout,
            mt_timeout_seconds=mt_timeout,
        )
