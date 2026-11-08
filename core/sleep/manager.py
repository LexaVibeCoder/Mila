"""
core/sleep/manager.py — Mila's Sleep Manager (Orchestrator).
"""

import asyncio
import json
import sqlite3
import time
import os
from datetime import datetime, timedelta, timezone
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


def _parse_iso(raw) -> datetime | None:
    """Parse DB timestamps (naive → assume UTC). None on failure."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _seconds_until_deadline(deadline_iso: str | None) -> float | None:
    """Wall-clock seconds until scheduled_end_at. None = indefinite/no deadline."""
    dt = _parse_iso(deadline_iso)
    if dt is None:
        return None
    return (dt - datetime.now(timezone.utc)).total_seconds()


def _presence_to_rest(note: str) -> None:
    """Best-effort presence sync on wake: sleep ended → back to rest.

    ``journal=False``: waking is journaled once by ``_record_sleep_wake`` with
    the real duration. A second "перешла в rest" line would only add noise.
    """
    try:
        from core.presence import get_presence, enter_rest
        if get_presence().get("state") == "sleep":
            enter_rest(note, journal=False)
    except Exception:
        pass


def _ensure_presence_sleep(note: str) -> None:
    """Best-effort presence sync on sleep start (keeps a custom note if set)."""
    try:
        from core.presence import get_presence, enter_sleep_note
        if get_presence().get("state") != "sleep":
            enter_sleep_note(note)
    except Exception:
        pass


def _presence_snapshot() -> dict:
    try:
        from core.presence import get_presence
        return get_presence()
    except Exception:
        return {"state": "rest", "focus_chat_id": None,
                "focus_user_name": None, "note": "", "updated_at": None}


def _record_sleep_wake(started_at: datetime | None, source: str) -> None:
    """Journal one wake event with the real wall-clock sleep duration."""
    if started_at is None:
        return
    try:
        from core.action_journal import record_sleep_wake
        now = datetime.now(timezone.utc)
        duration = max(0.0, (now - started_at).total_seconds())
        record_sleep_wake(duration, now=now, source=source)
    except Exception as e:
        print(f"[Sleep] journal wake failed: {e}")


async def _resume_sleep_until_deadline(session_id: str, deadline_iso: str):
    """Crash-resume for the dormant phase: processes already done → wait
    wall-clock until the deadline, then commit. No timer to freeze: the
    deadline lives in the DB."""
    try:
        secs = _seconds_until_deadline(deadline_iso)
        if secs is not None and secs > 0:
            print(f"[Recovery] Resuming sleep until {deadline_iso} ({secs:.0f}s left)")
            await asyncio.sleep(secs)
        cur = get_current_state()
        if cur.get("state") == "sleeping" and cur.get("session_id") == session_id:
            print("[Recovery] Deadline reached → committing resumed sleep")
            await commit_all(session_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[Recovery] resume error: {e}")


async def recover_from_crash() -> bool:
    global _current_session_id, _sleep_in_progress, _sleep_start_time, _sleep_duration
    _ensure_sleep_tables()
    current = get_current_state()
    state = current.get("state", "awake")
    session_id = current.get("session_id")

    if state == "awake":
        return False

    print(f"\n{'='*60}")
    print(f"  CRASH RECOVERY DETECTED")
    print(f"  Previous state: {state}")
    print(f"  Session: {session_id}")
    print(f"{'='*60}\n")

    if state == "sleeping":
        # Wall-clock deadline from the DB is authoritative, not the
        # in-memory asyncio timer (which dies with the process).
        # Dormant phase marker: execute_sleep sets stage idle/90% right
        # before the long wait; anything else means the crash hit mid
        # processing → roll back (safe), otherwise resume waiting.
        stage = current.get("current_stage", "")
        try:
            progress = float(current.get("progress_pct") or 0.0)
        except (TypeError, ValueError):
            progress = 0.0
        dormant = (stage == SleepStage.IDLE.value) and progress >= 89.0
        scheduled_end = current.get("scheduled_end_at")
        secs_left = _seconds_until_deadline(scheduled_end)

        if dormant and (secs_left is None or secs_left > 0):
            # Processes already done → keep staged ops, wait for deadline.
            _current_session_id = session_id
            _sleep_in_progress = True
            started = _parse_iso(current.get("started_at"))
            _sleep_start_time = started.timestamp() if started else time.time()
            end = _parse_iso(scheduled_end)
            if started is not None and end is not None:
                _sleep_duration = max(0.0, (end - started).total_seconds() / 60.0)
            else:
                _sleep_duration = None
            _ensure_presence_sleep(f"resumed {(session_id or '')[:8]}")
            if secs_left is not None:
                asyncio.create_task(_resume_sleep_until_deadline(session_id, scheduled_end))
                print(f"[Recovery] Dormant sleep resumed ({secs_left:.0f}s until deadline)")
            else:
                print("[Recovery] Indefinite sleep resumed (until manual wake)")
            return True

        if dormant and secs_left is not None and secs_left <= 0:
            # Deadline passed while offline → finish the sleep.
            print("[Recovery] Deadline passed while offline → committing")
            await commit_all(session_id)
            return True

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
            _record_sleep_wake(_parse_iso(current.get("started_at")), "recovery")
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
    before_rollback = get_current_state()
    sleep_started = _parse_iso(before_rollback.get("started_at"))
    was_sleeping = before_rollback.get("state") in (
        "sleeping", "committing", "sleep_error")

    if not session_id:
        session_id = before_rollback.get("session_id")

    if not session_id:
        print("[Rollback] No session_id to rollback")
        set_state(SleepState.AWAKE)
        _presence_to_rest("sleep rollback")
        if was_sleeping:
            _record_sleep_wake(sleep_started, "rollback")
        return

    print(f"[Rollback] Rolling back session {session_id[:8]}...")

    staged = get_staged_operations(session_id)

    from core.db import get_conn
    conn = get_conn()
    rolled_back_faiss_ids: list[int] = []
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
                        row = conn.execute(
                            "SELECT faiss_id FROM memories WHERE mem_id=?",
                            (target_id,)
                        ).fetchone()
                        if row and row["faiss_id"] is not None:
                            rolled_back_faiss_ids.append(int(row["faiss_id"]))
                        conn.execute(
                            "DELETE FROM memories WHERE mem_id=?",
                            (target_id,)
                        )

                elif target_table == "users":
                    # Fact extraction may have created a users row for a new
                    # id. Remove it only if nothing else references it now;
                    # profiles that existed before the session are untouched.
                    if op_type == "insert":
                        has_refs = False
                        for table, col in (
                            ("memories", "user_id"),
                            ("chunks", "user_id"),
                            ("chat_members", "user_id"),
                            ("user_attitude_history", "user_id"),
                            ("media_logs", "user_id"),
                        ):
                            try:
                                if conn.execute(
                                    f"SELECT 1 FROM {table} WHERE {col}=? LIMIT 1",
                                    (target_id,),
                                ).fetchone():
                                    has_refs = True
                                    break
                            except sqlite3.OperationalError:
                                continue
                        if not has_refs:
                            conn.execute(
                                "DELETE FROM users WHERE user_id=?", (target_id,))

            except Exception as e:
                print(f"[Rollback] Error processing op {op['id']}: {e}")

        conn.commit()
    finally:
        conn.close()

    # Rows staged during sleep were indexed immediately; rollback must drop
    # their vectors too, otherwise ntotal drifts from SQLite.
    if rolled_back_faiss_ids:
        try:
            from core.memory.indexer import get_indexer
            get_indexer().vector.remove_ids(rolled_back_faiss_ids)
        except Exception as e:
            print(f"[Rollback] FAISS cleanup failed: {e}")

    clear_staged_operations(session_id)
    # Always back up before clearing — a crash must not silently drop the diary.
    if save_diary_to_logs():
        clear_diary()
    else:
        print("[Rollback] WARNING: diary backup failed — keeping entries")
    set_state(SleepState.AWAKE)
    _presence_to_rest("sleep rollback")
    if was_sleeping:
        _record_sleep_wake(sleep_started, "rollback")
    print(f"[Rollback] Complete")


async def commit_all(session_id: str) -> bool:
    print(f"\n{'='*60}")
    print(f"  COMMITTING SLEEP CHANGES")
    print(f"{'='*60}\n")

    # Capture the session start before COMMITTING overwrites the state: the
    # wake event needs the real wall-clock duration.
    before_commit = get_current_state()
    sleep_started = _parse_iso(before_commit.get("started_at"))
    was_sleeping = before_commit.get("state") in ("sleeping", "committing")

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
    _presence_to_rest("sleep complete")
    if was_sleeping:
        _record_sleep_wake(sleep_started, "commit")

    print(f"[Commit] Sleep session {session_id[:8]}... committed successfully")
    return True


async def execute_sleep(session_id: str, duration_minutes: int = None) -> bool:
    global _sleep_in_progress

    try:
        _sleep_in_progress = True

        all_success, message, process_results = await run_all_processes(session_id)

        if all_success and duration_minutes:
            remaining = max(0, duration_minutes * 60 - (time.time() - _sleep_start_time))
            # Wall-clock deadline from the DB is authoritative (survives restarts).
            try:
                db_left = _seconds_until_deadline(
                    get_current_state().get("scheduled_end_at"))
                if db_left is not None:
                    remaining = max(0.0, db_left)
            except Exception:
                pass
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


async def start_sleep(duration_minutes: int = None, triggered_by: str = "manual",
                      note: str = "") -> Dict[str, Any]:
    global _sleep_in_progress, _current_session_id, _sleep_start_time, _sleep_duration

    # Snapshot BEFORE presence is switched to sleep: the journal needs to know
    # where Mila was (rest/working/dialog) and for how long.
    prev_presence = _presence_snapshot()
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
    # Single source of truth for "Mila sleeps": sleep_state row + presence.
    # Centralized here so manual (/sleep API), auto and tool paths agree.
    _ensure_presence_sleep(
        note or (f"{duration_minutes} min" if duration_minutes else "until woken"))

    # Journal the start once, here — this is the only place that knows the
    # real planned duration and the previous presence. Never fail sleep because
    # of the journal.
    try:
        from core.action_journal import record_sleep_start
        record_sleep_start(duration_minutes, prev_presence,
                           note=note, source=triggered_by)
    except Exception as e:
        print(f"[Sleep] journal start failed: {e}")

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

    # Wall-clock first (survives restarts); in-memory only as fallback.
    elapsed = None
    remaining = None
    if state_data.get("state") == "sleeping":
        started = _parse_iso(state_data.get("started_at"))
        if started is not None:
            elapsed = max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
        elif _sleep_start_time:
            elapsed = time.time() - _sleep_start_time
        db_left = _seconds_until_deadline(state_data.get("scheduled_end_at"))
        if db_left is not None:
            remaining = max(0.0, db_left)
        elif _sleep_duration and elapsed is not None:
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
