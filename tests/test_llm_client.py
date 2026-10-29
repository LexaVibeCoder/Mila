"""Reasoning-effort transport selection for LLMClient."""

from core.llm.client import LLMClient, _reasoning_kwargs


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
