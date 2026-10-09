"""Non-secret configuration for the worker shell."""

import logging
import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        level = os.getenv("NASTYA_LOG_LEVEL", "INFO").upper()
        if level not in logging.getLevelNamesMapping():
            raise ValueError("NASTYA_LOG_LEVEL must name a Python logging level")
        return cls(log_level=level)
