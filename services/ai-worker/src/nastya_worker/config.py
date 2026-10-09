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
    mt_model: str = ""
    http_timeout_seconds: float = 20.0

    @property
    def api_ready(self) -> bool:
        return bool(self.stt_url and self.mt_url)

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
        return cls(
            log_level=level,
            stt_url=_endpoint(os.getenv("NASTYA_STT_URL", ""), "NASTYA_STT_URL"),
            mt_url=_endpoint(os.getenv("NASTYA_MT_URL", ""), "NASTYA_MT_URL"),
            stt_api_key=os.getenv("NASTYA_STT_API_KEY", ""),
            mt_api_key=os.getenv("NASTYA_MT_API_KEY", ""),
            stt_model=os.getenv("NASTYA_STT_MODEL", "whisper-1"),
            mt_model=os.getenv("NASTYA_MT_MODEL", ""),
            http_timeout_seconds=timeout,
        )
