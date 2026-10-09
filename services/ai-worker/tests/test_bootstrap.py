import os
from unittest.mock import patch

import pytest

from nastya_worker.cli import health
from nastya_worker.config import Settings


def test_health_is_explicitly_not_ai_ready() -> None:
    assert health() == {
        "service": "ai-worker",
        "status": "ok",
        "rtcReady": False,
        "modelsReady": False,
    }


def test_default_log_level() -> None:
    with patch.dict(os.environ, {}, clear=True):
        assert Settings.from_env().log_level == "INFO"


def test_invalid_log_level_is_rejected() -> None:
    with patch.dict(os.environ, {"NASTYA_LOG_LEVEL": "NOT_A_LEVEL"}):
        with pytest.raises(ValueError, match="NASTYA_LOG_LEVEL"):
            Settings.from_env()
