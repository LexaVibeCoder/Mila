import asyncio
from datetime import datetime, timedelta

import pytest

from core.sleep import manager, processes, state
from Mila_observatory.api.sleep import SleepWakeRequest, api_sleep_status, api_wake_up


async def _wait_until_stage(stage):
    async with asyncio.timeout(2):
        while manager.get_sleep_status()["current_stage"] != stage:
            await asyncio.sleep(0.005)


@pytest.mark.parametrize("duration", [30, None])
async def test_observatory_wake_commits_dormant_sleep(sleep_env, monkeypatch, duration):
    monkeypatch.setattr(manager, "_WAKE_POLL_SECONDS", 0.01)
    session = sleep_env.begin_sleep(duration_minutes=duration)
    sleep_env.add_diary("alpha diary note")

    async def work(session_id):
        await sleep_env.stage_fact(session_id, content="alpha fact")

    sleep_env.use_processes(monkeypatch, work)
    task = asyncio.create_task(manager.execute_sleep(session))
    try:
        await _wait_until_stage("dormant")
        status = await api_sleep_status()
        assert status.progress_pct == 100
        assert not status.in_progress
        if duration is None:
            assert status.remaining_seconds is None
        else:
            assert status.remaining_seconds > 0

        # A stale rollback button from the reflection phase cannot revert finished work.
        stale = await api_wake_up(SleepWakeRequest(session_id=session, mode="rollback"))
        assert not stale.success
        result = await api_wake_up(SleepWakeRequest(session_id=session, mode="keep"))
        assert result.success
        assert (await api_sleep_status()).wake_requested
        assert await asyncio.wait_for(task, 2)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert sleep_env.memory_count("alpha fact") == 1
    assert sleep_env.diary_count() == 0
    assert all(row["applied"] for row in sleep_env.staging(session))
    status = await api_sleep_status()
    assert status.state == "awake"
    assert status.session_id is None
    assert status.scheduled_end_at is None


async def test_reflection_reports_parallel_phases_and_can_cancel(sleep_env, monkeypatch):
    monkeypatch.setattr(manager, "_WAKE_POLL_SECONDS", 0.01)
    session = sleep_env.begin_sleep(duration_minutes=None)
    diary_started = asyncio.Event()
    fact_started = asyncio.Event()
    cancelled = asyncio.Event()
    wait = asyncio.Event()

    async def diary(session_id):
        await sleep_env.stage_fact(session_id)
        state.set_process_status(session_id, "diary_analysis", "verifying")
        diary_started.set()
        try:
            await wait.wait()
        finally:
            cancelled.set()

    async def dialog(session_id):
        return True, "done"

    async def facts(session_id):
        fact_started.set()
        await wait.wait()

    monkeypatch.setattr(processes, "process_diary_analysis", diary)
    monkeypatch.setattr(processes, "process_dialog_summarization", dialog)
    monkeypatch.setattr(processes, "process_user_fact_extraction", facts)

    task = asyncio.create_task(manager.execute_sleep(session))
    try:
        await asyncio.wait_for(diary_started.wait(), 2)
        await asyncio.wait_for(fact_started.wait(), 2)
        status = await api_sleep_status()
        assert status.current_stage == "reflection"
        assert status.in_progress
        assert status.processes == {
            "diary_analysis": "verifying",
            "dialog_summarization": "completed",
            "user_fact_extraction": "running",
        }
        assert not (await manager.wake_up(session, "keep"))["success"]
        assert not (await manager.wake_up("previous-session", "rollback"))["success"]
        assert (await manager.wake_up(session, "rollback"))["success"]
        assert not await asyncio.wait_for(task, 2)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert cancelled.is_set()
    assert sleep_env.memory_count("alpha fact") == 0
    assert state.get_state() == state.SleepState.AWAKE


async def test_recovery_accepts_wake_of_legacy_dormant_sleep(sleep_env, monkeypatch):
    monkeypatch.setattr(manager, "_WAKE_POLL_SECONDS", 0.01)
    session = sleep_env.begin_sleep(duration_minutes=None)
    await sleep_env.stage_fact(session)
    conn = state.get_conn()
    try:
        conn.execute("ALTER TABLE sleep_state DROP COLUMN processes_json")
        conn.execute("ALTER TABLE sleep_state DROP COLUMN wake_requested")
        conn.execute("UPDATE sleep_state SET current_stage='idle', progress_pct=90 WHERE id=1")
        conn.commit()
    finally:
        conn.close()

    assert (await api_sleep_status()).current_stage == "dormant"
    assert await manager.recover_from_crash()
    assert (await manager.wake_up(session, "keep"))["success"]
    await _wait_until_stage("idle")
    assert sleep_env.memory_count("alpha fact") == 1
    assert all(row["applied"] for row in sleep_env.staging(session))


def test_wake_request_wins_before_dormant_transition(sleep_env):
    session = sleep_env.begin_sleep(duration_minutes=None)
    assert state.request_wake(session, "reflection")
    assert not state.finish_reflection(session)


def test_only_one_waiter_can_claim_commit(sleep_env):
    session = sleep_env.begin_sleep(duration_minutes=None)
    assert state.finish_reflection(session)
    assert state.set_state(state.SleepState.COMMITTING, session_id=session)
    assert state.get_current_state()["current_stage"] == "committing"
    assert not state.set_state(state.SleepState.COMMITTING, session_id=session)


async def test_auto_sleep_waits_for_next_window_after_wake(sleep_env, monkeypatch):
    from Mila_observatory.runtime import runtime_config

    now = datetime(2025, 1, 2, 2).astimezone()
    completed = (now - timedelta(minutes=30)).isoformat()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    config = {"sleep.start_hour": 22, "sleep.end_hour": 7}
    calls = []

    async def start_sleep(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(runtime_config, "get", config.get)
    monkeypatch.setattr(manager, "datetime", Clock)
    monkeypatch.setattr(manager, "start_sleep", start_sleep)
    monkeypatch.setattr(manager, "get_current_state", lambda: {
        "state": "awake", "completed_at": completed,
    })

    await manager._check_auto_sleep()
    assert calls == []

    now += timedelta(days=1)
    await manager._check_auto_sleep()
    assert calls == [{"duration_minutes": 300, "triggered_by": "automatic"}]
