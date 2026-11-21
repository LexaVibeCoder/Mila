"""
core/sleep/manager.py — Mila's Sleep Manager (Orchestrator).
"""

import asyncio
import json
import sqlite3
import math
import time
import os
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any

from config import settings
from core.sleep.state import (
    SleepState, SleepStage, get_state, get_current_state, set_state,
    get_sleep_stage, finish_reflection, request_wake, begin_rollback,
    get_queued_messages, mark_messages_processed, queue_message,
    get_staged_operations, stage_operation, mark_staged_applied,
    clear_staged_operations, get_queue_count, gc_staged_operations,
    mark_dialog_summaries_rolled_back
)
from core.diary.manager import (
    clear_diary, snapshot_diary_entries, restore_diary_entries,
)
from core.context.buffer import delete_entry, delete_summary_matching, restore_messages
from core.sleep.processes import (
    run_all_processes, restore_media_from_quarantine, purge_media_quarantine,
)
from core.needs.tracker import get_tracker as get_needs_tracker


_auto_sleep_task = None
_WAKE_POLL_SECONDS = 1.0


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


async def _finish_sleep(session_id: str) -> bool:
    """Commit completed reflection, falling back to recovery on commit failure."""
    try:
        if await commit_all(session_id):
            return True
    except Exception as exc:
        print(f"[Sleep] Commit failed: {exc}")
    await rollback_all(session_id)
    return False


async def _wait_for_wake(session_id: str) -> bool:
    """Wait for the persisted deadline or an Observatory wake request."""
    while True:
        current = get_current_state()
        if current.get("session_id") != session_id or get_sleep_stage(current) != SleepStage.DORMANT:
            return False
        remaining = _seconds_until_deadline(current.get("scheduled_end_at"))
        if current.get("wake_requested") or (remaining is not None and remaining <= 0):
            return await _finish_sleep(session_id)
        delay = _WAKE_POLL_SECONDS if remaining is None else min(_WAKE_POLL_SECONDS, remaining)
        await asyncio.sleep(delay)


async def recover_from_crash() -> bool:
    """Resume dormant sleep or recover a previously interrupted transaction."""
    current = get_current_state()
    state = current.get("state", "awake")
    session_id = current.get("session_id")

    if state == "awake":
        _presence_to_rest("sleep complete")
        return False

    print(f"[Recovery] Session {session_id} in state {state}")
    stage = get_sleep_stage(current)
    if stage == SleepStage.ROLLING_BACK:
        await rollback_all(session_id)
        return True

    if state == "sleeping":
        if stage == SleepStage.DORMANT and session_id:
            remaining = _seconds_until_deadline(current.get("scheduled_end_at"))
            if current.get("wake_requested") or (remaining is not None and remaining <= 0):
                await _finish_sleep(session_id)
            else:
                _ensure_presence_sleep(f"resumed {session_id[:8]}")
                asyncio.create_task(_wait_for_wake(session_id))
            return True

        print("[Recovery] Sleep was in progress → rolling back all changes")
        await rollback_all(session_id)
        return True

    elif state == "committing":
        print("[Recovery] Commit was in progress → checking if changes were applied")
        staged = get_staged_operations(session_id) if session_id else []
        if not staged:
            print("[Recovery] All changes were committed. Resuming normal operation.")
            set_state(SleepState.AWAKE)
            _presence_to_rest("sleep complete")
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


def _restore_diary_snapshot(snapshot: list, reason: str) -> int:
    """Put diary rows back after an aborted commit (S1/S5/S6).

    Idempotent: ``restore_diary_entries`` uses ``INSERT OR IGNORE`` by id, so a
    table that was never cleared (or a second rollback) keeps its rows as-is.
    """
    if not snapshot:
        return 0
    try:
        restored = restore_diary_entries(snapshot)
        print(f"[Commit] Diary restored after {reason}: {restored} entr(y/ies)")
        return restored
    except Exception as e:
        print(f"[Commit] ERROR: diary restore failed after {reason}: {e}")
        return 0


async def _rollback_context_summary(payload: dict) -> list:
    """Undo one staged dialog compression: media, summary row, messages.

    Idempotent: the payload is written before each destructive step, so a
    crash at any point leaves undo instructions that are safe to re-apply.
    Returns a list of failure descriptions; each guard is independent so one
    broken step (e.g. a missing file) cannot skip the others (M1).
    """
    failures: list = []
    chat_id = payload.get("chat_id")
    if not chat_id:
        print("[Rollback] context_buffer op without chat_id — skipped")
        return ["context_buffer op without chat_id"]

    media_entries = payload.get("media") or []
    if media_entries:
        restored = failed = 0
        try:
            restored, failed = restore_media_from_quarantine(media_entries)
        except Exception as e:
            failures.append(f"media files for {chat_id[:12]}: {e}")
            failed = len(media_entries)
            print(f"[Rollback] media file restore failed for {chat_id[:12]}...: {e}")

        chunk_media = [
            {"chunk_id": e.get("chunk_id"), "media_path": e.get("media_path")}
            for e in media_entries
        ]
        media_rows = [r for e in media_entries for r in (e.get("media_logs") or [])]
        refs = 0
        try:
            from core.db import restore_detached_media
            refs = restore_detached_media(chunk_media, media_rows)
        except Exception as e:
            failures.append(f"media refs for {chat_id[:12]}: {e}")
            print(f"[Rollback] media refs restore failed for {chat_id[:12]}...: {e}")
        print(f"[Rollback] media for {chat_id[:12]}...: {restored} file(s) back, "
              f"{failed} failed, {refs} DB ref(s) restored")
        if failed:
            failures.append(f"{failed} media file(s) not restored for {chat_id[:12]}")

    try:
        summary_id = payload.get("summary_entry_id")
        if summary_id:
            deleted = delete_entry(chat_id, summary_id)
        elif payload.get("compressing"):
            # Crash between the summary insert and the payload update: match by text.
            deleted = delete_summary_matching(chat_id, payload.get("summary_text") or "")
        else:
            # The destructive step never started — nothing to delete.
            deleted = 0
        if deleted:
            print(f"[Rollback] summary removed for {chat_id[:12]}...")
    except Exception as e:
        failures.append(f"summary delete for {chat_id[:12]}: {e}")
        print(f"[Rollback] summary delete failed for {chat_id[:12]}...: {e}")

    try:
        restored = restore_messages(chat_id, payload.get("deleted_rows") or [])
        if restored:
            print(f"[Rollback] restored {restored} message(s) for {chat_id[:12]}...")
    except Exception as e:
        failures.append(f"messages for {chat_id[:12]}: {e}")
        print(f"[Rollback] message restore failed for {chat_id[:12]}...: {e}")

    return failures


def _rollback_diary_snapshot(payload: dict) -> list:
    """Restore the diary rows carried by a ``diary_entries`` staging row (M2).

    The snapshot is persisted before the destructive clear, so a crash between
    clear and mark can no longer lose entries that only a log file held.
    """
    rows = payload.get("rows") or []
    if not rows:
        return []
    try:
        restored = restore_diary_entries(rows)
        print(f"[Rollback] diary restored from the staged snapshot: {restored} entr(y/ies)")
        return []
    except Exception as e:
        print(f"[Rollback] ERROR: diary snapshot restore failed: {e}")
        return [f"diary restore: {e}"]


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

    begin_rollback(session_id)
    print(f"[Rollback] Rolling back session {session_id[:8]}...")

    staged = get_staged_operations(session_id)

    from core.db import get_conn

    rolled_back_faiss_ids: list[int] = []
    failures: list[str] = []
    media_leftovers = False

    # B1: undo the ops that use their *own* connections first — the context
    # buffer (media refs go through a second mila.db connection) and the diary
    # snapshot (context_buffer.db). Holding the outer mila.db write transaction
    # while they run would block them for busy_timeout and silently drop the
    # restored references.
    own_conn_ops = [
        op for op in staged
        if str(op["target_table"]).startswith("context_buffer")
        or op["target_table"] == "diary_entries"
    ]
    outer_ops = [
        op for op in staged
        if not (str(op["target_table"]).startswith("context_buffer")
                or op["target_table"] == "diary_entries")
    ]

    for op in reversed(own_conn_ops):
        payload = {}
        try:
            payload = json.loads(op["payload"] or "{}")
            if op["target_table"] == "diary_entries":
                failures.extend(_rollback_diary_snapshot(payload))
            else:
                failures.extend(await _rollback_context_summary(payload))
        except Exception as e:
            failures.append(f"op {op['id']} ({op['target_table']}): {e}")
            print(f"[Rollback] Error processing op {op['id']}: {e}")
        finally:
            # M1: leftovers must be known even when the undo above raised.
            for entry in payload.get("media") or []:
                for f in entry.get("files") or []:
                    if f.get("quarantine") and os.path.exists(f["quarantine"]):
                        media_leftovers = True

    conn = get_conn()
    try:
        for op in reversed(outer_ops):
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
                failures.append(f"op {op['id']} ({op['target_table']}): {e}")
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

    # S4: the summary log rows survive for diagnostics but must not look applied.
    try:
        marked = mark_dialog_summaries_rolled_back(session_id)
        if marked:
            print(f"[Rollback] {marked} dialog summary log row(s) marked rolled_back")
    except Exception as e:
        print(f"[Rollback] summary log marking failed: {e}")

    if failures or media_leftovers:
        # M1: an incomplete undo must stay retryable. Keep the staging rows,
        # keep the quarantined bytes, and park the state in sleep_error so
        # recovery runs rollback again instead of pretending the night is over.
        reason = "; ".join(failures) if failures else "media left in quarantine"
        print(f"\n[Rollback] ❌ INCOMPLETE for session {session_id[:8]}...: {reason}")
        print("[Rollback] Staging rows kept, quarantine kept — recovery will retry")
        try:
            set_state(SleepState.SLEEP_ERROR,
                      error_message=f"rollback incomplete: {reason}"[:500])
        except Exception as e:
            print(f"[Rollback] ERROR: could not set sleep_error state: {e}")
        return

    clear_staged_operations(session_id)

    # S13: files are already back (or were never moved). Only a fully restored
    # quarantine dir is discarded, so a failed restore keeps its bytes.
    purge_media_quarantine(session_id)

    # S1: the diary is NOT cleared on rollback. The night analysis was not
    # committed, so its source (the diary) must survive intact; commit_all is
    # the only place that archives and clears it.
    print("[Rollback] Diary kept (sleep was not committed)")

    set_state(SleepState.AWAKE)
    _presence_to_rest("sleep rollback")
    if was_sleeping:
        _record_sleep_wake(sleep_started, "rollback")
    print(f"[Rollback] Complete")


async def commit_all(session_id: str) -> bool:
    """Finalize this session once, even if multiple waiters reach wake."""
    before_commit = get_current_state()
    if not set_state(SleepState.COMMITTING, session_id=session_id):
        # A stale waiter must not commit or roll back a session it did not claim.
        return True

    print(f"\n{'='*60}")
    print(f"  COMMITTING SLEEP CHANGES")
    print(f"{'='*60}\n")

    # Capture the session start before COMMITTING overwrites the state: the
    # wake event needs the real wall-clock duration.
    sleep_started = _parse_iso(before_commit.get("started_at"))

    # S1/S5/M2: snapshot the rows once, then publish them as a staging undo row
    # *before* the destructive clear. An in-memory snapshot dies with the
    # process, so a crash between clear and mark would otherwise leave entries
    # only in the log file; the staged row is restored by rollback/recovery.
    diary_snapshot = snapshot_diary_entries()
    if diary_snapshot:
        stage_operation(
            session_id=session_id,
            operation_type="insert",
            target_table="diary_entries",
            target_id=session_id,
            payload={"rows": diary_snapshot, "entry_count": len(diary_snapshot)},
        )

    staged = get_staged_operations(session_id)
    staging_ids = [op["id"] for op in staged]

    # S7: clearing without a verified backup is silent data loss, and a failed
    # clear must not leave the session looking committed.
    try:
        clear_diary(diary_snapshot)
        print("[Commit] Diary cleared")
    except Exception as e:
        print(f"[Commit] ERROR: diary clear failed: {e}")
        _restore_diary_snapshot(diary_snapshot, "clear failed")
        return False

    # S5: mark applied last. Before this point a crash makes recovery see
    # applied=0 and roll back instead of trusting a half-done commit.
    if staging_ids:
        try:
            mark_staged_applied(staging_ids)
        except Exception as e:
            print(f"[Commit] ERROR: failed to mark staged operations applied: {e}")
            _restore_diary_snapshot(diary_snapshot, "mark applied failed")
            return False
        print(f"[Commit] {len(staging_ids)} operations marked as applied")

    # S13: quarantined media is only discarded once everything above is durable.
    purge_media_quarantine(session_id)

    # S17: applied staging rows are diagnostics, not history — keep them bounded.
    try:
        gc_staged_operations()
    except Exception as e:
        print(f"[Commit] staging GC failed: {e}")

    _presence_to_rest("sleep complete")
    set_state(SleepState.AWAKE)
    _record_sleep_wake(sleep_started, "commit")

    print(f"[Commit] Sleep session {session_id[:8]}... committed successfully")
    return True


async def execute_sleep(session_id: str, duration_minutes: int = None) -> bool:
    """Run the reflection workers and own all wake and rollback transitions."""
    reflection = asyncio.create_task(run_all_processes(session_id))
    timeout = time.monotonic() + duration_minutes * 60 + 300 if duration_minutes else None
    try:
        try:
            while not reflection.done():
                current = get_current_state()
                if current.get("session_id") != session_id:
                    return False
                if current.get("wake_requested") or (timeout is not None and time.monotonic() >= timeout):
                    break
                await asyncio.wait({reflection}, timeout=_WAKE_POLL_SECONDS)

            all_success = False
            if reflection.done():
                all_success, _, process_results = reflection.result()
                for result in process_results:
                    print(f"[Sleep] {result}")
        finally:
            if not reflection.done():
                reflection.cancel()
            await asyncio.gather(reflection, return_exceptions=True)

        if all_success and finish_reflection(session_id):
            return await _wait_for_wake(session_id)
        await rollback_all(session_id)
        return False

    except Exception as exc:
        print(f"[Sleep] Critical error during sleep: {exc}")
        set_state(SleepState.SLEEP_ERROR, error_message=str(exc))
        await rollback_all(session_id)
        return False


async def start_sleep(duration_minutes: int = None, triggered_by: str = "manual",
                      note: str = "") -> Dict[str, Any]:
    # Snapshot BEFORE presence is switched to sleep: the journal needs to know
    # where Mila was (rest/working/dialog) and for how long.
    prev_presence = _presence_snapshot()
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

    import uuid
    session_id = str(uuid.uuid4())
    if not set_state(SleepState.SLEEPING, session_id=session_id, triggered_by=triggered_by,
                     duration_minutes=duration_minutes):
        return {"success": False, "message": "A sleep session is already active or needs recovery"}
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

    asyncio.create_task(execute_sleep(session_id, duration_minutes))

    return {
        "success": True,
        "message": "Sleep started",
        "session_id": session_id,
        "duration_minutes": duration_minutes
    }


async def wake_up(session_id: str = None, mode: str = None) -> Dict[str, Any]:
    """Request safe completion or interruption for the current sleep session."""
    current = get_current_state()
    if session_id is not None and session_id != current.get("session_id"):
        return {"success": False, "message": "Сеанс сна сменился. Проверьте статус и повторите действие."}
    if current.get("state") == SleepState.AWAKE:
        return {"success": True, "message": "Мила уже проснулась"}

    stage = get_sleep_stage(current)
    expected_mode = {
        SleepStage.DORMANT: "keep",
        SleepStage.REFLECTION: "rollback",
    }.get(stage)
    if not expected_mode or not current.get("session_id"):
        return {"success": False, "message": "Пробуждение в этой фазе недоступно"}
    if mode is not None and mode != expected_mode:
        return {"success": False, "message": "Фаза сна изменилась. Проверьте статус и повторите действие."}
    if not request_wake(current["session_id"], current["current_stage"]):
        return {"success": False, "message": "Фаза сна изменилась. Проверьте статус и повторите действие."}
    return {"success": True, "message": "Запрос на пробуждение принят"}


def is_sleeping() -> bool:
    return get_state() == SleepState.SLEEPING


def is_committing() -> bool:
    return get_state() == SleepState.COMMITTING


def get_sleep_status() -> Dict[str, Any]:
    """Report persisted phases and workers identically from either process."""
    state_data = get_current_state()
    stage = get_sleep_stage(state_data)
    active = state_data.get("state") in (SleepState.SLEEPING, SleepState.COMMITTING)
    started = _parse_iso(state_data.get("started_at")) if active else None
    remaining = _seconds_until_deadline(state_data.get("scheduled_end_at")) if active else None
    processes = json.loads(state_data.get("processes_json") or "{}")
    elapsed = (datetime.now(timezone.utc) - started).total_seconds() if started else None
    if stage in (SleepStage.DORMANT, SleepStage.COMMITTING):
        progress = 100.0
    elif processes:
        progress = 100 * sum(value == "completed" for value in processes.values()) / len(processes)
    else:
        progress = 0.0

    return {
        **state_data,
        "current_stage": stage,
        "progress_pct": progress,
        "processes": processes,
        "wake_requested": bool(state_data.get("wake_requested")),
        "queue_count": get_queue_count(),
        "elapsed_seconds": max(0.0, elapsed) if elapsed is not None else None,
        "remaining_seconds": max(0.0, remaining) if remaining is not None else None,
        "in_progress": stage in (SleepStage.REFLECTION, SleepStage.COMMITTING, SleepStage.ROLLING_BACK),
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

    now = datetime.now().astimezone()
    start = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if start > now:
        start -= timedelta(days=1)
    end = start.replace(hour=end_hour)
    if end < start:
        end += timedelta(days=1)
    if not start <= now < end:
        return

    current = get_current_state()
    completed = _parse_iso(current.get("completed_at"))
    if current.get("state") == SleepState.AWAKE and (completed is None or completed < start):
        minutes = math.ceil((end - now).total_seconds() / 60)
        print(f"[AutoSleep] Time to sleep (hour {now.hour})")
        await start_sleep(duration_minutes=minutes, triggered_by="automatic")


def start_auto_sleep_scheduler():
    global _auto_sleep_task
    _auto_sleep_task = asyncio.create_task(auto_sleep_scheduler())
    print("[AutoSleep] Scheduler started")


def queue_incoming_message(chat_id: str, user_id: str, content: str):
    session_id = get_current_state().get("session_id")
    if session_id:
        queue_message(session_id, chat_id, user_id, content)
    else:
        print("[SleepQueue] WARNING: sleeping but no session — message kept in chunks/buffer, not in sleep queue")


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
