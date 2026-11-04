"""Dialog replies: plain text split into bubbles on blank lines."""

import pytest

import core.pipeline as pipeline_module
from core.pipeline import ThinkPipeline, split_reply_bubbles


class _FakeLLM:
    default_temperature = 0.7

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


def _reply(content):
    return {"thoughts": content, "tool_calls": None, "finish_reason": "stop",
            "reasoning": ""}


def _empty():
    return {"thoughts": "", "tool_calls": None, "finish_reason": "stop",
            "reasoning": ""}


@pytest.fixture
def make_pipeline(monkeypatch):
    monkeypatch.setattr(pipeline_module, "_get_presence_state",
                        lambda: {"state": "dialog", "focus_chat_id": "chat-1"})
    monkeypatch.setattr(pipeline_module, "build_main_llm_system_prompt",
                        lambda *a, **k: "system prompt")
    monkeypatch.setattr(pipeline_module, "_max_iterations", lambda: 3)

    def build(responses):
        fake = _FakeLLM(responses)
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


def test_single_paragraph_stays_one_bubble():
    assert split_reply_bubbles("привет\nкак дела") == ["привет\nкак дела"]


def test_blank_line_splits_bubbles():
    assert split_reply_bubbles("первое\n\nвторое") == ["первое", "второе"]


def test_multiple_blank_lines_and_spaces_split_bubbles():
    raw = "  первое \n \n\n второе \n\n\n третье "
    assert split_reply_bubbles(raw) == ["первое", "второе", "третье"]


def test_crlf_blank_line_splits_bubbles():
    assert split_reply_bubbles("первое\r\n\r\nвторое") == ["первое", "второе"]


def test_empty_and_whitespace_replies_have_no_bubbles():
    assert split_reply_bubbles("") == []
    assert split_reply_bubbles("  \n\n  ") == []


def test_leading_timestamp_is_stripped_from_each_bubble():
    raw = "[2026-09-07 18:38] первое\n\n[2026-09-07 18:39] второе"
    assert split_reply_bubbles(raw) == ["первое", "второе"]


@pytest.mark.asyncio
async def test_plain_text_reply_is_split_and_sent_as_bubbles(make_pipeline):
    pipeline, fake = make_pipeline([_reply("первое\n\nвторое")])

    events = await _collect(pipeline._run_stream("hi", [], None,
                                                 request_user_id="user-1",
                                                 request_chat_id="chat-1"))

    assert events[-1] == {"kind": "reply_bubbles",
                          "texts": ["первое", "второе"],
                          "target_chat_id": "chat-1"}
    assert "response_format" not in fake.calls[0]


@pytest.mark.asyncio
async def test_no_bubble_limit(make_pipeline):
    raw = "\n\n".join(f"bubble-{i}" for i in range(5))
    pipeline, fake = make_pipeline([_reply(raw)])

    events = await _collect(pipeline._run_stream("hi", [], None,
                                                 request_user_id="user-1",
                                                 request_chat_id="chat-1"))

    assert events[-1]["texts"] == [f"bubble-{i}" for i in range(5)]


@pytest.mark.asyncio
async def test_empty_reply_is_silence_without_retry(make_pipeline):
    pipeline, fake = make_pipeline([_empty()])

    events = await _collect(pipeline._run_stream("hi", [], None,
                                                 request_user_id="user-1",
                                                 request_chat_id="chat-1"))

    assert len(fake.calls) == 1
    assert events[-1]["kind"] == "silence"
    assert events[-1]["reason"] == "empty reply"
