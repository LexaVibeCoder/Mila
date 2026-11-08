"""Observatory test chat: sandbox rules, session identity and test diary."""

from types import SimpleNamespace

import pytest

import core.pipeline as pipeline_module
from core.delivery import TurnContext
from core.pipeline import ThinkPipeline
from core.test_chat import store
from core.test_chat.context import SandboxContext
from core.tools.executor import ToolExecutor, get_tool_definitions


def _context(session: dict) -> SandboxContext:
    return SandboxContext(
        session_id=session["session_id"],
        chat_id=session["chat_id"],
        alias=session["alias"],
        user_name=session["user_name"],
        user_id=session["user_id"],
        state=session["state"],
    )


def _executor(session: dict, sandbox: bool = True) -> ToolExecutor:
    return ToolExecutor(
        text_memory=None,
        embedding_client=None,
        current_user_id=session["user_id"],
        chat_id=session["chat_id"],
        sandbox=sandbox,
        sandbox_context=_context(session),
    )


def test_dialog_tools_stay_normal_in_sandbox():
    names = [d["function"]["name"] for d in get_tool_definitions("dialog")]
    assert "search_memory" in names
    assert "write_diary" in names
    assert "update_diary" in names
    assert "enter_dialog" in names
    assert "send_message" in names
    assert "start_sleep" in names


def test_session_identity_is_derived_from_name():
    session = store.create_session(title="scenario", user_name="Алекс")
    sid = session["session_id"]
    assert session["user_name"] == "Алекс"
    assert session["user_id"] == "test-user:алекс"
    assert session["alias"] == "алекс"
    assert session["state"] == "dialog"

    updated = store.update_session(sid, user_name="Мария Петрова", state="rest")
    assert updated["user_name"] == "Мария Петрова"
    assert updated["user_id"] == "test-user:мария-петрова"
    assert updated["alias"] == "мария-петрова"
    assert updated["state"] == "rest"

    store.delete_session(sid)


def test_store_diary_roundtrip_and_session_delete():
    session = store.create_session(user_name="Алекс")
    sid = session["session_id"]

    first = store.add_diary_entry(sid, "alpha event", "day_entries")
    second = store.add_diary_entry(sid, "beta feeling", "reflection_entries")
    assert first and second
    assert store.get_diary_context(sid).count("[ID:") == 2

    assert store.update_diary_entry(sid, first["id"], "alpha updated") is True
    entries = store.get_diary_entries(sid)
    assert entries[0]["content"] == "alpha updated"

    assert store.delete_diary_entry(sid, second["id"]) is True
    assert len(store.get_diary_entries(sid)) == 1

    assert store.delete_session(sid) is True
    assert store.get_diary_entries(sid) == []


@pytest.mark.asyncio
async def test_sandbox_diary_writes_only_session_diary(monkeypatch):
    import core.diary.manager as real_diary

    monkeypatch.setattr(real_diary, "add_diary_entry",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("real diary")))
    monkeypatch.setattr(real_diary, "update_diary_entry",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("real diary")))

    session = store.create_session(user_name="Алекс")
    sid = session["session_id"]
    executor = _executor(session)

    written = await executor.execute_action({
        "action_type": "write_diary",
        "params": {"entry": "alpha event", "category": "day_entries"},
    })
    assert written["status"] == "written"
    entries = store.get_diary_entries(sid)
    assert len(entries) == 1
    assert entries[0]["content"] == "alpha event"

    updated = await executor.execute_action({
        "action_type": "update_diary",
        "params": {"entry": "alpha updated", "entry_id": str(entries[0]["id"])},
    })
    assert updated["status"] == "updated"
    assert store.get_diary_entries(sid)[0]["content"] == "alpha updated"

    store.delete_session(sid)


@pytest.mark.asyncio
async def test_sandbox_without_context_never_touches_real_state(monkeypatch):
    import core.diary.manager as real_diary
    import core.presence as real_presence

    monkeypatch.setattr(real_diary, "add_diary_entry",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("real diary")))
    monkeypatch.setattr(real_diary, "update_diary_entry",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("real diary")))
    monkeypatch.setattr(real_presence, "enter_rest",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("real presence")))

    executor = ToolExecutor(
        text_memory=None, embedding_client=None,
        current_user_id="test-user:алекс", chat_id="990000000001",
        sandbox=True,
    )
    for action in (
        {"action_type": "write_diary", "params": {"entry": "alpha"}},
        {"action_type": "update_diary", "params": {"entry": "alpha", "entry_id": "1"}},
        {"action_type": "enter_rest", "params": {}},
        {"action_type": "send_message", "params": {"text": "hello"}},
    ):
        result = await executor.execute_action(action)
        assert result["status"] == "ignored", action


@pytest.mark.asyncio
async def test_sandbox_presence_switches_session_state_only():
    session = store.create_session(user_name="Алекс")
    sid = session["session_id"]
    ctx = _context(session)
    executor = ToolExecutor(
        text_memory=None, embedding_client=None,
        current_user_id=session["user_id"], chat_id=session["chat_id"],
        sandbox=True, sandbox_context=ctx,
    )

    rest = await executor.execute_action({"action_type": "enter_rest", "params": {}})
    assert rest["status"] == "switched"
    assert rest["presence"]["state"] == "rest"
    assert store.get_session(sid)["state"] == "rest"
    assert ctx.state == "rest"

    dialog = await executor.execute_action({
        "action_type": "enter_dialog",
        "params": {"chat_alias": session["alias"], "note": "хочу поговорить"},
    })
    assert dialog["status"] == "switched"
    assert dialog["presence"]["focus_chat_id"] == session["chat_id"]
    assert dialog["presence"]["chat_alias"] == session["alias"]
    assert store.get_session(sid)["state"] == "dialog"

    # A real/unknown chat does not exist here: silently ignored, state kept.
    ignored = await executor.execute_action({
        "action_type": "enter_dialog",
        "params": {"chat_alias": "real-chat", "note": "x"},
    })
    assert ignored["status"] == "ignored"
    assert store.get_session(sid)["state"] == "dialog"

    store.delete_session(sid)


@pytest.mark.asyncio
async def test_sandbox_send_message_and_unused_tools():
    session = store.create_session(user_name="Алекс")
    sid = session["session_id"]
    executor = _executor(session)

    own = await executor.execute_action({
        "action_type": "send_message",
        "params": {"text": "hello", "chat_alias": session["alias"]},
    })
    assert own == {"type": "send_message", "text": "hello",
                   "chat_id": session["chat_id"], "status": "ready"}

    default = await executor.execute_action({
        "action_type": "send_message", "params": {"text": "hello"},
    })
    assert default["status"] == "ready"

    foreign = await executor.execute_action({
        "action_type": "send_message",
        "params": {"text": "hello", "chat_alias": "real-chat"},
    })
    assert foreign["status"] == "ignored"

    for action in ("start_sleep", "peek_recent_chats", "mystery_tool"):
        result = await executor.execute_action(
            {"action_type": action, "params": {}})
        assert result["status"] == "ignored", action

    store.delete_session(sid)


@pytest.mark.asyncio
async def test_sandbox_search_hides_legacy_null_user_rows(monkeypatch):
    import core.db as db

    monkeypatch.setattr(
        db, "search_memories_like",
        lambda user_id, query, limit=5: (
            [] if user_id else
            [{"mem_id": "mem-1", "content": "legacy fact", "user_id": None}]
        ),
    )

    sandbox = ToolExecutor(
        text_memory=None, embedding_client=None,
        current_user_id=store.user_id_for_name("Алекс"),
        chat_id="990000000001", sandbox=True,
    )
    result = sandbox._sqlite_fallback("legacy", "test")
    assert result["results"] == []

    normal = ToolExecutor(
        text_memory=None, embedding_client=None,
        current_user_id="user-1", chat_id="chat-1", sandbox=False,
    )
    result = normal._sqlite_fallback("legacy", "test")
    assert result["results"][0]["content"] == "legacy fact"


class _FakeLLM:
    default_temperature = 0.7

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


def _reply(text):
    return {"thoughts": text, "tool_calls": None, "finish_reason": "stop",
            "reasoning": ""}


def _tool_call(name, arguments):
    return SimpleNamespace(
        id=f"call-{name}",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _sandbox_snapshot() -> dict:
    return {
        "state": "dialog",
        "focus_chat_id": "990000000001",
        "focus_user_name": "Алекс",
        "focus_chat_alias": "алекс",
        "chat_alias": "алекс",
        "note": "",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_sandbox_turn_skips_log_needs_and_global_presence(monkeypatch):
    fake = _FakeLLM([_reply("hello")])
    monkeypatch.setattr(ThinkPipeline, "main_llm", property(lambda self: fake))
    monkeypatch.setattr(pipeline_module, "_max_iterations", lambda: 3)

    captured = {}

    def fake_prompt(*args, **kwargs):
        captured.update(kwargs)
        return "system"

    monkeypatch.setattr(pipeline_module, "build_main_llm_system_prompt", fake_prompt)

    # Any global presence read means the turn escaped its session snapshot.
    monkeypatch.setattr(pipeline_module, "_get_presence_state",
                        lambda: (_ for _ in ()).throw(AssertionError("global presence read")))

    pipeline = ThinkPipeline(None, None, "test-user:алекс", "990000000001")
    monkeypatch.setattr(pipeline, "_attach_media",
                        lambda messages, media, default_chat_id=None: messages)

    log_calls = []
    monkeypatch.setattr(pipeline, "_save_log",
                        lambda *a, **k: log_calls.append(a) or _noop())

    needs_calls = []
    monkeypatch.setattr(pipeline_module, "get_needs_tracker", lambda: SimpleNamespace(
        apply_action_effect=lambda action: needs_calls.append(action)))

    turn = TurnContext(
        turn_id="turn-test",
        chat_id="990000000001",
        user_id="test-user:алекс",
        presence=_sandbox_snapshot(),
    )
    ctx = SandboxContext(
        session_id="session-1", chat_id="990000000001", alias="алекс",
        user_name="Алекс", user_id="test-user:алекс", state="dialog",
    )

    result = await pipeline.process_message(
        user_message="hi",
        user_visible_history=[],
        agent_internal_history=[],
        request_user_id="test-user:алекс",
        request_chat_id="990000000001",
        turn=turn,
        sandbox=True,
        sandbox_context=ctx,
        sandbox_diary_context="[ID: 1] alpha",
    )

    assert result["success"] is True
    assert result["reply_texts"] == ["hello"]
    assert captured.get("diary_context_override") == "[ID: 1] alpha"
    assert "sandbox_mode" not in captured
    assert log_calls == []
    assert needs_calls == []
    # Real dialog tools stay available (unused ones are ignored by the executor).
    tool_names = [t["function"]["name"] for t in fake.calls[0]["tools"]]
    assert "search_memory" in tool_names
    assert "write_diary" in tool_names
    assert "enter_rest" in tool_names


@pytest.mark.asyncio
async def test_runner_stores_user_and_reply_rows(monkeypatch):
    from core.test_chat import runner

    session = store.create_session(user_name="Алекс")
    sid = session["session_id"]

    fake = _FakeLLM([_reply("hello")])
    monkeypatch.setattr(ThinkPipeline, "main_llm", property(lambda self: fake))
    monkeypatch.setattr(pipeline_module, "_max_iterations", lambda: 3)
    monkeypatch.setattr(pipeline_module, "build_main_llm_system_prompt",
                        lambda *a, **k: "system")
    monkeypatch.setattr(ThinkPipeline, "_attach_media",
                        staticmethod(lambda messages, media, default_chat_id=None: messages))
    monkeypatch.setattr(runner, "get_bot_user_id", lambda: None)
    monkeypatch.setattr(runner, "get_indexer", lambda: SimpleNamespace(
        vector=None, embedder=None))

    result = await runner.run_turn(sid, "hello")

    assert [m["role"] for m in result["messages"]] == ["user", "assistant"]
    assert result["messages"][0]["content"] == "hello"
    assert result["messages"][1]["kind"] == "bubble"
    assert result["messages"][1]["content"] == "hello"
    assert result["session"]["user_name"] == "Алекс"

    history = store.build_history(sid)
    assert [m["role"] for m in history] == ["user", "assistant"]

    assert store.delete_session(sid) is True
    assert store.get_messages(sid) == []


@pytest.mark.asyncio
async def test_runner_write_diary_tool_uses_session_diary(monkeypatch):
    from core.test_chat import runner

    session = store.create_session(user_name="Алекс")
    sid = session["session_id"]

    fake = _FakeLLM([
        {"thoughts": "", "tool_calls": [
            _tool_call("write_diary",
                       '{"entry": "alpha event", "category": "day_entries"}'),
        ], "finish_reason": "tool_calls", "reasoning": ""},
        _reply("записала"),
    ])
    monkeypatch.setattr(ThinkPipeline, "main_llm", property(lambda self: fake))
    monkeypatch.setattr(pipeline_module, "_max_iterations", lambda: 3)
    monkeypatch.setattr(pipeline_module, "build_main_llm_system_prompt",
                        lambda *a, **k: "system")
    monkeypatch.setattr(ThinkPipeline, "_attach_media",
                        staticmethod(lambda messages, media, default_chat_id=None: messages))
    monkeypatch.setattr(runner, "get_bot_user_id", lambda: None)
    monkeypatch.setattr(runner, "get_indexer", lambda: SimpleNamespace(
        vector=None, embedder=None))

    result = await runner.run_turn(sid, "запиши это")

    assert result["messages"][1]["content"] == "записала"
    entries = store.get_diary_entries(sid)
    assert len(entries) == 1
    assert entries[0]["content"] == "alpha event"

    store.delete_session(sid)


@pytest.mark.asyncio
async def test_runner_rest_to_dialog_tool_switch_persists(monkeypatch):
    from core.test_chat import runner

    session = store.create_session(user_name="Алекс", state="rest")
    sid = session["session_id"]

    fake = _FakeLLM([
        {"thoughts": "", "tool_calls": [
            _tool_call("enter_dialog",
                       '{"chat_alias": "%s", "note": "хочу поговорить"}'
                       % session["alias"]),
        ], "finish_reason": "tool_calls", "reasoning": ""},
        _reply("привет"),
    ])
    monkeypatch.setattr(ThinkPipeline, "main_llm", property(lambda self: fake))
    monkeypatch.setattr(pipeline_module, "_max_iterations", lambda: 3)
    monkeypatch.setattr(pipeline_module, "build_main_llm_system_prompt",
                        lambda *a, **k: "system")
    monkeypatch.setattr(ThinkPipeline, "_attach_media",
                        staticmethod(lambda messages, media, default_chat_id=None: messages))
    monkeypatch.setattr(runner, "get_bot_user_id", lambda: None)
    monkeypatch.setattr(runner, "get_indexer", lambda: SimpleNamespace(
        vector=None, embedder=None))

    result = await runner.run_turn(sid, "привет")

    assert result["session"]["state"] == "dialog"
    assert store.get_session(sid)["state"] == "dialog"
    assert result["messages"][-1]["content"] == "привет"

    store.delete_session(sid)


@pytest.mark.asyncio
async def test_api_session_lifecycle():
    from fastapi import HTTPException
    from Mila_observatory.api import test_chat as api
    from Mila_observatory.models import (
        TestChatDiaryRequest,
        TestChatDiaryUpdateRequest,
        TestChatSessionCreateRequest,
        TestChatSessionUpdateRequest,
    )

    meta = await api.test_chat_meta()
    assert meta.default_user_name == store.DEFAULT_USER_NAME
    assert meta.states == list(store.VALID_STATES)

    created = await api.create_test_session(
        TestChatSessionCreateRequest(title="api scenario", user_name="Алекс"))
    sid = created.session_id
    assert created.user_name == "Алекс"
    assert created.user_id == "test-user:алекс"

    updated = await api.update_test_session(
        sid, TestChatSessionUpdateRequest(user_name="Мария", state="rest"))
    assert updated.user_name == "Мария"
    assert updated.state == "rest"
    assert updated.user_id == "test-user:мария"

    detail = await api.get_test_session(sid)
    assert detail.session.session_id == sid
    assert detail.messages == []

    entry = await api.create_test_diary_entry(
        sid, TestChatDiaryRequest(content="alpha event"))
    assert entry.content == "alpha event"
    updated_entry = await api.update_test_diary_entry(
        sid, entry.id, TestChatDiaryUpdateRequest(content="alpha updated"))
    assert updated_entry.content == "alpha updated"
    assert len(await api.list_test_diary(sid)) == 1
    assert (await api.delete_test_diary_entry(sid, entry.id))["success"] is True

    assert (await api.delete_test_session(sid))["success"] is True
    with pytest.raises(HTTPException) as exc:
        await api.get_test_session(sid)
    assert exc.value.status_code == 404


async def _noop():
    return None
