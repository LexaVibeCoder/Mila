"""Dialog response_format: runtime knob + empty-content fallback."""

import pytest

import core.pipeline as pipeline_module
from core.pipeline import ThinkPipeline


class _FakeLLM:
    default_temperature = 0.7

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeRuntime:
    def __init__(self, mode):
        self.mode = mode

    def get(self, key, default=None):
        if key == "llm.response_format":
            return self.mode
        return default


def _reply(content):
    return {"thoughts": content, "tool_calls": None, "finish_reason": "stop",
            "reasoning": ""}


def _empty():
    return {"thoughts": "", "tool_calls": None, "finish_reason": "stop",
            "reasoning": ""}


@pytest.fixture
def make_pipeline(monkeypatch):
    monkeypatch.setattr(pipeline_module, "HAS_OBSERVATORY", True)
    monkeypatch.setattr(pipeline_module, "_get_presence_state",
                        lambda: {"state": "dialog", "focus_chat_id": "chat-1"})
    monkeypatch.setattr(pipeline_module, "build_main_llm_system_prompt",
                        lambda *a, **k: "system prompt")
    monkeypatch.setattr(pipeline_module, "_max_iterations", lambda: 3)

    def build(mode, responses):
        fake = _FakeLLM(responses)
        monkeypatch.setattr(pipeline_module, "runtime_config", _FakeRuntime(mode))
        monkeypatch.setattr(ThinkPipeline, "main_llm", property(lambda self: fake))
        pipeline = ThinkPipeline(None, None, "user-1", "chat-1")
        monkeypatch.setattr(pipeline, "_attach_media",
                            lambda messages, media, default_chat_id=None: messages)
        monkeypatch.setattr(pipeline, "_current_tool_defs",
                            lambda include_proactive=False: [])
        return pipeline, fake

    return build


async def _collect(stream):
    return [event async for event in stream]


@pytest.mark.asyncio
async def test_empty_schema_reply_retries_prompt_only(make_pipeline):
    pipeline, fake = make_pipeline(
        "json_schema",
        [_empty(), _reply('{"messages": [{"content": "привет"}]}')],
    )

    events = await _collect(pipeline._run_stream("hi", [], None,
                                                 request_user_id="user-1",
                                                 request_chat_id="chat-1"))

    assert len(fake.calls) == 2
    assert fake.calls[0]["response_format"]["type"] == "json_schema"
    assert fake.calls[1]["response_format"] is None
    assert events[-1]["kind"] == "reply_bubbles"
    assert events[-1]["texts"] == ["привет"]


@pytest.mark.asyncio
async def test_valid_schema_reply_is_not_retried(make_pipeline):
    pipeline, fake = make_pipeline(
        "json_schema",
        [_reply('{"messages": [{"content": "привет"}]}')],
    )

    events = await _collect(pipeline._run_stream("hi", [], None,
                                                 request_user_id="user-1",
                                                 request_chat_id="chat-1"))

    assert len(fake.calls) == 1
    assert fake.calls[0]["response_format"]["type"] == "json_schema"
    assert events[-1]["kind"] == "reply_bubbles"


@pytest.mark.asyncio
async def test_prompt_mode_sends_no_response_format(make_pipeline):
    pipeline, fake = make_pipeline(
        "prompt",
        [_reply('{"messages": [{"content": "привет"}]}')],
    )

    events = await _collect(pipeline._run_stream("hi", [], None,
                                                 request_user_id="user-1",
                                                 request_chat_id="chat-1"))

    assert len(fake.calls) == 1
    assert fake.calls[0]["response_format"] is None
    assert events[-1]["kind"] == "reply_bubbles"


@pytest.mark.asyncio
async def test_json_object_mode_and_fallback(make_pipeline):
    pipeline, fake = make_pipeline(
        "json_object",
        [_empty(), _reply('{"messages": [{"content": "привет"}]}')],
    )

    await _collect(pipeline._run_stream("hi", [], None,
                                       request_user_id="user-1",
                                       request_chat_id="chat-1"))

    assert fake.calls[0]["response_format"] == {"type": "json_object"}
    assert fake.calls[1]["response_format"] is None


def test_response_format_mode_defaults_to_json_schema(monkeypatch):
    monkeypatch.setattr(pipeline_module, "HAS_OBSERVATORY", True)
    monkeypatch.setattr(pipeline_module, "runtime_config", _FakeRuntime("bogus"))
    assert pipeline_module._response_format_mode() == "json_schema"

    monkeypatch.setattr(pipeline_module, "HAS_OBSERVATORY", False)
    assert pipeline_module._response_format_mode() == "json_schema"


def test_dialog_response_format_builders():
    assert pipeline_module._dialog_response_format("prompt") is None
    assert pipeline_module._dialog_response_format("json_object") == {"type": "json_object"}
    assert pipeline_module._dialog_response_format("json_schema")["type"] == "json_schema"
