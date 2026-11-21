"""Sleep atomicity: commit / rollback / recovery contract.

Covers TODO.md S1-S9, S15-S16, S18-S19. Expectations come from the SPEC table,
not from the implementation. Hermetic: temp SQLite files, fake indexer, no LLM.
"""

import glob
import re
import sqlite3

import pytest

import core.sleep.manager as sm
import core.sleep.processes as sp
from core import db as mila_db
from core.context import buffer as context_buffer
from core.diary import manager as diary
from core.memory import eternal
from core.sleep.state import SleepState, get_state


def _snapshot(rows):
    """Identity of message rows: ids, text and type, in order."""
    return [(r["id"], r["content"], r["type"]) for r in rows]


def _backup_files(env):
    return glob.glob(str(env.log_dir / "diary_logs" / "diary_*.md"))


def _chunk_ids(env):
    eternal._ensure_table()
    return [r["chunk_id"] for r in env._rows("SELECT chunk_id FROM mila_memory_chunks")]


# ---------------------------------------------------------------------------
# Case 1: successful sleep -> summary + memory + diary backup + diary clear
# ---------------------------------------------------------------------------

async def test_successful_sleep_summarizes_extracts_backs_up_and_clears_diary(
        sleep_env, monkeypatch):
    env = sleep_env
    env.add_diary("alpha diary note")
    seeded = env.seed_messages(12)
    session = env.begin_sleep()

    async def work(sid):
        await env.stage_summary(sid, text="alpha summary", claimed=12)
        await env.stage_fact(sid, content="alpha fact")

    env.use_processes(monkeypatch, work)

    assert await sm.execute_sleep(session) is True

    # S2: after commit only the summary remains in the chat, no messages.
    # The stored text carries the code-added period prefix derived from the
    # seeded rows' local dates; the plain LLM body is preserved after it.
    summaries = [s["content"] for s in env.summaries()]
    assert len(summaries) == 1
    dates = sorted({
        context_buffer.format_created_at_local(r.get("created_at") or "")[:10]
        for r in seeded
    })
    dates = [d for d in dates if re.match(r"^\d{4}-\d{2}-\d{2}$", d or "")]
    expected_prefix = (
        f"[{dates[0]}] " if dates and dates[0] == dates[-1]
        else f"[{dates[0]} \u2014 {dates[-1]}] " if dates else ""
    )
    assert expected_prefix, "seeded rows must carry parseable dates"
    assert summaries[0] == expected_prefix + "alpha summary"
    assert summaries[0].endswith("alpha summary")
    assert env.messages() == []
    assert env.memory_count("alpha fact") == 1
    # S1: diary archived (backup written) and then cleared.
    backups = _backup_files(env)
    assert backups, "diary must be archived to logs before it is cleared"
    assert "alpha diary note" in open(backups[0], encoding="utf-8").read()
    assert env.diary_count() == 0
    # Staging is marked applied after the commit, and the summary log is not rolled back.
    staged = env.staging(session)
    assert staged and all(r["applied"] == 1 for r in staged)
    log = env.summary_log(session)
    assert len(log) == 1
    assert log[0]["messages_removed"] == 12
    assert log[0]["rolled_back"] == 0
    assert get_state() == SleepState.AWAKE


# ---------------------------------------------------------------------------
# S1, S2, S3, S4: failed sleep keeps the diary and restores the chat
# ---------------------------------------------------------------------------

async def test_failed_process_keeps_diary_and_restores_messages(sleep_env, monkeypatch):
    env = sleep_env
    env.add_diary("alpha diary note")
    before = _snapshot(env.seed_messages(12))
    session = env.begin_sleep()

    async def work(sid):
        await env.stage_summary(sid, claimed=12)
        await env.stage_fact(sid)

    env.use_processes(monkeypatch, work, ok=False)

    assert await sm.execute_sleep(session) is False

    # S1: diary survives a failed sleep, content intact.
    entries = diary.get_all_diary_entries()
    assert [e["content_md"] for e in entries] == ["alpha diary note"]
    # S2/S3: original messages back with original ids, no summary row.
    assert _snapshot(env.messages()) == before
    assert env.summaries() == []
    # Memory staged during the failed night is gone.
    assert env.memory_count("alpha fact") == 0
    assert env.staging(session) == []
    # S4: the rolled-back session's summary log is marked (or removed).
    log = env.summary_log(session)
    assert len(log) == 1, "the failed night produced one summary log row"
    assert all(r["rolled_back"] == 1 for r in log)
    assert get_state() == SleepState.AWAKE


async def test_rollback_restores_compressed_messages_and_removes_summary(sleep_env):
    env = sleep_env
    before = _snapshot(env.seed_messages(12))
    session = env.begin_sleep()
    await env.stage_summary(session, claimed=12)
    # Precondition: the compression really happened before we roll it back.
    assert env.messages() == []
    assert len(env.summaries()) == 1

    await sm.rollback_all(session)

    assert _snapshot(env.messages()) == before
    assert env.summaries() == []
    assert env.staging(session) == []
    assert get_state() == SleepState.AWAKE


async def test_rollback_marks_summary_log_rows_as_rolled_back(sleep_env):
    env = sleep_env
    env.seed_messages(12)
    session = env.begin_sleep()
    await env.stage_summary(session, claimed=12)
    assert len(env.summary_log(session)) == 1

    await sm.rollback_all(session)

    # Implementation contract: the row is kept and marked (S4 also allows removal;
    # require the non-empty length first so a removal cannot pass vacuously).
    rows = env.summary_log(session)
    assert len(rows) == 1
    assert all(r["rolled_back"] == 1 for r in rows)


async def test_rollback_is_idempotent(sleep_env):
    env = sleep_env
    before = _snapshot(env.seed_messages(12))
    session = env.begin_sleep()
    await env.stage_summary(session, claimed=12)

    await sm.rollback_all(session)
    await sm.rollback_all(session)

    assert _snapshot(env.messages()) == before
    assert env.summaries() == []


# ---------------------------------------------------------------------------
# S5: backup first, mark applied last; backup failure -> applied=0 -> recovery rolls back
# ---------------------------------------------------------------------------

async def test_backup_failure_leaves_staging_unapplied_and_recovery_rolls_back(
        sleep_env, monkeypatch):
    env = sleep_env
    env.add_diary("alpha diary note")
    before = _snapshot(env.seed_messages(12))
    session = env.begin_sleep()
    await env.stage_summary(session, claimed=12)
    await env.stage_fact(session)
    monkeypatch.setattr(diary, "save_diary_to_logs", lambda entries: False)

    # S6: a failed backup makes the commit fail.
    assert await sm.commit_all(session) is False

    # S5: nothing was marked applied, so recovery sees unfinished work.
    staged = env.staging(session)
    assert staged and all(r["applied"] == 0 for r in staged)
    # The diary was not cleared because its backup did not succeed.
    assert env.diary_count() == 1

    assert await sm.recover_from_crash() is True

    assert _snapshot(env.messages()) == before
    assert env.summaries() == []
    assert env.memory_count("alpha fact") == 0
    assert env.diary_count() == 1
    assert get_state() == SleepState.AWAKE


async def test_commit_backs_up_diary_before_marking_staging_applied(sleep_env, monkeypatch):
    env = sleep_env
    env.add_diary("alpha diary note")
    env.seed_messages(12)
    session = env.begin_sleep()
    await env.stage_summary(session, claimed=12)

    order = []
    real_save = diary.save_diary_to_logs
    real_clear = sm.clear_diary
    real_mark = sm.mark_staged_applied

    def save(entries):
        order.append("backup")
        return real_save(entries)

    def clear(entries):
        order.append("clear")
        return real_clear(entries)

    def mark(ids):
        order.append("mark")
        return real_mark(ids)

    monkeypatch.setattr(diary, "save_diary_to_logs", save)
    monkeypatch.setattr(sm, "clear_diary", clear)
    monkeypatch.setattr(sm, "mark_staged_applied", mark)

    assert await sm.commit_all(session) is True

    assert "backup" in order and "mark" in order
    assert order.index("backup") < order.index("mark")
    assert order[-1] == "mark", "applied flags must be written last"


# ---------------------------------------------------------------------------
# S6 / S18: commit failure reaches the rollback branch in execute_sleep
# ---------------------------------------------------------------------------

async def test_execute_sleep_rolls_back_when_diary_backup_fails(sleep_env, monkeypatch):
    env = sleep_env
    env.add_diary("alpha diary note")
    before = _snapshot(env.seed_messages(12))
    session = env.begin_sleep()

    async def work(sid):
        await env.stage_summary(sid, claimed=12)
        await env.stage_fact(sid)

    env.use_processes(monkeypatch, work, ok=True)
    monkeypatch.setattr(diary, "save_diary_to_logs", lambda entries: False)

    assert await sm.execute_sleep(session) is False

    assert _snapshot(env.messages()) == before
    assert env.summaries() == []
    assert env.memory_count("alpha fact") == 0
    assert env.diary_count() == 1  # S6: the diary is not lost
    assert get_state() == SleepState.AWAKE


async def test_commit_failure_inside_execute_sleep_takes_rollback_branch(
        sleep_env, monkeypatch):
    env = sleep_env
    env.add_diary("alpha diary note")
    before = _snapshot(env.seed_messages(12))
    session = env.begin_sleep()

    async def work(sid):
        await env.stage_summary(sid, claimed=12)
        await env.stage_fact(sid)

    env.use_processes(monkeypatch, work, ok=True)

    def broken_clear(entries):
        raise OSError("simulated clear failure")

    monkeypatch.setattr(sm, "clear_diary", broken_clear)

    assert await sm.execute_sleep(session) is False

    assert _snapshot(env.messages()) == before
    assert env.summaries() == []
    assert env.memory_count("alpha fact") == 0
    assert env.diary_count() == 1
    assert env.staging(session) == []
    assert get_state() == SleepState.AWAKE


# ---------------------------------------------------------------------------
# S7: clear failure is not committed; retry does not duplicate
# ---------------------------------------------------------------------------

async def test_clear_failure_is_not_committed_and_retry_does_not_duplicate(
        sleep_env, monkeypatch):
    env = sleep_env
    env.add_diary("alpha diary note")
    before = _snapshot(env.seed_messages(12))
    session1 = env.begin_sleep()
    await env.stage_summary(session1, claimed=12)
    await env.stage_fact(session1)

    real_clear = sm.clear_diary

    def broken_clear(entries):
        raise OSError("simulated clear failure")

    monkeypatch.setattr(sm, "clear_diary", broken_clear)

    assert await sm.commit_all(session1) is False
    staged = env.staging(session1)
    assert staged and all(r["applied"] == 0 for r in staged)
    assert env.diary_count() == 1

    assert await sm.recover_from_crash() is True
    assert _snapshot(env.messages()) == before
    assert env.summaries() == []
    assert env.memory_count("alpha fact") == 0

    # Retry on a healthy disk: exactly one summary and one memory afterwards.
    monkeypatch.setattr(sm, "clear_diary", real_clear)
    session2 = env.begin_sleep()
    await env.stage_summary(session2, claimed=12)
    await env.stage_fact(session2)

    assert await sm.commit_all(session2) is True
    assert len(env.summaries()) == 1
    assert env.memory_count("alpha fact") == 1
    assert env.diary_count() == 0


async def test_applied_marking_failure_does_not_lose_diary(sleep_env, monkeypatch):
    env = sleep_env
    env.add_diary("alpha diary note")
    before = _snapshot(env.seed_messages(12))
    session = env.begin_sleep()
    await env.stage_summary(session, claimed=12)

    def broken_mark(ids):  # noqa: ARG001
        raise sqlite3.OperationalError("simulated failure marking staging applied")

    monkeypatch.setattr(sm, "mark_staged_applied", broken_mark)

    assert await sm.commit_all(session) is False
    # S1/S6: an unfinished commit must not take the diary with it.
    assert [e["content_md"] for e in diary.get_all_diary_entries()] == ["alpha diary note"]

    assert await sm.recover_from_crash() is True
    assert _snapshot(env.messages()) == before
    assert env.summaries() == []
    assert env.diary_count() == 1


async def test_commit_is_idempotent_after_success(sleep_env):
    env = sleep_env
    env.add_diary("alpha diary note")
    env.seed_messages(12)
    session = env.begin_sleep()
    await env.stage_summary(session, claimed=12)

    assert await sm.commit_all(session) is True
    assert await sm.commit_all(session) is True

    assert len(env.summaries()) == 1
    assert env.messages() == []
    assert env.diary_count() == 0


# ---------------------------------------------------------------------------
# S8: push + pop atomic; S9 is covered in the summary-guard tests below
# ---------------------------------------------------------------------------

async def test_pop_failure_leaves_no_summary_and_retry_does_not_duplicate(
        sleep_env, context_delete_failures):
    env = sleep_env
    before = _snapshot(env.seed_messages(12))
    session = env.begin_sleep()

    context_delete_failures["on"] = True
    with pytest.raises(sqlite3.OperationalError):
        await env.stage_summary(session, claimed=12)
    context_delete_failures["on"] = False

    # S8: the failed pop must not leave a summary next to the messages it summarizes.
    assert env.summaries() == []
    assert _snapshot(env.messages()) == before

    # Retry of the same step produces exactly one summary and removes the messages.
    await env.stage_summary(session, claimed=12)
    assert len(env.summaries()) == 1
    assert env.messages() == []

    # The whole session can still be undone cleanly, with no duplicated messages.
    await sm.rollback_all(session)
    assert _snapshot(env.messages()) == before
    assert env.summaries() == []


async def test_pop_failure_inside_sleep_rolls_back_and_retry_succeeds(
        sleep_env, monkeypatch, context_delete_failures):
    env = sleep_env
    before = _snapshot(env.seed_messages(12))

    async def work(sid):
        await env.stage_summary(sid, claimed=12)
        await env.stage_fact(sid)

    env.use_processes(monkeypatch, work)

    context_delete_failures["on"] = True
    session1 = env.begin_sleep()
    assert await sm.execute_sleep(session1) is False
    context_delete_failures["on"] = False

    assert _snapshot(env.messages()) == before
    assert env.summaries() == []
    assert env.memory_count("alpha fact") == 0
    assert get_state() == SleepState.AWAKE

    session2 = env.begin_sleep()
    assert await sm.execute_sleep(session2) is True
    assert len(env.summaries()) == 1
    assert env.messages() == []
    assert env.memory_count("alpha fact") == 1


# ---------------------------------------------------------------------------
# S15 / S16: crash window between staging and the destructive insert
# ---------------------------------------------------------------------------

async def test_crash_after_memory_insert_is_undone_by_rollback(sleep_env):
    env = sleep_env
    env.indexer.crash_after_insert = True
    session = env.begin_sleep()

    with pytest.raises(RuntimeError):
        await env.stage_fact(session, content="alpha fact")

    mem_id = env.indexer.calls[0]["mem_id"]
    # The undo record exists and names the row that the crash left behind.
    staged = [r for r in env.staging(session) if r["target_table"] == "memories"]
    assert [r["target_id"] for r in staged] == [mem_id]
    assert mila_db.get_memory(mem_id) is not None

    await sm.rollback_all(session)
    assert mila_db.get_memory(mem_id) is None

    # Retry after recovery does not duplicate the fact.
    env.indexer.crash_after_insert = False
    session2 = env.begin_sleep()
    await env.stage_fact(session2, content="alpha fact")
    assert env.memory_count("alpha fact") == 1


async def test_crash_between_chunk_stage_and_insert_leaves_no_orphan(
        sleep_env, monkeypatch):
    env = sleep_env

    def crash(**kwargs):  # noqa: ARG001
        raise RuntimeError("simulated crash before chunk insert")

    monkeypatch.setattr(sp, "add_chunk", crash)
    session = env.begin_sleep()
    executor = sp.StagingExecutor(session, "dialog_summarization")

    with pytest.raises(RuntimeError):
        await executor.execute_tool_call(env.call(
            "add_memory_chunk", title="alpha title", content_md="alpha body",
            importance=0.6))

    # The undo record is in place, but no chunk row exists yet.
    staged = [r for r in env.staging(session) if r["target_table"] == "mila_memory_chunks"]
    assert len(staged) == 1
    assert _chunk_ids(env) == []

    await sm.rollback_all(session)
    assert env.staging(session) == []
    assert _chunk_ids(env) == []


async def test_staged_chunk_id_is_the_inserted_row_and_rollback_removes_it(sleep_env):
    env = sleep_env
    session = env.begin_sleep()
    executor = sp.StagingExecutor(session, "dialog_summarization")

    await executor.execute_tool_call(env.call(
        "add_memory_chunk", title="alpha title", content_md="alpha body", importance=0.6))

    staged = [r for r in env.staging(session) if r["target_table"] == "mila_memory_chunks"]
    assert len(staged) == 1
    chunk_id = staged[0]["target_id"]
    # The id the undo record points at is the id of the row that exists: no duplicates.
    assert _chunk_ids(env) == [chunk_id]

    await sm.rollback_all(session)
    assert _chunk_ids(env) == []


# ---------------------------------------------------------------------------
# Crash recovery entry point
# ---------------------------------------------------------------------------

async def test_recovery_of_interrupted_sleep_rolls_back_compression(sleep_env):
    env = sleep_env
    before = _snapshot(env.seed_messages(12))
    session = env.begin_sleep()
    await env.stage_summary(session, claimed=12)

    # The process died while the sleep was still in 'sleeping'.
    assert await sm.recover_from_crash() is True

    assert _snapshot(env.messages()) == before
    assert env.summaries() == []
    assert get_state() == SleepState.AWAKE


# ---------------------------------------------------------------------------
# M2: crash between diary clear and "applied" marking must not lose the diary
# ---------------------------------------------------------------------------

class SimulatedCrash(BaseException):
    """Stands in for a process death: not an Exception, so no handler swallows it."""


async def test_crash_between_clear_and_mark_restores_diary_from_staged_snapshot(
        sleep_env, monkeypatch):
    env = sleep_env
    env.add_diary("alpha diary note")
    before = _snapshot(env.seed_messages(12))
    session = env.begin_sleep()
    await env.stage_summary(session, claimed=12)
    await env.stage_fact(session)

    def crash(ids):  # noqa: ARG001
        raise SimulatedCrash("process died after the diary clear")

    monkeypatch.setattr(sm, "mark_staged_applied", crash)

    with pytest.raises(SimulatedCrash):
        await sm.commit_all(session)

    # The clear already happened, and the durable undo record holds the rows.
    assert env.diary_count() == 0
    snapshot_rows = [r for r in env.staging(session) if r["target_table"] == "diary_entries"]
    assert len(snapshot_rows) == 1
    assert snapshot_rows[0]["applied"] == 0
    assert get_state() == SleepState.COMMITTING

    assert await sm.recover_from_crash() is True

    assert [e["content_md"] for e in diary.get_all_diary_entries()] == ["alpha diary note"]
    assert _snapshot(env.messages()) == before
    assert env.summaries() == []
    assert env.memory_count("alpha fact") == 0
    assert env.staging(session) == []
    assert get_state() == SleepState.AWAKE
