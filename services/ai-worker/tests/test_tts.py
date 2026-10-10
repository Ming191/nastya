import asyncio
import json
from types import SimpleNamespace

import pytest

from nastya_worker.providers.edge_tts import (
    DEFAULT_VOICE, MAX_AUDIO_BYTES, EdgeSpeechSynthesizer, VOICES,
)
from nastya_worker.providers.tts import (
    OptionalTts, SpeechSynthesisError, SynthesizedSpeech,
)
from nastya_worker.tts_config import TtsSettings
from nastya_worker.tts_experiment import SAMPLES, experiment


class FakeCommunicate:
    def __init__(self, chunks=(), *, delay=0, failure=None):
        self.chunks, self.delay, self.failure = chunks, delay, failure
        self.closed = False

    async def stream(self):
        try:
            for chunk in self.chunks:
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield chunk
            if self.failure is not None:
                raise self.failure
        finally:
            self.closed = True


def run(value):
    return asyncio.run(value)


def test_ru_vi_voices_and_default_feature_off(monkeypatch):
    monkeypatch.delenv("NASTYA_TTS_ENABLED", raising=False)
    settings = TtsSettings.from_env()
    assert not settings.enabled and settings.provider == "edge"
    assert DEFAULT_VOICE["ru"] in VOICES["ru"]
    assert DEFAULT_VOICE["vi"] in VOICES["vi"]
    assert len(SAMPLES["vi"]) == len(SAMPLES["ru"]) == 3
    result = run(settings.build().synthesize("Xin chào", "vi", DEFAULT_VOICE["vi"], "u-1"))
    assert result.audio is None and result.reason == "disabled"


def test_validate_configuration_and_reject_unknown_provider(monkeypatch):
    monkeypatch.setenv("NASTYA_TTS_ENABLED", "yes")
    with pytest.raises(ValueError, match="true or false"):
        TtsSettings.from_env()
    monkeypatch.setenv("NASTYA_TTS_ENABLED", "true")
    monkeypatch.setenv("NASTYA_TTS_PROVIDER", "other")
    with pytest.raises(ValueError, match="supports edge"):
        TtsSettings.from_env()
    monkeypatch.setenv("NASTYA_TTS_PROVIDER", "edge")
    monkeypatch.setenv("NASTYA_TTS_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValueError, match="0.5..30"):
        TtsSettings.from_env()


def test_synthesizes_mp3_and_measures_actual_first_chunk_delay():
    instance = FakeCommunicate(
        [
            {"type": "WordBoundary", "text": "ignored"},
            {"type": "audio", "data": b"\xff\xfb\x90\x64"},
            {"type": "audio", "data": b"\x00\x00"},
        ], delay=0.015,
    )
    provider = EdgeSpeechSynthesizer(2, communicate_factory=lambda text, voice: instance)
    speech = run(provider.synthesize("Привет", "ru", DEFAULT_VOICE["ru"], "human:owner:1:1"))
    assert speech.audio == b"\xff\xfb\x90\x64\x00\x00"
    assert speech.format == "audio/mpeg"
    assert speech.utterance_id == "human:owner:1:1"
    assert speech.language == "ru"
    assert speech.first_audio_latency_ms >= 15
    assert speech.total_latency_ms >= speech.first_audio_latency_ms
    assert instance.closed


def test_invalid_text_voice_and_utterance_never_reach_external_provider():
    calls = []
    provider = EdgeSpeechSynthesizer(
        communicate_factory=lambda text, voice: calls.append((text, voice))
    )
    for text, language, voice, uid, error in [
        ("", "vi", DEFAULT_VOICE["vi"], "u", "invalid_request"),
        ("x" * 501, "vi", DEFAULT_VOICE["vi"], "u", "invalid_request"),
        ("\x00secret", "vi", DEFAULT_VOICE["vi"], "u", "invalid_request"),
        ("hello", "vi", DEFAULT_VOICE["ru"], "u", "unsupported_voice"),
        ("hello", "vi", DEFAULT_VOICE["vi"], "bad/path", "invalid_request"),
        ("hello", "en", DEFAULT_VOICE["vi"], "u", "invalid_request"),
    ]:
        with pytest.raises(SpeechSynthesisError) as err:
            run(provider.synthesize(text, language, voice, uid))
        assert err.value.code == error
    assert not calls


def test_no_audio_and_oversize_fail_safely():
    provider = EdgeSpeechSynthesizer(
        communicate_factory=lambda text, voice: FakeCommunicate([
            {"type": "SentenceBoundary", "text": "ok"},
        ]),
    )
    with pytest.raises(SpeechSynthesisError) as err:
        run(provider.synthesize("Xin chào", "vi", DEFAULT_VOICE["vi"], "u"))
    assert err.value.code == "invalid_audio"
    provider = EdgeSpeechSynthesizer(
        communicate_factory=lambda text, voice: FakeCommunicate([
            {"type": "audio", "data": bytes(MAX_AUDIO_BYTES + 1)},
        ]),
    )
    with pytest.raises(SpeechSynthesisError) as err:
        run(provider.synthesize("Xin chào", "vi", DEFAULT_VOICE["vi"], "u"))
    assert err.value.code == "invalid_audio"


@pytest.mark.parametrize("status,code", [
    (429, "throttled"), (403, "denied"), (401, "denied"), (503, "unavailable"),
])
def test_provider_status_codes_are_sanitized_and_no_auto_retry(status, code):
    class UpstreamError(Exception):
        def __init__(self):
            self.status = status
            super().__init__("upstream PRIVATE TRANSCRIPT TOKEN=secret")

    calls = []
    def factory(text, voice):
        calls.append(voice)
        return FakeCommunicate(failure=UpstreamError())

    provider = EdgeSpeechSynthesizer(communicate_factory=factory)
    with pytest.raises(SpeechSynthesisError) as err:
        run(provider.synthesize("Привет", "ru", DEFAULT_VOICE["ru"], "u"))
    assert err.value.code == code
    assert "PRIVATE TRANSCRIPT" not in str(err.value)
    assert len(calls) == 1


def test_timeout_closes_async_stream_without_retry():
    fake = FakeCommunicate([{"type": "audio", "data": b"valid"}], delay=0.8)
    provider = EdgeSpeechSynthesizer(.5, communicate_factory=lambda text, voice: fake)
    with pytest.raises(SpeechSynthesisError) as err:
        run(provider.synthesize("Xin chào", "vi", DEFAULT_VOICE["vi"], "u"))
    assert err.value.code == "timeout"
    assert fake.closed


def test_optional_tts_isolation_and_cancel_propagation():
    class Failing:
        async def synthesize(self, *args):
            raise RuntimeError("secret: private speech")
    service = OptionalTts(True, Failing())
    outcome = run(service.synthesize("text", "vi", DEFAULT_VOICE["vi"], "u"))
    assert outcome.audio is None and outcome.reason == "unavailable"

    class Cancelled:
        async def synthesize(self, *args):
            raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        run(OptionalTts(True, Cancelled()).synthesize("text", "vi", "voice", "u"))


def test_opt_in_experiment_writes_only_synthetic_samples(tmp_path, monkeypatch):
    class FakeSynth:
        async def synthesize(self, text, lang, voice, uid):
            return SynthesizedSpeech(uid, lang, voice, "audio/mpeg", b"\xff\xfbtest", 2.5, 5)

    class StubSettings:
        enabled = True
        @classmethod
        def from_env(cls):
            return cls()
        def build(self):
            return OptionalTts(True, FakeSynth())

    monkeypatch.setattr("nastya_worker.tts_experiment.TtsSettings", StubSettings)
    summary = run(experiment("ru", DEFAULT_VOICE["ru"], tmp_path))
    report = json.loads((tmp_path / "tts-ru-report.json").read_text(encoding="utf-8"))
    assert summary["success"] == 3
    assert len(report["samples"]) == 3
    assert report["human_review"] == "pending"
    assert report["intelligibility_verified"] is False
    assert all(row["first_audio_latency_ms"] == 2.5 for row in report["samples"])
    assert all((tmp_path / row["file"]).read_bytes() == b"\xff\xfbtest" for row in report["samples"])
    assert "Привет" not in json.dumps(report, ensure_ascii=False)


def test_experiment_rejects_disabled_and_does_not_create_output(tmp_path, monkeypatch):
    monkeypatch.setenv("NASTYA_TTS_ENABLED", "false")
    with pytest.raises(ValueError, match="explicitly set"):
        run(experiment("vi", DEFAULT_VOICE["vi"], tmp_path / "not_created"))
    assert not (tmp_path / "not_created").exists()
