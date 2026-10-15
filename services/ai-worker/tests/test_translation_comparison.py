import asyncio
import json
from copy import deepcopy

import httpx
import pytest

from nastya_worker.config import Settings
from nastya_worker.evaluation import EvaluationError, load_corpus
from nastya_worker.translation_comparison import capture_response, compare, model_specs, summarise


def test_chat_comparison_uses_same_120_sources_and_isolates_paired_metadata(tmp_path):
    _, rows = load_corpus()
    seen = []

    async def respond(request):
        body = json.loads(request.content)
        assert body["temperature"] == 0
        assert body["response_format"] == {"type": "json_object"}
        assert len(body["messages"]) == 2
        seen.append((body["model"], body["messages"][1]["content"]))
        await asyncio.sleep(0)
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "usage": {
                    "total_tokens": 17 if body["model"] == "small" else 29,
                    "prompt_time": 0.01,
                },
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps({"translated_text": "synthetic mock output"}),
                            "reasoning": "PRIVATE_REASONING_NOT_TO_SAVE",
                        },
                    }
                ],
            },
        )

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), event_hooks={"response": [capture_response]}
        ) as client:
            return await compare(
                rows,
                [("small", "none"), ("large", "low")],
                client,
                Settings(mt_url="https://model.test/chat"),
                0,
                2,
                tmp_path,
            )

    report = asyncio.run(go())
    assert len(seen) == 240
    for model, token_count in (("small", 17), ("large", 29)):
        assert {text for name, text in seen if name == model} == {r["source_text"] for r in rows}
        result = report["results"][model]
        assert result["scheduled"] == result["succeeded"] == 120
        assert result["tokens"]["total_tokens"] == {"measured": 120, "total": 120 * token_count}
        assert result["reported_model_counts"] == {model: 120}
        assert result["by_direction"]["ru"]["succeeded"] == 60
        assert result["by_direction"]["vi"]["response_ms"]["n"] == 60
        assert result["human_quality"] is None
        assert result["peak_gpu_vram_mib"] is None
    assert all(
        "PRIVATE_REASONING" not in path.read_text(encoding="utf8") for path in tmp_path.iterdir()
    )


def test_failed_comparison_does_not_invent_latency_tokens_or_quality():
    _, rows = load_corpus()
    records = [
        {
            "id": r["id"],
            "error": "INFERENCE_FAILED",
            "hypothesis": "",
            "response_ms": 10,
            "queue_ms": 0,
        }
        for r in rows
    ]
    result = summarise(records, rows)
    assert result["scheduled"] == result["failed"] == 120
    assert result["succeeded"] == 0
    assert result["response_ms"]["median"] is None
    assert result["failed_response_ms"]["n"] == 120
    assert result["tokens"]["total_tokens"] == {"measured": 0, "total": None}
    assert result["human_quality"] is None
    assert result["automatic_diagnostics"]["character_overlap_f1_proxy"] is None


def test_summary_rejects_missing_duplicate_or_empty_success():
    _, rows = load_corpus()
    records = [
        {"id": r["id"], "error": None, "hypothesis": "test", "response_ms": 0, "queue_ms": 0}
        for r in rows
    ]
    assert summarise(records, rows)["response_ms"]["median"] == 0
    with pytest.raises(EvaluationError):
        summarise(records[:-1], rows)
    duplicate = deepcopy(records)
    duplicate[-1] = duplicate[0]
    with pytest.raises(EvaluationError):
        summarise(duplicate, rows)
    records[-1]["hypothesis"] = ""
    with pytest.raises(EvaluationError):
        summarise(records, rows)


@pytest.mark.parametrize(
    "values", [["a=none"], ["a=none", "a=low"], ["a=invalid", "b=low"], ["a", "b"]]
)
def test_invalid_model_specs(values):
    with pytest.raises(EvaluationError):
        model_specs(values)


def test_http_error_is_not_a_success_or_private_body_in_output(tmp_path):
    _, rows = load_corpus()
    pair = [next(r for r in rows if r["source_language"] == lang) for lang in ("vi", "ru")]

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    429, json={"error": "PRIVATE_PROVIDER_BODY"}, headers={"retry-after": "2"}
                )
            ),
            event_hooks={"response": [capture_response]},
        ) as client:
            return await compare(
                pair,
                [("a", "none"), ("b", "low")],
                client,
                Settings(mt_url="https://model.test/chat"),
                0,
                2,
                tmp_path,
            )

    result = asyncio.run(go())
    assert result["results"]["a"]["failed"] == 2
    assert result["paced_successes_per_second"] == 0
    assert all(
        "PRIVATE_PROVIDER_BODY" not in path.read_text(encoding="utf8")
        for path in tmp_path.iterdir()
    )
