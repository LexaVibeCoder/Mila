"""Diary writes, reorganization, local-day reports and safe clearing."""

import asyncio
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from config import settings
from core.context import buffer
from core.diary import manager as diary, reorganization
from core.sleep import state
from core.tools.executor import ToolExecutor
from Mila_observatory.api import diary as api


@pytest.fixture(autouse=True)
def diary_env(tmp_path, monkeypatch):
    monkeypatch.setattr(buffer, "CONTEXT_DB_PATH", str(tmp_path / "context.db"))
    monkeypatch.setattr(diary, "DAILY_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setattr(settings, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(settings, "LOCAL_TIMEZONE", "Asia/Yekaterinburg")
    monkeypatch.setattr("core.db.DB_PATH", str(tmp_path / "mila.db"))


def freeze(monkeypatch, timestamp):
    instant = datetime.fromisoformat(timestamp)
    monkeypatch.setattr(buffer, "now_local", lambda: instant)


def test_exact_duplicates_and_update(monkeypatch):
    first = diary.add_diary_entry("alpha")
    second = diary.add_diary_entry("beta")
    repeat = diary.add_diary_entry(" alpha ", "reflection_entries")
    assert repeat["status"] == "duplicate"
    assert "decision_review" not in repeat
    assert repeat["entry_id"] == first["entry_id"]
    assert "update_entry_id" not in repeat

    rejected = diary.update_diary_entry(second["entry_id"], "alpha")
    assert rejected["status"] == "duplicate"
    assert rejected["entry_id"] == first["entry_id"]
    assert diary.update_diary_entry(second["entry_id"], "beta")["status"] == "unchanged"
    assert diary.update_diary_entry(1234, "gamma")["status"] == "not_found"
    assert diary.get_diary_entry_count() == 2


async def test_concurrent_write_allocates_once():
    left, right = await asyncio.gather(
        asyncio.to_thread(diary.add_diary_entry, "alpha"),
        asyncio.to_thread(diary.add_diary_entry, "alpha"),
    )
    assert {left["status"], right["status"]} == {"written", "duplicate"}
    assert left["entry_id"] == right["entry_id"]


async def test_local_midnight_reports_and_context(monkeypatch):
    freeze(monkeypatch, "2025-12-31T23:45:00+05:00")
    old = diary.add_diary_entry("alpha yesterday")
    freeze(monkeypatch, "2026-01-01T00:30:00+05:00")
    diary.add_diary_entry("beta reflection", "reflection_entries")
    freeze(monkeypatch, "2026-01-01T00:31:00+05:00")
    diary.add_diary_entry("gamma event")

    rows = diary.get_all_diary_entries(local_day=date(2026, 1, 1))
    assert [row["created_at"] for row in rows] == ["2025-12-31 19:30:00", "2025-12-31 19:31:00"]
    context = diary.get_today_diary_context()
    assert "alpha" not in context
    assert "beta" in context and "gamma" in context

    assert diary.update_diary_entry(old["entry_id"], "delta yesterday")["daily_file_synced"]
    assert "delta yesterday" in diary.load_daily_report("31.12.2025")
    report = await api.get_today_diary()
    assert report["date"] == "01.01.2026"
    assert report["entry_count"] == 2
    content = report["content"]
    assert "delta" not in content
    assert content.index("reflection_entries — 2026-01-01 00:30") < content.index("day_entries — 2026-01-01 00:31")
    assert [row["date"] for row in (await api.get_all_diaries())["reports"]] == ["01.01.2026", "31.12.2025"]
    Path(diary.DAILY_REPORTS_DIR, "31.02.2026.md").touch()
    assert "31.02.2026" not in diary.list_daily_reports()
    with pytest.raises(HTTPException) as error:
        await api.get_diary_by_date("31.02.2026")
    assert error.value.status_code == 400


def test_dst_day_bounds(monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_TIMEZONE", "Europe/Berlin")
    assert diary._utc_day_bounds(date(2026, 3, 29)) == ("2026-03-28 23:00:00", "2026-03-29 22:00:00")


def test_more_than_three_entries_in_one_category_per_day(monkeypatch):
    freeze(monkeypatch, "2026-04-01T12:00:00+05:00")
    for text in ("alpha", "beta", "gamma", "delta"):
        assert diary.add_diary_entry(text)["status"] == "written"

    reflection = diary.add_diary_entry("epsilon", "reflection_entries")
    result = diary.update_diary_entry(reflection["entry_id"], "epsilon", "day_entries")
    assert result["status"] == "updated"
    assert result["category"] == "day_entries"
    assert result["daily_file_synced"]
    assert len(diary.get_all_diary_entries(category="day_entries")) == 5
    assert diary.count_report_entries(diary.load_daily_report("01.04.2026")) == 5


def test_entry_length_limit():
    valid = diary.add_diary_entry("x" * diary.MAX_ENTRY_LENGTH)
    assert valid["status"] == "written"
    too_long = "я" * (diary.MAX_ENTRY_LENGTH + 1)
    assert diary.add_diary_entry(too_long)["status"] == "error"
    assert diary.update_diary_entry(valid["entry_id"], too_long)["status"] == "error"


def test_export_failure_reports_commit_and_retry_repairs_file(monkeypatch):
    original = diary._atomic_write

    def fail(path, markdown):
        raise OSError("disk unavailable")

    monkeypatch.setattr(diary, "_atomic_write", fail)
    first = diary.add_diary_entry("alpha")
    assert first["status"] == "written"
    assert not first["daily_file_synced"]
    assert diary.get_diary_entry_count() == 1
    monkeypatch.setattr(diary, "_atomic_write", original)
    retry = diary.add_diary_entry("alpha")
    assert retry["daily_file_synced"] and retry["entry_id"] == first["entry_id"]
    assert "alpha" in diary.load_daily_report()


def test_distinct_backups_and_clear_keeps_full_daily_report(monkeypatch, tmp_path):
    freeze(monkeypatch, "2026-04-01T12:00:00+05:00")
    diary.add_diary_entry("alpha")
    assert diary.save_diary_to_logs()
    diary.clear_diary()
    assert diary.get_diary_entry_count() == 0
    backups = list((tmp_path / "logs" / "diary_logs").glob("diary_20260401_*.md"))
    assert len(backups) == 2
    assert all("alpha" in path.read_text() for path in backups)
    diary.add_diary_entry("beta")
    assert diary.count_report_entries(diary.load_daily_report()) == 2


def test_clear_refuses_failed_backup_or_changed_snapshot(monkeypatch):
    diary.add_diary_entry("alpha")
    old_snapshot = diary.snapshot_diary_entries()
    diary.add_diary_entry("beta")
    with pytest.raises(RuntimeError, match="changed"):
        diary.clear_diary(old_snapshot)
    with monkeypatch.context() as patch:
        patch.setattr(diary, "save_diary_to_logs", lambda entries: False)
        with pytest.raises(OSError, match="backup failed"):
            diary.clear_diary()
    assert diary.get_diary_entry_count() == 2


def test_legacy_schema_migrates_at_initialization():
    with sqlite3.connect(buffer.CONTEXT_DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE diary_entries (
                id INTEGER PRIMARY KEY, entry_number INTEGER, content_md TEXT, created_at TEXT
            );
            INSERT INTO diary_entries VALUES (1, 1, 'alpha', '2025-05-04 08:00:00');
        """)
    diary.init_diary()
    diary.init_diary()
    entry, = diary.get_all_diary_entries()
    assert entry["category"] == "day_entries"
    assert entry["updated_at"] == entry["created_at"]
    assert diary.update_diary_entry(entry["id"], "beta")["status"] == "updated"


def test_backup_path_uses_project_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "LOG_DIR", "test-logs")
    # Point the project root to a temp directory to check path resolution safely.
    monkeypatch.setattr("config.PROJECT_DIR", tmp_path / "project")
    diary.add_diary_entry("alpha")
    assert diary.save_diary_to_logs()
    assert len(list((tmp_path / "project" / "test-logs" / "diary_logs").glob("*.md"))) == 1
    assert not (tmp_path / "test-logs").exists()


async def test_tools_save_rephrasing_and_explicit_edit_before_reorganization(monkeypatch):
    propose = AsyncMock(side_effect=AssertionError("Tools must save without waiting for the organizer"))
    monkeypatch.setattr(reorganization, "_propose_merges", propose)
    executor = ToolExecutor(None, None, "user-1", "chat-1")

    async def write(text):
        return await executor.execute_action({"action_type": "write_diary", "params": {"entry": text}})

    first = await write("Alpha test finished.")
    second = await write("Completed the alpha test.")
    assert first["status"] == second["status"] == "written"
    assert (await write("Alpha test finished."))["status"] == "duplicate"
    updated = await executor.execute_action({
        "action_type": "update_diary",
        "params": {"entry_id": first["entry_id"], "entry": "Alpha test finished. It took one minute."},
    })
    assert updated["status"] == "updated"
    assert {row["id"] for row in diary.get_diary_reorganization_batch()} == {
        first["entry_id"], second["entry_id"],
    }
    propose.assert_not_awaited()


async def test_reorganization_keeps_originals_and_exports_curated_diary(monkeypatch):
    first = diary.add_diary_entry("Alpha test finished.")
    second = diary.add_diary_entry("Completed the alpha test in one minute.")
    older_id, newer_id = first["entry_id"], second["entry_id"]
    merged = "Alpha test finished in one minute."
    generate = AsyncMock(return_value={
        "finish_reason": "stop",
        "thoughts": json.dumps({"merges": [{"ids": [newer_id, older_id], "text": merged}]}),
    })
    monkeypatch.setattr(settings, "DIARY_LLM_MODEL", "diary-model")
    client = Mock(return_value=SimpleNamespace(client=AsyncMock(), generate=generate))
    monkeypatch.setattr(reorganization, "LLMClient", client)

    await reorganization.reorganize_diary()
    client.assert_called_once_with(model="diary-model", timeout=25)
    generate.assert_awaited_once()
    payload = json.loads(generate.await_args.args[0][0]["content"])
    assert [entry["id"] for entry in payload["entries"]] == [older_id, newer_id]
    assert all(entry["pending"] for entry in payload["entries"])
    row, = diary.get_all_diary_entries()
    assert (row["id"], row["content_md"]) == (older_id, merged)
    assert diary.get_diary_entry_count() == 1
    assert diary.count_report_entries(diary.load_daily_report()) == 1
    assert merged in diary.load_daily_report()
    assert diary.get_diary_reorganization_batch() == []
    with sqlite3.connect(buffer.CONTEXT_DB_PATH) as conn:
        original, sources = conn.execute(
            "SELECT content_md, source_ids FROM diary_reorganization_history WHERE entry_id=?",
            (older_id,),
        ).fetchone()
        assert original == "Alpha test finished."
        assert json.loads(sources) == [newer_id]
        assert conn.execute("SELECT content_md, merged_into FROM diary_entries WHERE id=?",
                            (newer_id,)).fetchone() == ("Completed the alpha test in one minute.", older_id)
    assert diary.add_diary_entry("Alpha test finished.")["status"] == "duplicate"
    assert diary.add_diary_entry("Completed the alpha test in one minute.")["status"] == "duplicate"
    await reorganization.reorganize_diary()
    generate.assert_awaited_once()
    assert diary.update_diary_entry(older_id, "Completed the alpha test in one minute.")["status"] == "updated"


def test_reorganization_retains_pending_edit_when_snapshot_is_stale():
    first = diary.add_diary_entry("alpha")
    second = diary.add_diary_entry("beta")
    batch = diary.get_diary_reorganization_batch()
    target = first["entry_id"]
    assert diary.update_diary_entry(target, "alpha revised")["status"] == "updated"
    assert not diary.apply_diary_reorganization(batch, [([target, second["entry_id"]], "alpha merged")])
    assert [row["content_md"] for row in diary.get_all_diary_entries()] == ["alpha revised", "beta"]
    assert len(diary.get_diary_reorganization_batch()) == 2


async def test_sleep_transition_defers_hourly_plan_and_allows_final_review(monkeypatch):
    first = diary.add_diary_entry("alpha")
    second = diary.add_diary_entry("beta")
    plan = [([first["entry_id"], second["entry_id"]], "alpha and beta")]
    session_id = "sleep-1"

    async def start_sleep_during_review(_):
        assert state.set_state(state.SleepState.SLEEPING, session_id=session_id)
        return plan

    monkeypatch.setattr(reorganization, "_propose_merges", start_sleep_during_review)
    await reorganization.reorganize_diary()
    assert diary.get_diary_entry_count() == 2
    assert len(diary.get_diary_reorganization_batch()) == 2

    propose = AsyncMock(return_value=plan)
    monkeypatch.setattr(reorganization, "_propose_merges", propose)
    await reorganization.reorganize_diary(session_id)
    propose.assert_awaited_once()
    assert diary.get_diary_entry_count() == 1


def test_reorganization_retries_report_after_export_failure(monkeypatch):
    first = diary.add_diary_entry("alpha")
    second = diary.add_diary_entry("beta")
    batch = diary.get_diary_reorganization_batch()
    assert diary.apply_diary_reorganization(batch, [([first["entry_id"], second["entry_id"]], "alpha and beta")])

    def fail_export(path, markdown):
        raise OSError("disk unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(diary, "_atomic_write", fail_export)
        diary.sync_reorganized_diary_reports()
    assert diary.count_report_entries(diary.load_daily_report()) == 2
    diary.sync_reorganized_diary_reports()
    assert diary.count_report_entries(diary.load_daily_report()) == 1


@pytest.mark.parametrize("groups", [
    [{"ids": [1, 999], "text": "alpha"}],
    [{"ids": [1, 1], "text": "alpha"}],
    [{"ids": [2, 3], "text": "alpha"}],
    [{"ids": [1, 2], "text": "alpha"}, {"ids": [1, 3], "text": "beta"}],
    [{"ids": [1, 2], "text": "x" * (diary.MAX_ENTRY_LENGTH + 1)}],
])
def test_merge_plan_rejects_invalid_groups(groups):
    originals = [{"id": 1, "review_pending": True}, {"id": 2, "review_pending": False},
                 {"id": 3, "review_pending": False}]
    with pytest.raises(ValueError):
        reorganization._parse_merges(json.dumps({"merges": groups}), originals)


def test_reorganization_batch_stays_within_local_day_and_category(monkeypatch):
    freeze(monkeypatch, "2026-04-01T23:59:00+05:00")
    target = diary.add_diary_entry("alpha")
    diary.add_diary_entry("beta", "reflection_entries")
    freeze(monkeypatch, "2026-04-02T00:01:00+05:00")
    diary.add_diary_entry("gamma")
    batch = diary.get_diary_reorganization_batch()
    assert [row["id"] for row in batch] == [target["entry_id"]]


async def test_hourly_reorganization_repeats_until_cancelled(monkeypatch):
    sleep = AsyncMock(side_effect=[None, None, asyncio.CancelledError()])
    review = AsyncMock()
    monkeypatch.setattr(reorganization.asyncio, "sleep", sleep)
    monkeypatch.setattr(reorganization, "reorganize_diary", review)

    with pytest.raises(asyncio.CancelledError):
        await reorganization.run_hourly_diary_reorganization()

    assert [call.args for call in sleep.await_args_list] == [(3600,), (3600,), (3600,)]
    assert review.await_count == 2
