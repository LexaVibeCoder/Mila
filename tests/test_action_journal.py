"""Action journal: storage, compact rendering, and prompt placement."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import json

import pytest

import core.action_journal as journal
import core.db as db
import core.llm.persona as persona
import core.pipeline as pipeline_module
from core.delivery import TurnContext
from core.pipeline import ThinkPipeline


def _prev(state="rest", chat_id=None, name=None, updated_at=None):
    return {
        "state": state,
        "focus_chat_id": chat_id,
        "focus_user_name": name,
        "note": "",
        "updated_at": updated_at,
    }


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "mila.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    db.init_db()
    db.ensure_chat_exists("10001", "Alpha")
    return path


def test_dialog_span_and_state_transition(database):
    t0 = datetime(2026, 11, 6, 15, 30, tzinfo=timezone.utc)
    prev = _prev(updated_at=(t0 - timedelta(minutes=21)).isoformat())

    line = journal.record_state_change(
        prev, "dialog", chat_id="10001", chat_name="Alpha",
        note="say hi", now=t0,
    )

    assert "ты зашла в чат alpha" in line
    assert "из rest" in line
    assert "21 мин" in line
    assert "заметка: say hi" in line

    t1 = t0 + timedelta(minutes=21)
    journal.record_state_change(
        _prev("dialog", "10001", "Alpha", t0.isoformat()),
        "rest", note="tired", now=t1,
    )

    context = journal.get_journal_context(now=t1 + timedelta(minutes=1))
    assert "[15:30–15:51] была в чате alpha" in context
    assert "[15:51] перешла в rest (tired)" in context


def test_dialog_to_dialog_event_uses_previous_chat(database):
    db.ensure_chat_exists("10002", "Beta")
    t0 = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    prev = _prev("dialog", "10002", "Beta", (t0 - timedelta(minutes=21)).isoformat())

    line = journal.record_state_change(
        prev, "dialog", chat_id="10001", chat_name="Alpha", now=t0)

    assert "ты зашла в чат alpha из beta" in line
    assert "до этого была в чате beta 21 мин" in line


def test_chat_label_keeps_alias_and_new_name(database):
    db.ensure_chat_exists("10002", "Beta")
    db.ensure_chat_exists("10002", "Gamma")  # alias stays "beta", name changes
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)

    line = journal.record_state_change(
        _prev(), "dialog", chat_id="10002", chat_name="Gamma", now=now)

    assert "beta (Gamma)" in line


def test_same_state_is_not_journaled(database):
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    prev = _prev(updated_at=(now - timedelta(minutes=5)).isoformat())

    assert journal.record_state_change(prev, "rest", now=now) is None
    assert journal.get_journal_context(now=now) == ""


def test_sleep_tail_crosses_midnight(database):
    sleep_start = datetime(2026, 11, 5, 23, 30, tzinfo=timezone.utc)
    wake = datetime(2026, 11, 6, 6, 40, tzinfo=timezone.utc)

    journal.record_sleep_start(420, _prev(), now=sleep_start)
    journal.record_sleep_wake(
        (wake - sleep_start).total_seconds(), now=wake)

    context = journal.get_journal_context(
        now=datetime(2026, 11, 6, 7, 0, tzinfo=timezone.utc))

    assert "[вчера 23:30] легла спать на 7 ч" in context
    assert "[06:40] проснулась, спала 7 ч 10 мин" in context


def test_send_message_text_is_truncated(database):
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    journal.record_send_message("10001", "x" * 300, now=now)

    context = journal.get_journal_context(now=now)
    assert "отправила в чат alpha" in context
    assert "…" in context
    quoted = context.split("«", 1)[1].split("»", 1)[0]
    assert len(quoted) <= journal.SEND_TEXT_LIMIT


@pytest.mark.asyncio
async def test_delivery_journals_only_send_message(database, monkeypatch):
    import core.delivery as delivery

    async def fake_send_text(client, chat_id, text):
        return None

    async def fake_send_typing(client, chat_id):
        return None

    class _Indexer:
        def add_chunk(self, chat_id, user_id, content, **kwargs):
            return "chunk-1"

    monkeypatch.setattr(delivery, "send_text", fake_send_text)
    monkeypatch.setattr(delivery, "send_typing", fake_send_typing)
    monkeypatch.setattr(delivery.random, "uniform", lambda a, b: 0)
    monkeypatch.setattr(delivery, "push_message", lambda *a, **k: None)

    await delivery.deliver_messages(
        client=None, target="10001", bubbles=["hello"], my_user_id="me",
        indexer=_Indexer(), via="send_message_brush_off")
    await delivery.deliver_messages(
        client=None, target="10001", bubbles=["ordinary reply"],
        my_user_id="me", indexer=_Indexer(), via="bubbles")

    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT text FROM action_journal WHERE kind='send_message'").fetchall()
    finally:
        conn.close()

    assert [r["text"] for r in rows] == ["hello"]


def test_presence_transition_returns_fresh_event(database):
    from core.presence import enter_dialog, enter_rest

    enter_rest()
    result = enter_dialog("10001", "Alpha", "hello")

    assert "journal_event" in result
    assert "ты зашла в чат alpha" in result["journal_event"]
    assert "в чате alpha" in journal.get_journal_context()


@pytest.mark.asyncio
async def test_presence_tool_returns_string_state(database):
    from core.tools.executor import ToolExecutor

    executor = ToolExecutor(None, None, "user-1", "10001")
    result = await executor.execute_action(
        {"action_type": "enter_rest", "params": {"note": "tired"}})

    assert result["state"] == "rest"
    assert result["presence"]["state"] == "rest"
    assert "journal_event" in result


def test_same_state_note_keeps_span_start(database):
    from core.presence import enter_dialog, get_presence

    enter_dialog("10001", "Alpha", "first")
    started = get_presence()["updated_at"]
    enter_dialog("10001", "Alpha", "second")
    again = get_presence()

    assert again["updated_at"] == started
    assert again["note"] == "second"


@pytest.mark.asyncio
async def test_pipeline_switch_to_rest_keeps_state_and_fresh_event(
        database, monkeypatch):
    from types import SimpleNamespace
    from core.presence import enter_dialog, get_presence

    calls = []

    class _FakeLLM:
        default_temperature = 0.7

        async def generate(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                tool = SimpleNamespace(function=SimpleNamespace(
                    name="enter_rest",
                    arguments=json.dumps({"note": "tired"}),
                ))
                return {"thoughts": "", "tool_calls": [tool],
                        "finish_reason": "tool_calls"}
            return {"thoughts": "", "tool_calls": None,
                    "finish_reason": "stop"}

    prompts = []

    def fake_prompt(*args, **kwargs):
        prompts.append(kwargs)
        return "system"

    monkeypatch.setattr(ThinkPipeline, "main_llm", property(lambda self: _FakeLLM()))
    monkeypatch.setattr(
        pipeline_module, "build_main_llm_system_prompt", fake_prompt)
    monkeypatch.setattr(
        pipeline_module, "get_needs_tracker",
        lambda: SimpleNamespace(apply_action_effect=lambda *a: None),
    )
    monkeypatch.setattr(pipeline_module, "_max_iterations", lambda: 3)

    enter_dialog("10001", "Alpha")
    pipeline = ThinkPipeline(None, None, "user-1", "10001")
    monkeypatch.setattr(
        pipeline, "_attach_media",
        lambda messages, media, default_chat_id=None: messages)
    turn = TurnContext(
        turn_id="turn-1", chat_id="10001", user_id="user-1",
        presence=get_presence())

    events = [e async for e in pipeline._run_stream(
        "hi", [], None, request_user_id="user-1",
        request_chat_id="10001", turn=turn)]

    assert prompts[1]["presence"]["state"] == "rest"
    action = next(e for e in events if e["kind"] == "action")
    assert action["action"]["state"] == "rest"
    last_message = calls[1]["messages"][-1]["content"]
    assert "[событие]" in last_message
    assert "ты перешла в rest" in last_message


def test_prompt_puts_journal_between_state_and_diary(database, monkeypatch):
    monkeypatch.setattr(persona, "_get_static_prompt_prefix", lambda: "STATIC")
    monkeypatch.setattr(
        persona, "_get_profile_context",
        lambda user_id: ("Alpha", "REL", "2026-11-06 12:00"),
    )
    monkeypatch.setattr(persona, "get_today_diary_context", lambda: "DIARY")
    monkeypatch.setattr(
        persona, "get_needs_tracker",
        lambda: SimpleNamespace(get_needs_summary=lambda: "NEEDS"),
    )
    monkeypatch.setattr(
        "core.presence.format_state_block", lambda **kwargs: "PRESENCE")

    prompt = persona.build_main_llm_system_prompt(
        "user-1", journal_context="JOURNAL")

    needs_at = prompt.index("## Твоё текущее состояние")
    journal_at = prompt.index("## Что ты делала сегодня")
    diary_at = prompt.index("## Твой личный дневник")
    assert needs_at < journal_at < diary_at
    assert "JOURNAL" in prompt
