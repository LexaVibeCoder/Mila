"""Dialog replies: plain text split into bubbles on blank lines."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core.pipeline as pipeline_module
from core.delivery import TurnContext
from core.llm.client import LLMRequestError
from core.pipeline import ThinkPipeline, split_reply_bubbles


class _FakeLLM:
    default_temperature = 0.7

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def generate(self, **kwargs):
        kwargs["messages"] = list(kwargs["messages"])
        self.calls.append(kwargs)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _reply(content):
    return {"thoughts": content, "tool_calls": None, "finish_reason": "stop",
            "reasoning": ""}


def _empty():
    return {"thoughts": "", "tool_calls": None, "finish_reason": "stop",
            "reasoning": ""}


def _tool(name, arguments, call_id):
    call = SimpleNamespace(
        id=call_id, function=SimpleNamespace(name=name, arguments=arguments),
    )
    return {**_reply("internal draft"), "tool_calls": [call], "finish_reason": "tool_calls"}


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
                            lambda include_proactive=False, state=None: [])
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


def test_timestamp_after_single_newline_starts_a_new_bubble():
    raw = "первое\n[2026-09-07 18:39] второе"
    assert split_reply_bubbles(raw) == ["первое", "второе"]


def test_timestamp_only_line_after_single_newline_splits_bubbles():
    raw = "первое\n[2026-09-07 18:39:00]\nвторое"
    assert split_reply_bubbles(raw) == ["первое", "второе"]


def test_timestamp_after_newline_with_leading_spaces_splits_bubbles():
    raw = "первое\n   [2026-09-07 18:39] второе"
    assert split_reply_bubbles(raw) == ["первое", "второе"]


def test_trailing_timestamp_after_newline_is_removed():
    raw = "первое\n[2026-09-07 18:39]"
    assert split_reply_bubbles(raw) == ["первое"]


def test_timestamp_after_crlf_starts_a_new_bubble():
    raw = "первое\r\n[2026-09-07 18:39] второе"
    assert split_reply_bubbles(raw) == ["первое", "второе"]


@pytest.mark.asyncio
async def test_plain_text_reply_is_split_and_sent_as_bubbles(make_pipeline):
    pipeline, fake = make_pipeline([_reply("первое\n\nвторое")])

    events = await _collect(pipeline._run_stream("hi", [], None,
                                                 request_user_id="user-1",
                                                 request_chat_id="chat-1"))

    assert events[-1] == {"kind": "reply_bubbles",
                          "texts": ["первое", "второе"]}
    assert "response_format" not in fake.calls[0]


@pytest.mark.asyncio
async def test_timestamp_marked_reply_is_split_and_sent_as_bubbles(make_pipeline):
    pipeline, fake = make_pipeline([_reply("первое\n[2026-09-07 18:39] второе")])

    events = await _collect(pipeline._run_stream("hi", [], None,
                                                 request_user_id="user-1",
                                                 request_chat_id="chat-1"))

    assert events[-1] == {"kind": "reply_bubbles",
                          "texts": ["первое", "второе"]}


@pytest.mark.asyncio
async def test_no_bubble_limit(make_pipeline):
    raw = "\n\n".join(f"bubble-{i}" for i in range(5))
    pipeline, fake = make_pipeline([_reply(raw)])

    events = await _collect(pipeline._run_stream("hi", [], None,
                                                 request_user_id="user-1",
                                                 request_chat_id="chat-1"))

    assert events[-1]["texts"] == [f"bubble-{i}" for i in range(5)]


@pytest.mark.parametrize("state", ["dialog", "rest", "working"])
async def test_empty_reply_is_silence_without_retry(state, make_pipeline, monkeypatch):
    pipeline, fake = make_pipeline([_empty()])
    monkeypatch.setattr(pipeline, "_build_notification_block", lambda *a, **k: None)
    focus = "chat-1" if state == "dialog" else None
    turn = TurnContext(turn_id="turn-1", chat_id="chat-1", user_id="user-1",
                       presence={"state": state, "focus_chat_id": focus})
    events = await _collect(pipeline._run_stream("hi", [], None,
                                                 request_user_id="user-1",
                                                 request_chat_id="chat-1", turn=turn))

    assert len(fake.calls) == 1
    assert events[-1]["kind"] == "silence"
    assert events[-1]["reason"].startswith("empty reply")


@pytest.mark.parametrize("state", ["rest", "working"])
async def test_out_of_dialog_text_reminds_model_and_uses_iteration_limit(
    state, make_pipeline, monkeypatch,
):
    pipeline, fake = make_pipeline([_reply("internal draft")] * 3)
    monkeypatch.setattr(pipeline, "_build_notification_block", lambda *a, **k: None)
    turn = TurnContext(turn_id="turn-1", chat_id="chat-1", user_id="user-1",
                       presence={"state": state})

    events = await _collect(pipeline.process_message_stream(
        "hi", [], [], sandbox=True, turn=turn))

    reminders = [m["content"] for m in fake.calls[1]["messages"] if m["role"] == "system"]
    assert any("enter_dialog" in message and "send_message" in message for message in reminders)
    assert len(fake.calls) == 3
    assert all(event["type"] not in ("reply", "silence") for event in events)
    assert events[-1]["result"]["error_code"] == "iteration_limit"


@pytest.mark.parametrize("arguments", ["{", "[]", "", "null"])
async def test_invalid_tool_args_prompt_correction_without_delivering_draft(
    arguments, make_pipeline, monkeypatch,
):
    pipeline, fake = make_pipeline([
        _tool("write_diary", arguments, "call-1"),
        _tool("write_diary", '{"text":"alpha entry"}', "call-2"),
        _reply("alpha reply"),
    ])
    execute = AsyncMock(return_value={"type": "write_diary", "status": "written", "entry_id": 1})
    monkeypatch.setattr(pipeline, "_execute_single_action", execute)

    result = await pipeline.process_message("hi", [], [], sandbox=True)

    execute.assert_awaited_once()
    assert execute.await_args.args[0]["params"] == {"text": "alpha entry"}
    assert result["actions"][0]["status"] == "error"
    assert result["reply_texts"] == ["alpha reply"]
    assert any("write_diary не выполнен" in m["content"]
               and "JSON-объектом" in m["content"] for m in fake.calls[1]["messages"])


async def test_diary_match_suggests_updating_existing_id(make_pipeline, monkeypatch):
    pipeline, fake = make_pipeline([
        _tool("write_diary", '{"entry":"alpha draft"}', "call-1"),
        _reply("alpha reply"),
    ])
    execute = AsyncMock(return_value={
        "type": "write_diary", "status": "update_suggested", "entry_id": 3,
        "update_entry_id": 3, "existing_entry": "alpha original",
    })
    monkeypatch.setattr(pipeline, "_execute_single_action", execute)

    result = await pipeline.process_message("hi", [], [], sandbox=True)

    assert result["reply_texts"] == ["alpha reply"]
    assert any("update_diary с ID 3" in message["content"]
               and "полный объединённый текст" in message["content"]
               and '"alpha original"' in message["content"] for message in fake.calls[1]["messages"])
    assert "existing_entry" not in result["actions"][0]


async def test_duplicate_enter_dialog_switches_once_per_turn(make_pipeline, monkeypatch):
    pipeline, fake = make_pipeline([
        _tool("enter_dialog", '{"chat_alias":"alpha","note":"alpha note"}', "call-1"),
        _tool("enter_dialog", '{ "note":"alpha note", "chat_alias":"alpha" }', "call-2"),
        _reply("alpha reply"),
    ])
    turn = TurnContext(turn_id="turn-1", chat_id="chat-1", user_id="user-1",
                       presence={"state": "rest"})
    presence = {"state": "dialog", "focus_chat_id": "chat-1"}
    execute = AsyncMock(return_value={
        "type": "enter_dialog", "status": "switched", "chat_id": "chat-1",
        "presence": presence, "journal_event": "alpha event",
    })
    monkeypatch.setattr(pipeline, "_execute_single_action", execute)
    monkeypatch.setattr(pipeline, "_build_notification_block", lambda *a, **k: None)

    result = await pipeline.process_message("hi", [], [], sandbox=True, turn=turn)

    execute.assert_awaited_once()
    assert turn.presence == presence
    assert len(result["actions"]) == 1
    assert result["reply_texts"] == ["alpha reply"]
    assert any("Повторный вызов enter_dialog" in m["content"]
               for m in fake.calls[2]["messages"])


@pytest.mark.parametrize("failure, code, iterations", [
    (LLMRequestError(401), "llm_authorization", 2),
    (RuntimeError("alpha failure"), "pipeline_error", 2),
    (None, "iteration_limit", 3),
])
async def test_failure_result_and_log_keep_turn_context(
    failure, code, iterations, make_pipeline, monkeypatch,
):
    tool = _tool("search_memory", '{"query":"alpha"}', "call-1")
    pipeline, _ = make_pipeline([tool, failure] if failure else [tool] * 3)
    execute = AsyncMock(return_value={"type": "search_memory", "status": "searched"})
    monkeypatch.setattr(pipeline, "_execute_single_action", execute)
    save_log = AsyncMock()
    monkeypatch.setattr(pipeline, "_save_log", save_log)
    tracker = SimpleNamespace(apply_action_effect=lambda _: None)
    monkeypatch.setattr(pipeline_module, "get_needs_tracker", lambda: tracker)

    events = await _collect(pipeline.process_message_stream("hi", [], []))
    result = events[-1]["result"]

    assert events[-1]["type"] == "error"
    assert all(event["type"] not in ("reply", "silence") for event in events)
    assert result["success"] is False
    assert result["error_code"] == code
    assert result["silence_reason"] == ""
    assert result["reply_texts"] == []
    assert result["reply_chat_id"] == ""
    assert result["actions"] == [{"type": "search_memory", "status": "searched"}]
    assert result["timing"]["iterations"] == iterations
    assert result["raw_llm_responses"][0]["thoughts"] == "internal draft"
    assert result["system_prompt"].startswith("system prompt")
    saved = dict(save_log.await_args.args[0])
    assert bool(saved.pop("traceback")) == (code == "pipeline_error")
    assert saved == result
