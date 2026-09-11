"""
core/sleep/manager.py — Mila's Sleep Manager (Orchestrator).
"""

import asyncio
import json
import time
import os
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple, Callable

from config import settings
from core.sleep.state import (
    SleepState, SleepStage, get_state, get_current_state, set_state, set_stage,
    get_queued_messages, mark_messages_processed, queue_message,
    get_staged_operations, mark_staged_applied, clear_staged_operations,
    get_queue_count, _ensure_sleep_tables
)
from core.diary.manager import clear_diary, save_diary_to_logs, get_diary_markdown
from core.sleep.processes import run_all_processes
from core.needs.tracker import get_tracker as get_needs_tracker


_sleep_in_progress = False
_current_session_id = None
_sleep_start_time = None
_sleep_duration = None
_auto_sleep_task = None


async def recover_from_crash() -> bool:
    global _current_session_id, _sleep_in_progress, _sleep_start_time
    _ensure_sleep_tables()
    current = get_current_state()
    state = current.get("state", "awake")
    session_id = current.get("session_id")
    # Restore in-memory session so queue_incoming_message doesn't drop
    # messages right after a restart while DB still says sleeping.
    if state == "sleeping" and session_id:
        _current_session_id = session_id
        _sleep_in_progress = True

    if state == "awake":
        return False

    print(f"\n{'='*60}")
    print(f"  CRASH RECOVERY DETECTED")
    print(f"  Previous state: {state}")
    print(f"  Session: {session_id}")
    print(f"{'='*60}\n")

    if state == "sleeping":
        print("[Recovery] Sleep was in progress → rolling back all changes")
        await rollback_all(session_id)
        print("[Recovery] Rollback complete. System is now AWAKE.")
        return True

    elif state == "committing":
        print("[Recovery] Commit was in progress → checking if changes were applied")
        staged = get_staged_operations(session_id) if session_id else []
        if not staged:
            print("[Recovery] All changes were committed. Resuming normal operation.")
            set_state(SleepState.AWAKE)
            return True
        else:
            print(f"[Recovery] {len(staged)} unapplied operations found → rolling back")
            await rollback_all(session_id)
            return True

    elif state == "sleep_error":
        print("[Recovery] Sleep ended with error → rolling back")
        await rollback_all(session_id)
        return True

    set_state(SleepState.AWAKE)
    return True


async def rollback_all(session_id: str = None):
    if not session_id:
        current = get_current_state()
        session_id = current.get("session_id")

    if not session_id:
        print("[Rollback] No session_id to rollback")
        set_state(SleepState.AWAKE)
        return

    print(f"[Rollback] Rolling back session {session_id[:8]}...")

    staged = get_staged_operations(session_id)

    from core.db import get_conn
    conn = get_conn()
    try:
        for op in reversed(staged):
            try:
                payload = json.loads(op["payload"])
                op_type = op["operation_type"]
                target_table = op["target_table"]
                target_id = op["target_id"]

                if target_table == "mila_memory_chunks":
                    if op_type == "insert":
                        conn.execute(
                            "DELETE FROM mila_memory_chunks WHERE chunk_id=?",
                            (target_id,)
                        )
                    elif op_type == "update":
                        print(f"[Rollback] Update on {target_id[:8]}... — cannot auto-revert, needs review")
                    elif op_type == "delete":
                        conn.execute(
                            "UPDATE mila_memory_chunks SET is_active=1 WHERE chunk_id=?",
                            (target_id,)
                        )

                elif target_table == "memories":
                    if op_type == "insert":
                        conn.execute(
                            "DELETE FROM memories WHERE mem_id=?",
                            (target_id,)
                        )

            except Exception as e:
                print(f"[Rollback] Error processing op {op['id']}: {e}")

        conn.commit()
    finally:
        conn.close()

    clear_staged_operations(session_id)
    # Always back up before clearing — a crash must not silently drop the diary.
    if save_diary_to_logs():
        clear_diary()
    else:
        print("[Rollback] WARNING: diary backup failed — keeping entries")
    set_state(SleepState.AWAKE)
    print(f"[Rollback] Complete")


async def commit_all(session_id: str) -> bool:
    print(f"\n{'='*60}")
    print(f"  COMMITTING SLEEP CHANGES")
    print(f"{'='*60}\n")

    set_state(SleepState.COMMITTING)

    staged = get_staged_operations(session_id)
    staging_ids = [op["id"] for op in staged]

    if staging_ids:
        mark_staged_applied(staging_ids)
        print(f"[Commit] {len(staging_ids)} operations marked as applied")

    # Always back up before clearing. diary.save_processed is kept for API
    # compatibility but no longer gates the backup — clearing without a
    # backup is silent data loss.
    if save_diary_to_logs():
        print("[Commit] Diary saved to logs")
        clear_diary()
        print("[Commit] Diary cleared")
    else:
        print("[Commit] WARNING: diary backup failed — keeping entries")

    set_state(SleepState.AWAKE)

    print(f"[Commit] Sleep session {session_id[:8]}... committed successfully")
    return True


async def execute_sleep(session_id: str, duration_minutes: int = None) -> bool:
    global _sleep_in_progress

    try:
        _sleep_in_progress = True

        all_success, message, process_results = await run_all_processes(session_id)

        if all_success and duration_minutes:
            elapsed = time.time() - _sleep_start_time
            remaining = max(0, duration_minutes * 60 - elapsed)
            if remaining > 0:
                set_stage(SleepStage.IDLE, progress_pct=90.0)
                print(f"[Sleep] Processing done, staying asleep for {remaining:.0f}s more...")
                await asyncio.sleep(remaining)

        print(f"\n{'='*60}")
        print(f"  SLEEP RESULTS")
        for pr in process_results:
            print(f"  • {pr}")
        print(f"  Overall: {'✅ SUCCESS' if all_success else '❌ FAILURE'}")
        print(f"{'='*60}\n")

        if all_success:
            commit_success = await commit_all(session_id)
            if commit_success:
                print("[Sleep] ✅ Sleep completed successfully")
                return True
            else:
                print("[Sleep] ❌ Commit failed — rolling back")
                await rollback_all(session_id)
                return False
        else:
            print("[Sleep] ❌ Some processes failed — rolling back")
            await rollback_all(session_id)
            return False

    except Exception as e:
        print(f"[Sleep] ❌ Critical error during sleep: {e}")
        import traceback
        traceback.print_exc()
        set_state(SleepState.SLEEP_ERROR, error_message=str(e))
        await rollback_all(session_id)
        return False

    finally:
        _sleep_in_progress = False


async def start_sleep(duration_minutes: int = None, triggered_by: str = "manual") -> Dict[str, Any]:
    global _sleep_in_progress, _current_session_id, _sleep_start_time, _sleep_duration

    current_state = get_state()
    if current_state == SleepState.SLEEPING:
        return {
            "success": False,
            "message": "Mila is already sleeping",
            "session_id": _current_session_id
        }

    if current_state == SleepState.COMMITTING:
        return {
            "success": False,
            "message": "Mila is currently committing changes, cannot start sleep",
            "session_id": None
        }

    try:
        from Mila_observatory.runtime import runtime_config
        forced_awake = runtime_config.get("sleep.forced_awake", False)
    except ImportError:
        forced_awake = False

    if forced_awake:
        return {
            "success": False,
            "message": "Sleep is disabled (forced_awake is ON). Turn it off in Observatory to allow sleep.",
            "session_id": None
        }

    if triggered_by == "manual" and _auto_sleep_task and not _auto_sleep_task.done():
        _auto_sleep_task.cancel()
        print("[Sleep] Manual sleep triggered, cancelling pending auto-sleep")

    import uuid
    session_id = str(uuid.uuid4())
    _current_session_id = session_id
    _sleep_start_time = time.time()
    _sleep_duration = duration_minutes

    set_state(SleepState.SLEEPING, session_id=session_id, triggered_by=triggered_by,
              duration_minutes=duration_minutes)

    # Reset needs tracker offsets to avoid double-counting during sleep
    get_needs_tracker().reset_offsets()

    print(f"\n{'='*60}")
    print(f"  💤 MILA IS GOING TO SLEEP")
    print(f"  Session: {session_id[:8]}...")
    print(f"  Duration: {duration_minutes if duration_minutes else 'until manually woken'} min")
    print(f"  Triggered by: {triggered_by}")
    print(f"{'='*60}\n")

    try:
        from Mila_observatory.api.dashboard import add_event
        add_event("sleep", f"Sleep started (session: {session_id[:8]}...){', duration: ' + str(duration_minutes) + ' min' if duration_minutes else ''}")
    except ImportError:
        pass

    asyncio.create_task(_run_sleep_with_timeout(session_id, duration_minutes))

    return {
        "success": True,
        "message": "Sleep started",
        "session_id": session_id,
        "duration_minutes": duration_minutes
    }


async def _run_sleep_with_timeout(session_id: str, duration_minutes: int = None):
    try:
        if duration_minutes:
            sleep_task = asyncio.create_task(execute_sleep(session_id, duration_minutes))

            try:
                await asyncio.wait_for(
                    sleep_task,
                    timeout=duration_minutes * 60 + 300
                )
            except asyncio.TimeoutError:
                current = get_current_state()
                if current.get("state") == "committing":
                    print("[Sleep] Timeout reached during commit — waiting for commit to finish")
                    await sleep_task
                else:
                    print("[Sleep] Timeout reached — force waking up")
                    await wake_up()
        else:
            await execute_sleep(session_id)

    except asyncio.CancelledError:
        print("[Sleep] Sleep task cancelled")
        current = get_current_state()
        if current.get("state") != "committing":
            await rollback_all(session_id)

    except Exception as e:
        print(f"[Sleep] Error in sleep task: {e}")
        import traceback
        traceback.print_exc()


async def wake_up() -> Dict[str, Any]:
    global _sleep_in_progress, _current_session_id

    current_state = get_state()
    session_id = _current_session_id
    if not session_id:
        try:
            session_id = get_current_state().get("session_id")
            _current_session_id = session_id
        except Exception:
            session_id = None

    if current_state == SleepState.AWAKE:
        return {"success": True, "message": "Mila is already awake"}

    if current_state == SleepState.COMMITTING:
        return {"success": False, "message": "Mila is committing changes, cannot wake until commit completes"}

    if current_state == SleepState.SLEEPING:
        print(f"\n{'='*60}")
        print(f"  ⏰ MILA IS BEING WOKEN UP")
        print(f"{'='*60}\n")

        await rollback_all(session_id)
        _sleep_in_progress = False
        _current_session_id = None

        try:
            from Mila_observatory.api.dashboard import add_event
            add_event("wake", "Mila was woken up manually")
        except ImportError:
            pass

        return {"success": True, "message": "Mila woke up"}

    return {"success": False, "message": f"Unknown state: {current_state}"}


def is_sleeping() -> bool:
    return get_state() == SleepState.SLEEPING


def is_committing() -> bool:
    return get_state() == SleepState.COMMITTING


def get_sleep_status() -> Dict[str, Any]:
    state_data = get_current_state()
    queue_count = get_queue_count()

    elapsed = None
    if _sleep_start_time and state_data.get("state") == "sleeping":
        elapsed = time.time() - _sleep_start_time

    remaining = None
    if _sleep_duration and elapsed is not None:
        remaining = max(0, _sleep_duration * 60 - elapsed)

    return {
        "state": state_data.get("state", "awake"),
        "session_id": state_data.get("session_id"),
        "current_stage": state_data.get("current_stage", "idle"),
        "progress_pct": state_data.get("progress_pct", 0.0),
        "started_at": state_data.get("started_at"),
        "scheduled_end_at": state_data.get("scheduled_end_at"),
        "completed_at": state_data.get("completed_at"),
        "triggered_by": state_data.get("triggered_by", "manual"),
        "error_message": state_data.get("error_message"),
        "queue_count": queue_count,
        "elapsed_seconds": elapsed,
        "remaining_seconds": remaining,
        "in_progress": _sleep_in_progress
    }


async def auto_sleep_scheduler():
    while True:
        try:
            await _check_auto_sleep()
        except Exception as e:
            print(f"[AutoSleep] Error: {e}")

        await asyncio.sleep(60)


async def _check_auto_sleep():
    try:
        from Mila_observatory.runtime import runtime_config

        start_hour = runtime_config.get("sleep.start_hour")
        end_hour = runtime_config.get("sleep.end_hour")
        forced_awake = runtime_config.get("sleep.forced_awake", False)
    except ImportError:
        return

    if forced_awake:
        return

    if start_hour is None or end_hour is None:
        return

    now = datetime.now()
    current_hour = now.hour

    should_sleep = False
    if start_hour <= end_hour:
        if start_hour <= current_hour < end_hour:
            should_sleep = True
    else:
        if current_hour >= start_hour or current_hour < end_hour:
            should_sleep = True

    if should_sleep:
        current_state = get_state()
        if current_state == SleepState.AWAKE:
            print(f"[AutoSleep] Time to sleep (hour {current_hour})")
            await start_sleep(duration_minutes=None, triggered_by="automatic")


def start_auto_sleep_scheduler():
    global _auto_sleep_task
    _auto_sleep_task = asyncio.create_task(auto_sleep_scheduler())
    print("[AutoSleep] Scheduler started")


def queue_incoming_message(chat_id: str, user_id: str, content: str):
    global _current_session_id
    if not _current_session_id:
        # After a restart in-memory session is None while DB still says
        # sleeping — recover it instead of silently dropping the message.
        try:
            current = get_current_state()
            recovered = current.get("session_id")
            if recovered:
                _current_session_id = recovered
                print(f"[SleepQueue] Recovered session {recovered[:8]}... after restart")
        except Exception:
            pass
    if not _current_session_id:
        print("[SleepQueue] WARNING: sleeping but no session — message kept in chunks/buffer, not in sleep queue")
        return

    queue_message(_current_session_id, chat_id, user_id, content)
    print(f"[SleepQueue] Message queued from {user_id[:8]}... in {chat_id[:8]}...")


def take_unprocessed_sleep_queue(session_id: str | None = None) -> list[dict]:
    """Drain helper for startup/replay: returns unprocessed rows without marking.

    Caller re-injects them into MessageCoordinate then marks processed.
    """
    try:
        return get_queued_messages(session_id)
    except Exception:
        return []


def get_pending_messages_count() -> int:
    return get_queue_count()
