"""Request options and retry behavior for LLMClient."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import openai
import pytest

from core.llm.client import LLMClient, LLMRequestError, _reasoning_kwargs


def test_openai_compatible_uses_flat_reasoning_effort():
    kwargs = _reasoning_kwargs("http://127.0.0.1:1234/v1", "low", None)
    assert kwargs == {"reasoning_effort": "low"}


def test_none_is_sent_explicitly():
    # Important for models that reason by default (e.g. gpt-5.x).
    kwargs = _reasoning_kwargs("https://api.openai.com/v1", "none", None)
    assert kwargs == {"reasoning_effort": "none"}


def test_openrouter_uses_nested_extra_body():
    kwargs = _reasoning_kwargs("https://openrouter.ai/api/v1", "medium", None)
    assert kwargs == {"extra_body": {"reasoning": {"effort": "medium"}}}


def test_openrouter_merges_existing_extra_body():
    kwargs = _reasoning_kwargs(
        "https://openrouter.ai/api/v1", "high",
        {"reasoning": {"exclude": True}, "other": 1},
    )
    assert kwargs["extra_body"] == {
        "reasoning": {"exclude": True, "effort": "high"},
        "other": 1,
    }


def test_extra_body_is_forwarded_without_reasoning():
    kwargs = _reasoning_kwargs(
        "http://127.0.0.1:8080/v1", None,
        {"chat_template_kwargs": {"enable_thinking": True}},
    )
    assert kwargs == {
        "extra_body": {"chat_template_kwargs": {"enable_thinking": True}},
    }


def test_empty_options_produce_no_kwargs():
    assert _reasoning_kwargs("http://127.0.0.1:9/v1", None, {}) == {}
    assert _reasoning_kwargs("http://127.0.0.1:9/v1", "", None) == {}


def test_client_stores_and_reconfigures_reasoning():
    client = LLMClient(
        api_base="http://127.0.0.1:9/v1",
        reasoning_effort="low",
        extra_body={"a": 1},
    )
    assert client.default_reasoning_effort == "low"
    assert client.default_extra_body == {"a": 1}

    client.reconfigure(reasoning_effort="high", extra_body={"b": 2})
    assert client.default_reasoning_effort == "high"
    assert client.default_extra_body == {"b": 2}


def test_runtime_defaults_to_reasoning_none():
    from Mila_observatory.runtime import runtime_config

    assert runtime_config.get("llm.reasoning_effort") == "none"
    assert runtime_config.get("night_llm.reasoning_effort") == "none"


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("reconfigured", [False, True])
async def test_authorization_error_never_retries_even_with_audio(
    status, reconfigured, monkeypatch, caplog, capsys,
):
    client = LLMClient()
    if reconfigured:
        await client.client.close()
        client.reconfigure(api_base="http://127.0.0.1:9/v1")
    assert client.client.max_retries == 0
    request = httpx.Request("POST", "http://127.0.0.1:9/v1/chat/completions")
    failure = openai.APIStatusError(
        "provider-private-detail", body=None,
        response=httpx.Response(status, request=request),
    )
    create = AsyncMock(side_effect=failure)
    monkeypatch.setattr(client.client.chat.completions, "create", create)
    async with client.client:
        with pytest.raises(LLMRequestError, match=f"HTTP {status}") as exc:
            await client.generate([{"role": "user", "content": [
                {"type": "input_audio", "input_audio": {"data": "AA==", "format": "wav"}},
            ]}])

    create.assert_awaited_once()
    assert exc.value.authentication_failed
    assert "provider-private-detail" not in caplog.text + capsys.readouterr().out
    assert any(record.levelname == "ERROR" for record in caplog.records)


@pytest.mark.parametrize("status", [429, 503, None])
async def test_transient_provider_failure_retries_with_delay(status, monkeypatch):
    request = httpx.Request("POST", "http://127.0.0.1:9/v1/chat/completions")
    if status is None:
        failure = openai.APITimeoutError(request)
    else:
        failure = openai.APIStatusError(
            "provider error", body=None,
            response=httpx.Response(status, request=request, headers={"Retry-After": "4"}),
        )
    message = SimpleNamespace(role="assistant", content="alpha reply")
    response = SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])
    create = AsyncMock(side_effect=[failure, response])
    client = LLMClient()
    monkeypatch.setattr(client.client.chat.completions, "create", create)
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)

    async with client.client:
        result = await client.generate([])

    assert result["thoughts"] == "alpha reply"
    assert create.await_count == 2
    sleep.assert_awaited_once()
    minimum = 2 if status is None else 4
    assert minimum <= sleep.await_args.args[0] <= minimum + 0.5
