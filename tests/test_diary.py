"""Local-day exports, diary limits, decision model requests and safe clearing."""

import asyncio
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from config import settings
from core.context import buffer
from core.diary import decision, manager as diary
from core.tools.executor import ToolExecutor
from Mila_observatory.api import diary as api


@pytest.fixture(autouse=True)
def diary_env(tmp_path, monkeypatch):
    monkeypatch.setattr(buffer, "CONTEXT_DB_PATH", str(tmp_path / "context.db"))
    monkeypatch.setattr(diary, "DAILY_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setattr(settings, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(settings, "LOCAL_TIMEZONE", "Asia/Yekaterinburg")
    monkeypatch.setattr(settings, "DECISION_API_KEY", "")
    monkeypatch.setattr(settings, "DECISION_MODEL", "")


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


@pytest.mark.parametrize(("model_decision", "expected_status"), [
    ("duplicate", "duplicate"), ("update", "update_suggested"),
    ("replace", "updated"), ("uncertain-replace", "update_suggested"),
    ("new", "written"), ("weak-relation", "review_uncertain"),
    ("weak-change", "review_uncertain"), ("missing-change", "review_uncertain"),
    ("invalid-relation", "written"), ("failed-request", "written"),
])
@pytest.mark.parametrize(("api_base", "expected_url"), [
    ("", "https://openrouter.ai/api/alpha/decisions"),
    ("https://openrouter.ai/api/v1/", "https://openrouter.ai/api/alpha/decisions"),
    ("https://provider.example/api/alpha/ ", "https://provider.example/api/alpha/decisions"),
])
@pytest.mark.parametrize("category", diary.DIARY_CATEGORIES)
async def test_executor_decision_request(monkeypatch, model_decision, expected_status, api_base, expected_url, category):
    existing = diary.add_diary_entry("alpha entry", category)
    other_category = next(value for value in diary.DIARY_CATEGORIES if value != category)
    diary.add_diary_entry("gamma entry", other_category)
    entry_id = existing["entry_id"]
    monkeypatch.setattr(settings, "DECISION_API_BASE", api_base)
    monkeypatch.setattr(settings, "DECISION_API_KEY", "synthetic-key")
    monkeypatch.setattr(settings, "DECISION_MODEL", "provider/model")
    observed = []

    def handler(request):
        assert str(request.url) == expected_url
        assert request.headers["Authorization"] == "Bearer synthetic-key"
        body = json.loads(request.content)
        assert body["model"] == "provider/model"
        questions = body["questions"]
        assert set(questions) == {"grammar", f"related_{entry_id}", f"change_{entry_id}"}
        assert questions[f"related_{entry_id}"]["type"] == "noul"
        assert questions[f"change_{entry_id}"]["type"] == "choice"
        assert set(questions[f"change_{entry_id}"]["criteria"]) == {
            "duplicate", "extend", "replace", "unclear",
        }
        assert questions["grammar"]["type"] == "noul"
        observed.append(body["state"])
        if model_decision == "failed-request":
            return httpx.Response(503, text="unavailable")
        relation = {"new": 0.15, "weak-relation": 0.36, "uncertain-replace": 0.75,
                    "invalid-relation": 2}.get(model_decision, 0.95)
        answers = {
            f"related_{entry_id}": {"type": "noul", "noul": relation},
            "grammar": {"type": "noul", "noul": 0.95},
        }
        if model_decision != "missing-change":
            choice = "replace" if model_decision in ("replace", "uncertain-replace", "weak-change") else {
                "update": "extend", "duplicate": "duplicate",
            }.get(model_decision, "unclear")
            probability = 0.5 if model_decision == "weak-change" else 0.95
            answers[f"change_{entry_id}"] = {"type": "choice", "choice": choice, "probabilities": {choice: probability}}
        return httpx.Response(200, json={"answers": answers})

    original = httpx.AsyncClient
    monkeypatch.setattr(decision.httpx, "AsyncClient",
                        lambda: original(transport=httpx.MockTransport(handler)))
    executor = ToolExecutor(None, None, "user-1", "chat-1")
    repeat = await executor.execute_action({
        "action_type": "write_diary", "params": {"entry": "alpha entry", "category": category},
    })
    assert repeat["status"] == "duplicate"
    assert not observed
    result = await executor.execute_action({
        "action_type": "write_diary", "params": {"entry": "beta draft", "category": category},
    })
    assert result["status"] == expected_status
    trace = result["decision_review"]
    assert trace["model"] == "provider/model"
    assert trace["candidate_ids"] == [entry_id]
    assert trace["duration_ms"] >= 0
    assert "synthetic-key" not in json.dumps(trace)
    if model_decision == "failed-request":
        assert trace["status"] == "request_failed"
        assert trace["http_status"] == 503
    elif model_decision == "invalid-relation":
        assert trace["status"] == "invalid_response"
    else:
        assert trace["status"] == "reviewed"
        assert trace["grammar_probability"] == 0.95
        if model_decision == "weak-relation":
            assert trace["relation"]["probability"] == 0.36
            assert trace["accepted_match"] is None
        if model_decision == "uncertain-replace":
            assert trace["change"]["choice"] == "replace"
            assert trace["accepted_match"] == {"kind": "update_suggested", "entry_id": entry_id}
    assert observed == [{"draft": "beta draft", "candidates": [{"id": entry_id, "content_md": "alpha entry"}]}]
    rows = diary.get_all_diary_entries(category)
    if expected_status in ("written", "updated"):
        assert result.get("grammar_issue", False) is (model_decision not in ("invalid-relation", "failed-request"))
        assert result["daily_file_synced"]
        assert [row["content_md"] for row in rows] == (
            ["alpha entry", "beta draft"] if expected_status == "written" else ["beta draft"]
        )
        if expected_status == "updated":
            assert result["entry_id"] == entry_id
    else:
        assert result["entry_id"] == entry_id
        assert [row["content_md"] for row in rows] == ["alpha entry"]
        if expected_status == "update_suggested":
            assert result["update_entry_id"] == entry_id
            assert result["existing_entry"] == "alpha entry"
        else:
            assert "update_entry_id" not in result
        if expected_status == "review_uncertain":
            assert result["existing_entry"] == "alpha entry"
            assert trace["accepted_match"] is None


def test_competing_matches_do_not_trigger_automatic_replacement():
    candidates = [{"id": 1, "content_md": "alpha entry"}, {"id": 2, "content_md": "beta entry"}]
    answers = {
        "related_1": {"type": "noul", "noul": 0.92},
        "related_2": {"type": "noul", "noul": 0.96},
        "change_2": {"type": "choice", "choice": "replace", "probabilities": {"replace": 0.99}},
    }
    assert decision._semantic_match(answers, candidates, editing=False, trace={}) == (
        "review_uncertain", candidates[1],
    )


@pytest.mark.parametrize("model_decision", ["exact-repeat", "duplicate", "append", "category-change"])
async def test_executor_reviews_explicit_update(monkeypatch, model_decision):
    entry_id = diary.add_diary_entry("alpha entry")["entry_id"]
    other_id = diary.add_diary_entry("beta entry")["entry_id"]
    reflection_id = diary.add_diary_entry("gamma entry", "reflection_entries")["entry_id"]
    monkeypatch.setattr(settings, "DECISION_API_KEY", "synthetic-key")
    monkeypatch.setattr(settings, "DECISION_MODEL", "provider/model")
    switched_category = model_decision == "category-change"
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        candidate_id = reflection_id if switched_category else other_id
        assert [entry["id"] for entry in body["state"]["candidates"]] == [candidate_id]
        question = f"related_{candidate_id}"
        assert set(body["questions"]) == {"grammar", question}
        return httpx.Response(200, json={"answers": {
            question: {"type": "noul", "noul": 0.95 if model_decision == "duplicate" else 0.15},
        }})

    original = httpx.AsyncClient
    monkeypatch.setattr(decision.httpx, "AsyncClient",
                        lambda: original(transport=httpx.MockTransport(handler)))
    executor = ToolExecutor(None, None, "user-1", "chat-1")
    params = {"entry_id": str(entry_id), "entry": "alpha entry. Delta detail."}
    if model_decision == "exact-repeat":
        params["entry"] = "alpha entry"
    if switched_category:
        params.update(entry="alpha entry", category="reflection_entries")
    result = await executor.execute_action({"action_type": "update_diary", "params": params})
    if model_decision == "exact-repeat":
        assert not calls
        assert result["status"] == "unchanged"
        return
    assert len(calls) == 1
    assert result["decision_review"]["target_id"] == entry_id
    assert result["decision_review"]["status"] == "reviewed"
    target = next(row for row in diary.get_all_diary_entries() if row["id"] == entry_id)
    if model_decision == "duplicate":
        assert result["status"] == "duplicate"
        assert result["entry_id"] == other_id
        assert target["content_md"] == "alpha entry"
    else:
        assert result["status"] == "updated"
        assert target["content_md"] == params["entry"]
        assert target["category"] == params.get("category", "day_entries")


def test_semantic_replacement_uses_original_day_and_refuses_stale_target(monkeypatch):
    freeze(monkeypatch, "2026-04-01T12:00:00+05:00")
    entry_id = diary.add_diary_entry("alpha")["entry_id"]
    freeze(monkeypatch, "2026-04-02T12:00:00+05:00")
    for text in ("beta", "gamma", "delta"):
        diary.add_diary_entry(text)
    _, candidates = diary.diary_review_context("alpha revised", "day_entries")
    original = next(row for row in candidates if row["id"] == entry_id)

    result = diary.add_diary_entry("alpha revised", match=("replace", original))
    assert result["status"] == "updated"
    assert result["entry_id"] == entry_id
    assert "alpha revised" in diary.load_daily_report("01.04.2026")
    assert "alpha revised" not in diary.load_daily_report("02.04.2026")
    assert diary.add_diary_entry("alpha stale", match=("replace", original))["status"] == "review_stale"
    assert diary.get_diary_entry_count() == 4


def test_new_draft_refuses_stale_comparison_after_concurrent_write():
    _, candidates = diary.diary_review_context("alpha draft", "day_entries")
    first = diary.add_diary_entry("beta draft", candidates=candidates)
    second = diary.add_diary_entry("alpha draft", candidates=candidates)
    assert first["status"] == "written"
    assert second["status"] == "review_stale"
    assert diary.get_diary_entry_count() == 1
