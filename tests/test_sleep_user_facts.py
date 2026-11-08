"""User-fact attribution and sleep rollback (no network, no real data)."""

import json
from types import SimpleNamespace

import pytest

import core.db as db
import core.sleep.manager as sleep_manager
import core.sleep.processes as sleep_processes


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "mila.db"))
    db.init_db()
    return tmp_path / "mila.db"


@pytest.mark.parametrize("raw", [
    "6566090861",
    "65660908",
    "65660908...",
    "[65660908...]",
    " 65660908… ",
])
def test_resolve_fact_user_id_accepts_full_and_unique_prefix(raw):
    user_id, error = sleep_processes.resolve_fact_user_id(
        raw, {"6566090861", "5789805811"})
    assert error is None
    assert user_id == "6566090861"


def test_resolve_fact_user_id_rejects_unknown_and_ambiguous():
    known = {"1234567890", "1234567800"}
    user_id, error = sleep_processes.resolve_fact_user_id("999", known)
    assert user_id is None
    assert "not a participant" in error

    user_id, error = sleep_processes.resolve_fact_user_id("12345678", known)
    assert user_id is None
    assert "ambiguous" in error


@pytest.mark.asyncio
async def test_stage_user_fact_repairs_truncated_id(database, monkeypatch):
    calls = []

    class FakeIndexer:
        async def add_memory(self, **kwargs):
            calls.append(kwargs)
            return "mem-1", True

    monkeypatch.setattr("core.memory.indexer.get_indexer", lambda: FakeIndexer())
    monkeypatch.setattr(db, "ensure_user_exists", lambda _uid: False)
    monkeypatch.setattr(sleep_processes, "stage_operation", lambda **kwargs: 1)

    executor = sleep_processes.StagingExecutor(
        "session-1", "user_fact_extraction",
        known_user_ids={"6566090861", "5789805811"},
        default_source_chat_id="chat-1",
    )
    tool_call = SimpleNamespace(function=SimpleNamespace(
        name="add_user_fact",
        arguments=json.dumps({
            "user_id": "65660908...",
            "content": "alpha fact",
            "importance": 0.7,
            "source_chat_id": "ignored-chat",
        }),
    ))

    result = await executor.execute_tool_call(tool_call)

    assert "Fact added" in result["content"]
    assert calls[0]["user_id"] == "6566090861"
    assert calls[0]["source_chat_id"] == "chat-1"


@pytest.mark.asyncio
async def test_stage_user_fact_rejects_unknown_id(database, monkeypatch):
    calls = []

    class FakeIndexer:
        async def add_memory(self, **kwargs):
            calls.append(kwargs)
            return "mem-1", True

    monkeypatch.setattr("core.memory.indexer.get_indexer", lambda: FakeIndexer())
    monkeypatch.setattr(sleep_processes, "stage_operation", lambda **kwargs: 1)

    executor = sleep_processes.StagingExecutor(
        "session-1", "user_fact_extraction",
        known_user_ids={"6566090861"},
        default_source_chat_id="chat-1",
    )
    tool_call = SimpleNamespace(function=SimpleNamespace(
        name="add_user_fact",
        arguments=json.dumps({
            "user_id": "999",
            "content": "alpha fact",
            "importance": 0.7,
        }),
    ))

    result = await executor.execute_tool_call(tool_call)

    assert result["content"].startswith("ERROR:")
    assert calls == []


def test_ensure_user_exists_reports_only_new_rows(database):
    assert db.ensure_user_exists("user-1", "User") is True
    assert db.ensure_user_exists("user-1", "User") is False


def test_bot_user_id_is_stored_and_can_be_inferred(database):
    assert db.get_bot_user_id() is None

    db.ensure_user_exists("user-1", "User")
    db.ensure_chat_exists("chat-1", "Chat 1")
    db.ensure_chat_exists("chat-2", "Chat 2")
    db.add_chunk("chat-1", "user-1", "alpha")
    db.add_chunk("chat-2", "user-1", "beta")
    assert db.get_bot_user_id() == "user-1"

    db.set_bot_user_id("bot-1")
    assert db.get_bot_user_id() == "bot-1"


@pytest.mark.asyncio
async def test_rollback_removes_user_row_created_by_session(database):
    session_id = "session-1"
    assert db.ensure_user_exists("ghost-1") is True
    sleep_processes.stage_operation(
        session_id=session_id,
        operation_type="insert",
        target_table="users",
        target_id="ghost-1",
        payload={"user_id": "ghost-1"},
    )
    mem_id = db.add_memory("ghost-1", "alpha fact")
    sleep_processes.stage_operation(
        session_id=session_id,
        operation_type="insert",
        target_table="memories",
        target_id=mem_id,
        payload={"mem_id": mem_id, "user_id": "ghost-1"},
    )

    await sleep_manager.rollback_all(session_id)

    assert db.get_memory(mem_id) is None
    assert db.get_user_info("ghost-1") is None