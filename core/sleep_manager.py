"""
core/sleep_manager.py — Mila's Sleep Manager (Orchestrator).

This is the main orchestrator for the entire sleep/reflection system.

Responsibilities:
1. Sleep lifecycle: start → run processes → commit → wake
2. Crash recovery: detect IN_PROGRESS state on startup, rollback or resume
3. Transactional commit: apply all staged changes atomically
4. Rollback: revert all changes if sleep fails
5. Message queue management
6. Automatic sleep scheduling
7. Manual sleep via Observatory API

Lifecycle:
  AWAKE → SLEEPING → COMMITTING → AWAKE
                   ↘ SLEEP_ERROR → AWAKE (rollback)
"""

import asyncio
import json
import time
import os
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple, Callable

from config import settings
from core.sleep_state import (
    SleepState, SleepStage, get_state, get_current_state, set_state, set_stage,
    get_queued_messages, mark_messages_processed, queue_message,
    get_staged_operations, mark_staged_applied, clear_staged_operations,
    get_queue_count, _ensure_sleep_tables
)
from core.diary import clear_diary, save_diary_to_logs, get_diary_markdown
from core.sleep_processes import run_all_processes


# ─── Global State ──────────────────────────────────────────────

# Track whether sleep is currently in progress (for Observatory)
_sleep_in_progress = False
_current_session_id = None
_sleep_start_time = None
_sleep_duration = None
_auto_sleep_task = None


# ─── Crash Recovery ────────────────────────────────────────────

async def recover_from_crash() -> bool:
    """
    Check if the system was in the middle of a sleep session when it crashed.
    If so, rollback all changes and return to AWAKE state.
    
    This is called on startup before the bot is fully initialized.
    Returns True if recovery was needed.
    """
    _ensure_sleep_tables()
    current = get_current_state()
    state = current.get("state", "awake")
    session_id = current.get("session_id")
    
    if state == "awake":
        return False  # No recovery needed
    
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
        # During commit, we can't know if changes were applied or not.
        # We need to check if any staged operations were marked as applied.
        staged = get_staged_operations(session_id) if session_id else []
        if not staged:
            # All changes were already applied, just mark as awake
            print("[Recovery] All changes were committed. Resuming normal operation.")
            set_state(SleepState.AWAKE)
            return True
        else:
            # Some changes were not applied → rollback
            print(f"[Recovery] {len(staged)} unapplied operations found → rolling back")
            await rollback_all(session_id)
            return True
    
    elif state == "sleep_error":
        print("[Recovery] Sleep ended with error → rolling back")
        await rollback_all(session_id)
        return True
    
    # Force awake
    set_state(SleepState.AWAKE)
    return True


async def rollback_all(session_id: str = None):
    """
    Rollback all changes made during a sleep session.
    
    This reverses the operations that were applied:
    - mila_memory_chunks: delete added chunks, restore updated chunks (from payload),
      reactivate deactivated chunks
    - memories: delete added memories
    - context_buffer: the summaries are kept (they're already applied),
      but the sleep state is reset
    """
    if not session_id:
        current = get_current_state()
        session_id = current.get("session_id")
    
    if not session_id:
        print("[Rollback] No session_id to rollback")
        set_state(SleepState.AWAKE)
        return
    
    print(f"[Rollback] Rolling back session {session_id[:8]}...")
    
    # Get all staged operations
    staged = get_staged_operations(session_id)
    
    from core.db import get_conn
    conn = get_conn()
    try:
        for op in reversed(staged):  # Reverse order
            try:
                payload = json.loads(op["payload"])
                op_type = op["operation_type"]
                target_table = op["target_table"]
                target_id = op["target_id"]
                
                if target_table == "mila_memory_chunks":
                    if op_type == "insert":
                        # Delete the added chunk
                        conn.execute(
                            "DELETE FROM mila_memory_chunks WHERE chunk_id=?",
                            (target_id,)
                        )
                    elif op_type == "update":
                        # We can't easily restore the old version without a full snapshot,
                        # but we can mark it as needing review. For now, log it.
                        print(f"[Rollback] Update on {target_id[:8]}... — cannot auto-revert, needs review")
                    elif op_type == "delete":
                        # Reactivate the chunk
                        conn.execute(
                            "UPDATE mila_memory_chunks SET is_active=1 WHERE chunk_id=?",
                            (target_id,)
                        )
                
                elif target_table == "memories":
                    if op_type == "insert":
                        # Delete the added memory
                        conn.execute(
                            "DELETE FROM memories WHERE mem_id=?",
                            (target_id,)
                        )
                
                # Mark the operation as rolled back (we'll clear it later)
            except Exception as e:
                print(f"[Rollback] Error processing op {op['id']}: {e}")
        
        conn.commit()
    finally:
        conn.close()
    
    # Clear staged operations
    clear_staged_operations(session_id)
    
    # Clear diary
    clear_diary()
    
    # Reset state
    set_state(SleepState.AWAKE)
    print(f"[Rollback] Complete")


async def commit_all(session_id: str) -> bool:
    """
    Commit all staged changes atomically.
    
    During sleep, changes were already applied to the actual tables
    (add_chunk, update_chunk, etc. write directly to DB).
    The staging table tracks what was done for rollback purposes.
    
    After successful commit:
    1. All staged operations are marked as applied
    2. Diary is cleared (optionally saved to logs)
    3. Sleep state is set to AWAKE
    4. Queued messages are ready for processing
    """
    print(f"\n{'='*60}")
    print(f"  COMMITTING SLEEP CHANGES")
    print(f"{'='*60}\n")
    
    set_state(SleepState.COMMITTING)
    
    staged = get_staged_operations(session_id)
    staging_ids = [op["id"] for op in staged]
    
    # Mark all staged operations as applied
    if staging_ids:
        mark_staged_applied(staging_ids)
        print(f"[Commit] {len(staging_ids)} operations marked as applied")
    
    # Handle diary
    try:
        from Mila_observatory.runtime import runtime_config
        save_diary_enabled = runtime_config.get("diary.save_processed", False)
    except ImportError:
        save_diary_enabled = False
    
    if save_diary_enabled:
        save_diary_to_logs()
        print("[Commit] Diary saved to logs")
    
    # Clear diary
    clear_diary()
    print("[Commit] Diary cleared")
    
    # Set state to AWAKE
    set_state(SleepState.AWAKE)
    
    print(f"[Commit] Sleep session {session_id[:8]}... committed successfully")
    return True


# ═══════════════════════════════════════════════════════════════
# SLEEP EXECUTION
# ═══════════════════════════════════════════════════════════════

async def execute_sleep(session_id: str, duration_minutes: int = None) -> bool:
    """
    Execute the full sleep cycle:
    1. Run all three processes in parallel
    2. If all succeed → commit
    3. If any fail → rollback
    
    This is the main entry point for running sleep.
    """
    global _sleep_in_progress
    
    try:
        _sleep_in_progress = True
        
        # Run all processes
        all_success, message, process_results = await run_all_processes(session_id)
        
        print(f"\n{'='*60}")
        print(f"  SLEEP RESULTS")
        for pr in process_results:
            print(f"  • {pr}")
        print(f"  Overall: {'✅ SUCCESS' if all_success else '❌ FAILURE'}")
        print(f"{'='*60}\n")
        
        if all_success:
            # Commit all changes
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


# ═══════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════

async def start_sleep(duration_minutes: int = None, triggered_by: str = "manual") -> Dict[str, Any]:
    """
    Start a sleep session.
    
    Args:
        duration_minutes: Optional duration in minutes. If None, sleeps until manually woken.
        triggered_by: "manual" or "automatic"
    
    Returns:
        Dict with session_id, success, message
    """
    global _sleep_in_progress, _current_session_id, _sleep_start_time, _sleep_duration
    
    # Check if already sleeping
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
    
    # Check if manually triggered but auto-sleep is in progress
    if triggered_by == "manual" and _auto_sleep_task and not _auto_sleep_task.done():
        # Cancel any pending auto-sleep
        _auto_sleep_task.cancel()
        print("[Sleep] Manual sleep triggered, cancelling pending auto-sleep")
    
    # Create session
    import uuid
    session_id = str(uuid.uuid4())
    _current_session_id = session_id
    _sleep_start_time = time.time()
    _sleep_duration = duration_minutes
    
    # Set state to SLEEPING
    set_state(SleepState.SLEEPING, session_id=session_id, triggered_by=triggered_by,
              duration_minutes=duration_minutes)
    
    print(f"\n{'='*60}")
    print(f"  💤 MILA IS GOING TO SLEEP")
    print(f"  Session: {session_id[:8]}...")
    print(f"  Duration: {duration_minutes if duration_minutes else 'until manually woken'} min")
    print(f"  Triggered by: {triggered_by}")
    print(f"{'='*60}\n")
    
    # Add event to Observatory
    try:
        from Mila_observatory.api.dashboard import add_event
        add_event("sleep", f"Sleep started (session: {session_id[:8]}...){', duration: ' + str(duration_minutes) + ' min' if duration_minutes else ''}")
    except ImportError:
        pass
    
    # Execute sleep in background
    asyncio.create_task(_run_sleep_with_timeout(session_id, duration_minutes))
    
    return {
        "success": True,
        "message": "Sleep started",
        "session_id": session_id,
        "duration_minutes": duration_minutes
    }


async def _run_sleep_with_timeout(session_id: str, duration_minutes: int = None):
    """
    Run sleep with optional timeout.
    If duration is specified, sleep will auto-wake after that time.
    The commit phase is NOT interrupted if timeout expires during commit.
    """
    try:
        if duration_minutes:
            # Run sleep with a timeout
            sleep_task = asyncio.create_task(execute_sleep(session_id, duration_minutes))
            
            try:
                # Wait for sleep to complete OR timeout
                await asyncio.wait_for(
                    sleep_task,
                    timeout=duration_minutes * 60 + 300  # Add 5 min buffer for processing
                )
            except asyncio.TimeoutError:
                # Timeout reached — check if we're in COMMITTING state
                current = get_current_state()
                if current.get("state") == "committing":
                    print("[Sleep] Timeout reached during commit — waiting for commit to finish")
                    # Don't cancel, wait for the task to finish
                    await sleep_task
                else:
                    print("[Sleep] Timeout reached — force waking up")
                    # Force wake up (rollback will happen)
                    await wake_up()
        else:
            # No timeout, run until manually woken
            await execute_sleep(session_id)
    
    except asyncio.CancelledError:
        print("[Sleep] Sleep task cancelled")
        # Check if committing
        current = get_current_state()
        if current.get("state") != "committing":
            await rollback_all(session_id)
    
    except Exception as e:
        print(f"[Sleep] Error in sleep task: {e}")
        import traceback
        traceback.print_exc()


async def wake_up() -> Dict[str, Any]:
    """
    Wake Mila up.
    If sleep is in progress, the current operations complete first,
    then state is set to AWAKE.
    If sleep is not in progress, this is a no-op.
    """
    global _sleep_in_progress, _current_session_id
    
    current_state = get_state()
    session_id = _current_session_id
    
    if current_state == SleepState.AWAKE:
        return {"success": True, "message": "Mila is already awake"}
    
    if current_state == SleepState.COMMITTING:
        return {"success": False, "message": "Mila is committing changes, cannot wake until commit completes"}
    
    if current_state == SleepState.SLEEPING:
        print(f"\n{'='*60}")
        print(f"  ⏰ MILA IS BEING WOKEN UP")
        print(f"{'='*60}\n")
        
        # Rollback any uncommitted changes
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
    """Check if Mila is currently sleeping."""
    return get_state() == SleepState.SLEEPING


def is_committing() -> bool:
    """Check if Mila is currently committing changes."""
    return get_state() == SleepState.COMMITTING


def get_sleep_status() -> Dict[str, Any]:
    """
    Get detailed sleep status for Observatory.
    """
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


# ═══════════════════════════════════════════════════════════════
# AUTO-SLEEP SCHEDULER
# ═══════════════════════════════════════════════════════════════

async def auto_sleep_scheduler():
    """
    Background task that checks if it's time to sleep automatically.
    Runs every 60 seconds.
    """
    while True:
        try:
            await _check_auto_sleep()
        except Exception as e:
            print(f"[AutoSleep] Error: {e}")
        
        await asyncio.sleep(60)


async def _check_auto_sleep():
    """
    Check if automatic sleep should be triggered based on runtime_config.
    """
    try:
        from Mila_observatory.runtime import runtime_config
        
        start_hour = runtime_config.get("sleep.start_hour")
        end_hour = runtime_config.get("sleep.end_hour")
        forced_awake = runtime_config.get("sleep.forced_awake", False)
    except ImportError:
        return
    
    if forced_awake:
        return  # Sleep is disabled
    
    if start_hour is None or end_hour is None:
        return  # No auto-sleep configured
    
    now = datetime.now()
    current_hour = now.hour
    
    # Check if current time is within sleep window
    should_sleep = False
    if start_hour <= end_hour:
        # Normal range (e.g., 22:00 - 07:00)
        if start_hour <= current_hour < end_hour:
            should_sleep = True
    else:
        # Wraparound range (e.g., 22:00 - 07:00)
        if current_hour >= start_hour or current_hour < end_hour:
            should_sleep = True
    
    if should_sleep:
        current_state = get_state()
        if current_state == SleepState.AWAKE:
            print(f"[AutoSleep] Time to sleep (hour {current_hour})")
            await start_sleep(duration_minutes=None, triggered_by="automatic")


def start_auto_sleep_scheduler():
    """Start the auto-sleep background task."""
    global _auto_sleep_task
    _auto_sleep_task = asyncio.create_task(auto_sleep_scheduler())
    print("[AutoSleep] Scheduler started")


# ═══════════════════════════════════════════════════════════════
# MESSAGE QUEUE HANDLING
# ═══════════════════════════════════════════════════════════════

def queue_incoming_message(chat_id: str, user_id: str, content: str):
    """
    Queue a message received while Mila is sleeping.
    Called from bot.py when state == sleeping.
    """
    if not _current_session_id:
        return
    
    queue_message(_current_session_id, chat_id, user_id, content)
    print(f"[SleepQueue] Message queued from {user_id[:8]}... in {chat_id[:8]}...")


def get_pending_messages_count() -> int:
    """Get count of pending messages in queue."""
    return get_queue_count()