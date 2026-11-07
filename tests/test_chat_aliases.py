"""Chat aliases are stable, migrated, and resolved before routing."""

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import core.db as db
import core.pipeline as pipeline_module
import core.llm.persona as persona
import core.proactive as proactive
from core.delivery import TurnContext
from core.events import Notification
from core.pipeline import ThinkPipeline
from core.presence import enter_dialog, enter_rest, get_presence, format_state_block
from core.proactive import build_proactive_block
from core.tools.executor import ToolExecutor, get_tool_definitions


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "mila.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    db.init_db()
    return path


def test_old_database_is_migrated_without_changing_messages(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE chats (chat_id TEXT PRIMARY KEY, chat_name TEXT, created_at TEXT);
            CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL,
                user_id TEXT, content TEXT, llm_full_response TEXT, type TEXT,
                faiss_id INTEGER, token_count INTEGER, time_start TEXT, time_end TEXT);
            INSERT INTO chats VALUES ('10001', 'Алекс', '2020-01-01');
            INSERT INTO chats VALUES ('10002', 'Алекс', '2020-01-02');
            INSERT INTO chunks (chunk_id, chat_id, content) VALUES ('chunk-1', '10001', 'alpha');
        """)
    db.init_db()
    db.init_db()
    assert db.get_chat_alias("10001") == "алекс"
    assert db.get_chat_alias("10002") == "алекс-2"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT chat_id, content FROM chunks").fetchall() == [("10001", "alpha")]
        assert conn.execute("SELECT count(*) FROM chat_aliases").fetchone()[0] == 2


@pytest.mark.parametrize(("name", "expected"), [
    (" Мария К. 🚀 ", "мария-к"),
    ("@АЛЕКС 2", "алекс-2"),
    ("Cafe\u0301", "café"),
    ("🦊", ""),
])
def test_normalization(name, expected):
    assert db.normalize_chat_alias(name) == expected


def test_stable_names_collision_and_no_reuse(database):
    db.ensure_chat_exists("10001", "Алекс")
    db.ensure_chat_exists("10002", "АЛЕКС")
    db.ensure_chat_exists("10001", "Другое имя")
    assert db.get_chat_alias("10001") == "алекс"
    assert db.get_chat_alias("10002") == "алекс-2"
    assert db.resolve_chat_alias("@Алекс 2") == "10002"
    assert db.resolve_chat_alias("10001") == "10001"
    assert db.resolve_chat_alias("99999") is None
    with db.get_conn() as conn:
        assert conn.execute("SELECT chat_name FROM chats WHERE chat_id='10001'").fetchone()[0] == "Другое имя"
        conn.execute("DELETE FROM chats WHERE chat_id='10001'")
    assert db.resolve_chat_alias("алекс") is None
    db.ensure_chat_exists("10003", "Алекс")
    assert db.get_chat_alias("10003") == "алекс-3"


def test_fallback_names_numeric_names_and_length(database):
    for cid, name in [("10001", "🦊"), ("10002", "Личный чат"),
                      ("10003", "123"), ("10004", "А" * 50)]:
        db.ensure_chat_exists(cid, name)
    assert db.get_chat_alias("10001") == "чат"
    assert db.get_chat_alias("10002") == "чат-2"
    assert db.get_chat_alias("10003") == "чат-123"
    assert db.get_chat_alias("10004") == "а" * 32


def test_parallel_allocation_does_not_duplicate_or_swap_aliases(database):
    def create(index):
        chat_id = str(10000 + index)
        db.ensure_chat_exists(chat_id, "Алекс")
        return chat_id, db.get_chat_alias(chat_id)

    with ThreadPoolExecutor(max_workers=6) as pool:
        pairs = list(pool.map(create, range(12)))
    assert len({alias for _, alias in pairs}) == 12
    assert all(db.resolve_chat_alias(alias) == chat_id for chat_id, alias in pairs)


def test_model_contexts_show_alias_not_chat_id(database):
    db.ensure_chat_exists("10001", "Алекс")
    db.ensure_chat_exists("10002", "Сэм")
    enter_dialog("10001", "Алекс")
    blocks = [
        Notification(kind="photo", chat_id="10001", user_id="20001", sender_name="Алекс").llm_event_line(),
        format_state_block("10002", "Сэм"),
        build_proactive_block([{"chat_id": "10001", "chat_name": "Алекс"}]),
    ]
    assert all("10001" not in block and "10002" not in block for block in blocks)
    assert all("алекс" in block for block in blocks)
    assert "сэм" in blocks[1]


@pytest.mark.asyncio
async def test_executor_routes_aliases_and_rejects_bad_targets(database):
    db.ensure_chat_exists("10001", "Алекс")
    db.ensure_chat_exists("10002", "Сэм")
    executor = ToolExecutor(None, None, "user-1", "10001")
    result = await executor.execute_action({"action_type": "enter_dialog", "params": {"chat_alias": "сэм", "note": "reason"}})
    assert result["chat_id"] == "10002"
    assert get_presence()["focus_chat_id"] == "10002"
    result = await executor.execute_action({"action_type": "send_message", "params": {"chat_alias": "алекс", "text": "hi"}})
    assert result["chat_id"] == "10001"
    for value in ("unknown", "", "99999", None):
        result = await executor.execute_action({"action_type": "send_message", "params": {"chat_alias": value, "text": "hi"}})
        assert result["status"] == "error"
        assert "chat_id" not in result
    assert (await executor.execute_action({"action_type": "send_message", "params": {"text": "hi"}}))["chat_id"] == "10001"
    proactive = ToolExecutor(None, None, "proactive", "proactive")
    assert (await proactive.execute_action({"action_type": "send_message", "params": {"text": "hi"}}))["status"] == "error"
    assert (await executor.execute_action({"action_type": "enter_dialog", "params": {}}))["status"] == "error"


@pytest.mark.asyncio
async def test_peek_uses_aliases_and_rejects_invalid_filter(database):
    db.ensure_chat_exists("10001", "Алекс")
    db.ensure_chat_exists("10002", "Сэм")
    db.add_chunk("10001", "user-1", "alpha")
    db.add_chunk("10002", "user-2", "beta")
    executor = ToolExecutor(None, None, "proactive", "proactive")
    result = await executor.execute_action({"action_type": "peek_recent_chats", "params": {"chat_aliases": ["алекс"]}})
    assert result["status"] == "peeked"
    assert "алекс" in result["context_addon"]
    assert "10001" not in result["context_addon"]
    assert "beta" not in result["context_addon"]
    bad = await executor.execute_action({"action_type": "peek_recent_chats", "params": {"chat_aliases": ["алекс", "wrong"]}})
    assert bad["status"] == "error"


@pytest.mark.asyncio
async def test_peek_empty_chat_uses_chat_name_not_numeric_id(database):
    db.ensure_chat_exists("10001", "Алекс")
    executor = ToolExecutor(None, None, "proactive", "proactive")
    result = await executor.execute_action({"action_type": "peek_recent_chats", "params": {"chat_aliases": ["алекс"]}})
    assert "Алекс" in result["context_addon"]
    assert "10001" not in result["context_addon"]


def test_proactive_prompt_shows_selected_chat_alias(database, monkeypatch):
    db.ensure_chat_exists("10001", "Алекс")
    enter_dialog("10001", "Алекс")
    monkeypatch.setattr(persona, "get_today_diary_context", lambda: "")
    monkeypatch.setattr(persona, "get_needs_tracker", lambda: SimpleNamespace(get_needs_summary=lambda: ""))
    prompt = persona.build_main_llm_system_prompt("proactive", "proactive")
    assert "Диалог: чат алекс с Алекс." in prompt
    assert "10001" not in prompt


def test_tool_schemas_expose_aliases():
    for state in ("rest", "working", "dialog"):
        definitions = get_tool_definitions(state, include_proactive=True)
        for item in definitions:
            fn = item["function"]
            properties = fn["parameters"].get("properties", {})
            assert "chat_id" not in properties and "chat_ids" not in properties
            if fn["name"] in ("enter_dialog", "send_message"):
                assert "chat_alias" in properties
            if fn["name"] == "enter_dialog":
                assert "note" in fn["parameters"]["required"]
            if fn["name"] == "peek_recent_chats":
                assert "chat_aliases" in properties


@pytest.mark.asyncio
async def test_pipeline_retries_unknown_alias_then_routes_known_alias(database, monkeypatch):
    db.ensure_chat_exists("10001", "Алекс")
    calls = []

    class FakeLLM:
        default_temperature = 0.7

        async def generate(self, **kwargs):
            calls.append(json.loads(json.dumps(kwargs, ensure_ascii=False)))
            alias = "wrong" if len(calls) == 1 else "алекс"
            tool = SimpleNamespace(function=SimpleNamespace(
                name="send_message", arguments=json.dumps({"chat_alias": alias, "text": "hi"})))
            return {"thoughts": "", "tool_calls": [tool], "finish_reason": "tool_calls"}

    monkeypatch.setattr(ThinkPipeline, "main_llm", property(lambda self: FakeLLM()))
    monkeypatch.setattr(pipeline_module, "build_main_llm_system_prompt", lambda *a, **k: "system")
    monkeypatch.setattr(pipeline_module, "get_needs_tracker", lambda: SimpleNamespace(apply_action_effect=lambda *a: None))
    monkeypatch.setattr(pipeline_module, "_max_iterations", lambda: 3)
    pipeline = ThinkPipeline(None, None, "user-1", "10001")
    monkeypatch.setattr(pipeline, "_attach_media", lambda messages, *a, **k: messages)
    monkeypatch.setattr(pipeline, "_current_tool_defs", lambda **kwargs: [])
    events = [event async for event in pipeline._run_stream("alpha", [], None)]
    assert events[-1]["kind"] == "reply_tool"
    assert events[-1]["action"]["chat_id"] == "10001"
    assert len(calls) == 2
    context = json.dumps(calls[1]["messages"], ensure_ascii=False)
    assert "Неизвестный" in context and "алекс" in context and "10001" not in context


@pytest.mark.asyncio
async def test_proactive_peek_then_enter_dialog_routes_real_chat(database, monkeypatch):
    db.ensure_chat_exists("10001", "Алекс")
    db.add_chunk("10001", "user-1", "alpha")
    enter_rest()
    calls = []

    class FakeLLM:
        default_temperature = 0.7

        async def generate(self, **kwargs):
            calls.append(json.loads(json.dumps(kwargs, ensure_ascii=False)))
            if len(calls) == 1:
                name, params = "peek_recent_chats", {}
            elif len(calls) == 2:
                name, params = "enter_dialog", {"chat_alias": "алекс", "note": "reason"}
            else:
                return {"thoughts": "hello", "tool_calls": None, "finish_reason": "stop"}
            tool = SimpleNamespace(function=SimpleNamespace(name=name, arguments=json.dumps(params)))
            return {"thoughts": "", "tool_calls": [tool], "finish_reason": "tool_calls"}

    tracker = SimpleNamespace(apply_action_effect=lambda *a: None, get_needs_summary=lambda: "")
    monkeypatch.setattr(ThinkPipeline, "main_llm", property(lambda self: FakeLLM()))
    monkeypatch.setattr(pipeline_module, "get_needs_tracker", lambda: tracker)
    monkeypatch.setattr(persona, "get_needs_tracker", lambda: tracker)
    monkeypatch.setattr(persona, "get_today_diary_context", lambda: "")
    monkeypatch.setattr(pipeline_module, "_max_iterations", lambda: 4)
    pipeline = ThinkPipeline(None, None, "proactive", "proactive")
    monkeypatch.setattr(pipeline, "_attach_media", lambda messages, *a, **k: messages)
    block = build_proactive_block(db.get_chats_by_last_activity())
    turn = TurnContext(turn_id="turn-1", chat_id="proactive", user_id="proactive",
                       presence=get_presence(), proactive=True)
    events = [event async for event in pipeline._run_stream(
        "", [], None, request_user_id="proactive", request_chat_id="proactive",
        turn=turn, enable_peek=True, proactive_block=block)]
    # The reply event carries no recipient; the turn owns routing.
    assert events[-1] == {"kind": "reply_bubbles", "texts": ["hello"]}
    assert turn.resolve_target() == "10001"
    assert get_presence()["focus_chat_id"] == "10001"
    assert "Диалог: чат алекс с Алекс." in calls[-1]["system_prompt"]
    assert "10001" not in json.dumps(calls, ensure_ascii=False)


@pytest.mark.asyncio
async def test_enter_dialog_requires_nonempty_reason(database):
    db.ensure_chat_exists("10001", "alpha")
    executor = ToolExecutor(None, None, "user-1", "10001")
    for note in (None, "", "  ", 5):
        result = await executor.execute_action({
            "action_type": "enter_dialog",
            "params": {"chat_alias": "alpha", "note": note},
        })
        assert result["status"] == "error"
        assert get_presence()["state"] == "rest"
    result = await executor.execute_action({
        "action_type": "enter_dialog",
        "params": {"chat_alias": "alpha", "note": "test reason"},
    })
    assert result["note"] == "test reason"
    assert get_presence()["note"] == "test reason"


@pytest.mark.asyncio
async def test_notification_and_switch_use_focused_history_and_peer(database, monkeypatch):
    for cid, name, uid in [("10001", "alpha", "user-1"),
                           ("10002", "beta", "user-2"),
                           ("10003", "gamma", "user-3")]:
        db.ensure_chat_exists(cid, name)
        db.ensure_user_exists(uid, name)
        db.add_chunk(cid, uid, f"{name} history")
    db.ensure_chat_exists("10004", "empty")
    enter_dialog("10001", "alpha")
    calls, prompts = [], []

    class FakeLLM:
        default_temperature = 0.7

        async def generate(self, **kwargs):
            calls.append(json.loads(json.dumps(kwargs)))
            if len(calls) == 1:
                tc = SimpleNamespace(function=SimpleNamespace(name="enter_dialog", arguments=json.dumps({
                    "chat_alias": "gamma", "note": "chosen reason"})))
                return {"thoughts": "", "tool_calls": [tc], "finish_reason": "tool_calls"}
            return {"thoughts": "hello gamma", "tool_calls": None, "finish_reason": "stop"}

    monkeypatch.setattr(ThinkPipeline, "main_llm", property(lambda self: FakeLLM()))
    monkeypatch.setattr(pipeline_module, "build_main_llm_system_prompt",
                        lambda user, **kw: prompts.append((user, kw)) or "system")
    monkeypatch.setattr(pipeline_module, "get_needs_tracker",
                        lambda: SimpleNamespace(apply_action_effect=lambda *a: None))
    pipeline = ThinkPipeline(None, None, "user-2", "10002", my_user_id="self")
    monkeypatch.setattr(pipeline, "_attach_media", lambda messages, *a, **k: messages)
    histories = {"10001": [{"role": "user", "content": "alpha history"}],
                 "10003": [{"role": "user", "content": "gamma history"}]}
    turn = TurnContext("turn-1", "10002", "user-2", presence=get_presence())
    events = [event async for event in pipeline._run_stream(
        "beta notification", [{"role": "user", "content": "beta history"}], None,
        request_user_id="user-2", request_chat_id="10002", turn=turn,
        history_loader=lambda cid: histories.get(cid, []))]

    assert turn.resolve_target() == "10003"
    assert events[-1]["texts"] == ["hello gamma"]
    first = json.dumps(calls[0], ensure_ascii=False)
    second = json.dumps(calls[1], ensure_ascii=False)
    assert "alpha history" in first and "beta notification" in first
    assert "beta history" not in first
    assert "gamma history" in second and "chosen reason" in second
    assert "alpha history" not in second and "beta notification" not in second
    assert "empty" in calls[0]["system_prompt"]
    assert [item[0] for item in prompts] == ["user-1", "user-3"]
    assert prompts[0][1]["notification_user_id"] == "user-2"


@pytest.mark.asyncio
async def test_note_to_notification_does_not_switch_global_focus(database, monkeypatch):
    db.ensure_chat_exists("10001", "alpha")
    db.ensure_chat_exists("10002", "beta")
    enter_dialog("10001", "alpha")

    class FakeLLM:
        default_temperature = 0.7

        async def generate(self, **kwargs):
            tc = SimpleNamespace(function=SimpleNamespace(name="send_message", arguments=json.dumps({
                "chat_alias": "beta", "text": "short note"})))
            return {"thoughts": "", "tool_calls": [tc], "finish_reason": "tool_calls"}

    monkeypatch.setattr(ThinkPipeline, "main_llm", property(lambda self: FakeLLM()))
    monkeypatch.setattr(pipeline_module, "build_main_llm_system_prompt", lambda *a, **kw: "system")
    monkeypatch.setattr(pipeline_module, "get_needs_tracker",
                        lambda: SimpleNamespace(apply_action_effect=lambda *a: None))
    pipeline = ThinkPipeline(None, None, "user-2", "10002")
    monkeypatch.setattr(pipeline, "_attach_media", lambda messages, *a, **k: messages)
    turn = TurnContext("turn-1", "10002", "user-2", presence=get_presence())
    events = [event async for event in pipeline._run_stream(
        "notification", [], None, request_user_id="user-2", request_chat_id="10002",
        turn=turn, history_loader=lambda chat: [])]
    assert events[-1]["kind"] == "reply_tool"
    assert turn.resolve_target() == "10002"
    assert get_presence()["focus_chat_id"] == "10001"


@pytest.mark.asyncio
@pytest.mark.parametrize(("incoming", "sends"), [("10001", False), ("10002", True)])
async def test_proactive_uses_shared_lock_and_only_target_interrupts(database, monkeypatch, incoming, sends):
    db.ensure_chat_exists("10001", "alpha")
    db.ensure_chat_exists("10002", "beta")
    db.add_chunk("10001", "user-1", "alpha")
    enter_dialog("10001", "alpha")
    started, release = asyncio.Event(), asyncio.Event()
    sent = []

    class Pipeline:
        async def process_message_stream(self, **kwargs):
            started.set()
            await release.wait()
            yield {"type": "reply", "texts": ["hello"], "via": "bubbles"}

    coordinator = SimpleNamespace(turn_lock=asyncio.Lock(), _revisions={},
                                  is_user_typing=lambda chat: False)
    bot = SimpleNamespace(messages=coordinator, pipeline=Pipeline(), app=None,
                          me=SimpleNamespace(id=42), indexer=None)

    async def deliver(**kwargs):
        assert await kwargs["before_bubble"](0)
        sent.append(kwargs["target"])
        return SimpleNamespace(ok=True, sent=1, error=None)

    monkeypatch.setattr(proactive, "deliver_messages", deliver)
    monkeypatch.setattr("core.sleep.manager.is_sleeping", lambda: False)
    await coordinator.turn_lock.acquire()
    task = asyncio.create_task(proactive.run_proactive_turn(bot))
    await asyncio.sleep(0)
    assert not started.is_set()
    coordinator.turn_lock.release()
    await started.wait()
    coordinator._revisions[incoming] = 1
    release.set()
    result = await task
    assert bool(sent) is sends
    assert result["success"] is sends
    if sends:
        assert sent == ["10001"]
