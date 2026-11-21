"""Dialog replies: plain text split into bubbles on blank lines."""

import json
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


async def test_diary_pipeline_uses_local_write_status(make_pipeline, monkeypatch):
    pipeline, fake = make_pipeline([
        _tool("write_diary", '{"entry":"alpha draft"}', "call-1"),
        _reply("alpha reply"),
    ])
    execute = AsyncMock(return_value={"type": "write_diary", "status": "written", "entry_id": 3})
    monkeypatch.setattr(pipeline, "_execute_single_action", execute)

    result = await pipeline.process_message("hi", [], [], sandbox=True)

    assert result["actions"][0]["status"] == "written"
    assert any("Запись дневника сохранена (ID: 3)" in m["content"] for m in fake.calls[1]["messages"])


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
    failed_request = result["llm_iterations"][-1]
    assert failed_request["iteration"] == iterations
    assert failed_request["duration_ms"] >= 0
    if failure:
        assert "response" not in failed_request
        assert failed_request["context_delta"]["added_messages"][-1]["content"] == "[No results found]"
    if isinstance(failure, LLMRequestError):
        assert failed_request["provider"]["attempts"][0]["http_status"] == 401
    assert result["system_prompt"].startswith("system prompt")
    saved = dict(save_log.await_args.args[0])
    assert bool(saved.pop("traceback")) == (code == "pipeline_error")
    assert saved == result


async def test_trace_records_context_sent_on_each_iteration(make_pipeline, monkeypatch, tmp_path):
    from Mila_observatory.api import pipeline as pipeline_api

    notifications = "[notification] alpha trigger"
    history = [
        {"role": "user", "content": [
            {"type": "text", "text": "alpha history"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA=="}},
        ]},
    ]
    replacement = [{"role": "user", "content": "beta history"}]
    turn = TurnContext("turn-1", "chat-1", "user-1", {"state": "rest"})
    switched = {"state": "dialog", "focus_chat_id": "chat-2"}
    pipeline, fake = make_pipeline([
        _tool("search_memory", '{"query":"alpha"}', "call-1"),
        _tool("enter_dialog", '{"chat_alias":"beta","note":"alpha note"}', "call-2"),
        _reply("beta reply"),
    ])
    monkeypatch.setattr(pipeline, "_build_notification_block", lambda *a, **k: notifications)
    monkeypatch.setattr(pipeline_module, "build_main_llm_system_prompt",
                        lambda *a, presence, **k: "alpha prompt\n" + presence["state"])
    monkeypatch.setattr(pipeline_module, "get_needs_tracker",
                        lambda: SimpleNamespace(apply_action_effect=lambda _: None))
    monkeypatch.setattr(pipeline_module.settings, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(pipeline_api, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(pipeline, "_execute_single_action", AsyncMock(side_effect=[
        {"type": "search_memory", "status": "searched", "context_addon": "alpha fact"},
        {"type": "enter_dialog", "status": "switched", "chat_id": "chat-2",
         "presence": switched, "journal_event": "beta event"},
    ]))

    events = await _collect(pipeline.process_message_stream(
        "alpha trigger", [], history, turn=turn, history_loader=lambda _: replacement,
    ))

    assert events[-1]["type"] == "done"
    log_path, = tmp_path.glob("pipeline_*.json")
    raw_log = log_path.read_text(encoding="utf-8")
    assert "data:image/jpeg;base64" not in raw_log
    log = json.loads(raw_log)
    assert log["turn_id"] == "turn-1"
    assert log["trigger"] == {"kind": "notification", "text": "alpha trigger"}
    assert log["system_prompt"] == fake.calls[0]["system_prompt"]
    assert log["llm_messages"] == [
        {"role": "user", "content": "alpha history\n[фото]"},
        {"role": "system", "content": notifications},
    ]
    first, second, third = log["llm_iterations"]
    assert first["response"]["tool_calls"][0]["id"] == "call-1"
    assert [entry["action_count"] for entry in (first, second, third)] == [1, 1, 0]
    assert second["context_delta"] == {
        "removed_count": 0, "added_messages": fake.calls[1]["messages"][2:],
    }
    assert "system_prompt_diff" not in second
    assert third["active_chat_id"] == "chat-2"
    assert third["context_delta"] == {
        "removed_count": len(fake.calls[1]["messages"]),
        "added_messages": fake.calls[2]["messages"],
    }
    assert "-rest\n+dialog" in third["system_prompt_diff"]
    assert "raw_llm_responses" not in log
    detail = await pipeline_api.get_pipeline_log(log_path.name)
    assert detail.data["raw_llm_responses"][-1]["thoughts"] == "beta reply"


async def test_dialog_switch_message_matches_second_request_and_log(make_pipeline, monkeypatch, tmp_path):
    from Mila_observatory.api import pipeline as pipeline_api

    pipeline, fake = make_pipeline([
        _tool("enter_dialog", '{"chat_alias":"alpha","note":"alpha reason"}', "call-1"),
        _reply("alpha reply"),
    ])
    history = [{"role": "user", "content": "alpha trigger"}]
    turn = TurnContext("turn-1", "chat-1", "user-1", {"state": "rest"})
    monkeypatch.setattr(pipeline, "_build_notification_block", lambda *a, **k: "alpha notification")
    monkeypatch.setattr(pipeline, "_execute_single_action", AsyncMock(return_value={
        "type": "enter_dialog", "status": "switched", "chat_id": "chat-1",
        "journal_event": "alpha switch", "note": "alpha reason",
    }))
    monkeypatch.setattr(pipeline_module, "get_needs_tracker",
                        lambda: SimpleNamespace(apply_action_effect=lambda _: None))
    monkeypatch.setattr(pipeline_module.settings, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(pipeline_api, "LOG_DIR", str(tmp_path))

    events = await _collect(pipeline.process_message_stream(
        "alpha trigger", [], history, turn=turn, history_loader=lambda _: history,
    ))

    assert events[-1]["type"] == "done"
    switch_message = {"role": "system", "content": (
        "Presence switched. Decide in the new state now.\n"
        "[событие] alpha switch\nПричина перехода: alpha reason"
    )}
    assert fake.calls[1]["messages"] == history + [switch_message]
    log_path, = tmp_path.glob("pipeline_*.json")
    log = json.loads(log_path.read_text(encoding="utf-8"))
    detail = await pipeline_api.get_pipeline_log(log_path.name)
    for trace in (log, detail.data):
        first, second = trace["llm_iterations"]
        assert "context_delta" not in first
        assert second["context_delta"] == {"removed_count": 1, "added_messages": [switch_message]}


async def test_closing_proactive_turn_keeps_partial_trace(make_pipeline, monkeypatch):
    pipeline, _ = make_pipeline([_reply("alpha reply")])
    save = AsyncMock()
    monkeypatch.setattr(pipeline, "_save_log", save)
    stream = pipeline.process_message_stream("", [], [], proactive_block="alpha tick")

    assert (await anext(stream))["type"] == "thought"
    await stream.aclose()

    saved = save.await_args.args[0]
    assert saved["error_code"] == "interrupted"
    assert saved["trigger"] == {"kind": "proactive", "text": "alpha tick"}
    assert saved["llm_iterations"][0]["response"]["thoughts"] == "alpha reply"
