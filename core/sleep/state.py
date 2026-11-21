"""
core/sleep/state.py — Mila's sleep state machine.
"""

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import List, Dict, Any

from core.db import get_conn


class SleepState(str, Enum):
    AWAKE = "awake"
    SLEEPING = "sleeping"
    COMMITTING = "committing"
    SLEEP_ERROR = "sleep_error"


class SleepStage(str, Enum):
    REFLECTION = "reflection"
    DORMANT = "dormant"
    COMMITTING = "committing"
    ROLLING_BACK = "rolling_back"
    IDLE = "idle"


REFLECTION_PROCESSES = ("diary_analysis", "dialog_summarization", "user_fact_extraction")


def _ensure_sleep_tables():
    conn = get_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sleep_state (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                state TEXT NOT NULL DEFAULT 'awake',
                session_id TEXT,
                current_stage TEXT DEFAULT 'idle',
                progress_pct REAL DEFAULT 0.0,
                started_at TEXT,
                scheduled_end_at TEXT,
                completed_at TEXT,
                triggered_by TEXT DEFAULT 'manual',
                error_message TEXT,
                version INTEGER DEFAULT 1,
                wake_requested INTEGER NOT NULL DEFAULT 0,
                processes_json TEXT NOT NULL DEFAULT '{}'
            );
            INSERT OR IGNORE INTO sleep_state (id, state) VALUES (1, 'awake');
            CREATE TABLE IF NOT EXISTS sleep_message_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                content TEXT NOT NULL,
                received_at TEXT DEFAULT (datetime('now')),
                processed INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sleep_transaction_staging (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                operation_type TEXT NOT NULL,
                target_table TEXT NOT NULL,
                target_id TEXT,
                payload TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                applied INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sleep_dialog_summary_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                summary_text TEXT,
                messages_removed INTEGER DEFAULT 0,
                rolled_back INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)
        # Idempotent migration for DBs created before rollback tracking (S4).
        try:
            conn.execute(
                "ALTER TABLE sleep_dialog_summary_log "
                "ADD COLUMN rolled_back INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(sleep_state)")}
        for name, definition in (
            ("wake_requested", "INTEGER NOT NULL DEFAULT 0"),
            ("processes_json", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            if name not in columns:
                try:
                    conn.execute(f"ALTER TABLE sleep_state ADD COLUMN {name} {definition}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc):
                        raise
        conn.commit()
    finally:
        conn.close()


def get_current_state() -> Dict:
    _ensure_sleep_tables()
    conn = get_conn()
    try:
        cur = conn.execute("SELECT * FROM sleep_state WHERE id=1")
        row = cur.fetchone()
        return dict(row) if row else {"state": "awake", "current_stage": "idle", "progress_pct": 0.0}
    finally:
        conn.close()


def get_state() -> SleepState:
    state_data = get_current_state()
    return SleepState(state_data.get("state", "awake"))


def set_state(state: SleepState, session_id: str = None, triggered_by: str = "manual",
              duration_minutes: int = None, error_message: str = None) -> bool:
    _ensure_sleep_tables()
    conn = get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()

        if state == SleepState.SLEEPING:
            if not session_id:
                session_id = str(uuid.uuid4())
            scheduled_end = None
            if duration_minutes:
                scheduled_end = (datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)).isoformat()

            cur = conn.execute(
                """UPDATE sleep_state SET 
                   state=?, session_id=?, current_stage='reflection',
                   progress_pct=0.0, started_at=?, scheduled_end_at=?,
                   wake_requested=0, processes_json=?,
                   triggered_by=?, error_message=NULL, version=version+1
                   WHERE id=1 AND state='awake'""",
                (state.value, session_id, now, scheduled_end,
                 json.dumps(dict.fromkeys(REFLECTION_PROCESSES, "pending")), triggered_by)
            )
            if cur.rowcount == 0:
                return False
        elif state == SleepState.COMMITTING:
            cur = conn.execute(
                """UPDATE sleep_state SET 
                   state=?, current_stage='committing', wake_requested=0,
                   version=version+1 WHERE id=1 AND session_id=?
                   AND state='sleeping' AND current_stage<>'rolling_back'""",
                (state.value, session_id)
            )
            if cur.rowcount == 0:
                return False
        elif state == SleepState.AWAKE:
            conn.execute(
                """UPDATE sleep_state SET 
                   state=?, current_stage='idle', progress_pct=0.0,
                   completed_at=?, session_id=NULL, triggered_by='manual',
                   wake_requested=0, processes_json='{}', started_at=NULL, scheduled_end_at=NULL,
                   error_message=NULL, version=version+1 WHERE id=1""",
                (state.value, now)
            )
        elif state == SleepState.SLEEP_ERROR:
            conn.execute(
                """UPDATE sleep_state SET 
                   state=?, current_stage='idle', progress_pct=0.0, wake_requested=0,
                   error_message=?, version=version+1 WHERE id=1""",
                (state.value, error_message)
            )

        conn.commit()
        print(f"[SleepState] → {state.value}")
        return True
    finally:
        conn.close()


def get_sleep_stage(state: Dict) -> str:
    """Normalize phases written by previous versions of the sleep worker."""
    stage = state.get("current_stage")
    if stage == SleepStage.ROLLING_BACK:
        return SleepStage.ROLLING_BACK.value
    if state.get("state") == SleepState.COMMITTING:
        return SleepStage.COMMITTING.value
    if state.get("state") == SleepState.SLEEPING:
        legacy_dormant = stage == SleepStage.IDLE and (state.get("progress_pct") or 0) >= 89
        if stage == SleepStage.DORMANT or legacy_dormant:
            return SleepStage.DORMANT.value
        return SleepStage.REFLECTION.value
    return SleepStage.IDLE.value


def set_process_status(session_id: str, process: str, status: str):
    """Update one worker's phase without overwriting its parallel peers."""
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE sleep_state SET processes_json=json_set(processes_json, ?, ?)
               WHERE id=1 AND session_id=? AND state='sleeping' AND current_stage='reflection'""",
            (f"$.{process}", status, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def finish_reflection(session_id: str) -> bool:
    """Enter dormant sleep only if the session has no pending rollback request."""
    conn = get_conn()
    try:
        cur = conn.execute(
            """UPDATE sleep_state SET current_stage='dormant', progress_pct=100.0
               WHERE id=1 AND session_id=? AND state='sleeping'
                 AND current_stage='reflection' AND wake_requested=0""",
            (session_id,),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def request_wake(session_id: str, stage: str) -> bool:
    """Hand off wake to the session owner, guarded against a phase change."""
    conn = get_conn()
    try:
        cur = conn.execute(
            """UPDATE sleep_state SET wake_requested=1
               WHERE id=1 AND session_id=? AND state='sleeping' AND current_stage=?""",
            (session_id, stage),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def begin_rollback(session_id: str):
    """Publish rollback before starting undo operations."""
    _ensure_sleep_tables()
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE sleep_state SET current_stage='rolling_back', wake_requested=0
               WHERE id=1 AND session_id=?""",
            (session_id,),
        )
        conn.commit()
    finally:
        conn.close()


def queue_message(session_id: str, chat_id: str, user_id: str, content: str):
    _ensure_sleep_tables()
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO sleep_message_queue 
               (session_id, chat_id, user_id, content) VALUES (?, ?, ?, ?)""",
            (session_id, chat_id, user_id, content)
        )
        conn.commit()
    finally:
        conn.close()


def get_queued_messages(session_id: str = None) -> List[Dict]:
    _ensure_sleep_tables()
    conn = get_conn()
    try:
        if session_id:
            cur = conn.execute(
                "SELECT * FROM sleep_message_queue WHERE processed=0 AND session_id=? ORDER BY id ASC",
                (session_id,)
            )
        else:
            cur = conn.execute(
                "SELECT * FROM sleep_message_queue WHERE processed=0 ORDER BY id ASC"
            )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def mark_messages_processed(message_ids: List[int]):
    if not message_ids:
        return
    _ensure_sleep_tables()
    conn = get_conn()
    try:
        placeholders = ",".join("?" for _ in message_ids)
        conn.execute(
            f"UPDATE sleep_message_queue SET processed=1 WHERE id IN ({placeholders})",
            message_ids
        )
        conn.commit()
    finally:
        conn.close()


def get_queue_count() -> int:
    _ensure_sleep_tables()
    conn = get_conn()
    try:
        cur = conn.execute("SELECT COUNT(*) as cnt FROM sleep_message_queue WHERE processed=0")
        row = cur.fetchone()
        return row["cnt"] if row else 0
    finally:
        conn.close()


def stage_operation(session_id: str, operation_type: str, target_table: str,
                    target_id: str = None, payload: dict = None) -> int:
    _ensure_sleep_tables()
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO sleep_transaction_staging 
               (session_id, operation_type, target_table, target_id, payload)
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, operation_type, target_table, target_id, 
             json.dumps(payload, ensure_ascii=False) if payload else "{}")
        )
        conn.commit()
        staging_id = cur.lastrowid
        print(f"[SleepStaging] Staged {operation_type} on {target_table} (id={staging_id})")
        return staging_id
    finally:
        conn.close()


def get_staged_operations(session_id: str) -> List[Dict]:
    _ensure_sleep_tables()
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT * FROM sleep_transaction_staging WHERE session_id=? AND applied=0 ORDER BY id ASC",
            (session_id,)
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def mark_staged_applied(staging_ids: List[int]):
    if not staging_ids:
        return
    _ensure_sleep_tables()
    conn = get_conn()
    try:
        placeholders = ",".join("?" for _ in staging_ids)
        conn.execute(
            f"UPDATE sleep_transaction_staging SET applied=1 WHERE id IN ({placeholders})",
            staging_ids
        )
        conn.commit()
    finally:
        conn.close()


def clear_staged_operations(session_id: str):
    _ensure_sleep_tables()
    conn = get_conn()
    try:
        conn.execute(
            "DELETE FROM sleep_transaction_staging WHERE session_id=?",
            (session_id,)
        )
        conn.commit()
    finally:
        conn.close()


def discard_staged_operation(staging_id: int) -> bool:
    """Drop an unapplied undo row that is no longer needed.

    Used when a staged insert did not actually happen (e.g. a racing writer
    created the ``users`` row first) — keeping it would let rollback delete
    data this session never created (m9).
    """
    if not staging_id:
        return False
    _ensure_sleep_tables()
    conn = get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM sleep_transaction_staging WHERE id=? AND applied=0",
            (staging_id,)
        )
        conn.commit()
        return (cur.rowcount or 0) > 0
    finally:
        conn.close()


def update_staged_payload(staging_id: int, payload: dict) -> bool:
    """Rewrite the payload of an unapplied staging row.

    Needed when an undo record can only be completed after the destructive
    step produced an id (summary row id, real deleted rowcount) — the record
    itself is staged *before* that step so rollback always finds it.
    """
    if not staging_id:
        return False
    _ensure_sleep_tables()
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE sleep_transaction_staging SET payload=? WHERE id=? AND applied=0",
            (json.dumps(payload, ensure_ascii=False), staging_id)
        )
        conn.commit()
        return (cur.rowcount or 0) > 0
    finally:
        conn.close()


def gc_staged_operations(retention_days: int = 30) -> int:
    """Delete applied staging rows older than ``retention_days`` (S17).

    Applied rows are kept for a while so a look at the staging table can still
    explain a recent sleep, but they must not grow forever.
    """
    _ensure_sleep_tables()
    days = max(1, int(retention_days))
    conn = get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM sleep_transaction_staging "
            "WHERE applied=1 AND created_at < datetime('now', ?)",
            (f"-{days} days",)
        )
        conn.commit()
        removed = cur.rowcount or 0
        if removed:
            print(f"[SleepStaging] GC removed {removed} applied row(s)")
        return removed
    finally:
        conn.close()


def log_dialog_summary(session_id: str, chat_id: str, summary_text: str, messages_removed: int):
    _ensure_sleep_tables()
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO sleep_dialog_summary_log 
               (session_id, chat_id, summary_text, messages_removed)
               VALUES (?, ?, ?, ?)""",
            (session_id, chat_id, summary_text, messages_removed)
        )
        conn.commit()
    finally:
        conn.close()


def mark_dialog_summaries_rolled_back(session_id: str) -> int:
    """Flag this session's summary log rows as rolled back (S4).

    Rows are marked rather than deleted: the log is the only evidence that a
    summary was produced and then rejected by a failed sleep.
    """
    if not session_id:
        return 0
    _ensure_sleep_tables()
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE sleep_dialog_summary_log SET rolled_back=1 "
            "WHERE session_id=? AND rolled_back=0",
            (session_id,)
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def get_active_dialogs_since_last_sleep() -> List[str]:
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT completed_at FROM sleep_state WHERE id=1 AND completed_at IS NOT NULL"
        )
        row = cur.fetchone()
        last_sleep_time = row["completed_at"] if row else None

        if last_sleep_time:
            cur = conn.execute(
                "SELECT DISTINCT chat_id FROM chunks WHERE time_start > ?",
                (last_sleep_time,)
            )
        else:
            cur = conn.execute("SELECT DISTINCT chat_id FROM chunks")

        return [r["chat_id"] for r in cur.fetchall()]
    finally:
        conn.close()


def get_active_dialogs_detail() -> List[Dict[str, Any]]:
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT completed_at FROM sleep_state WHERE id=1 AND completed_at IS NOT NULL"
        )
        row = cur.fetchone()
        last_sleep_time = row["completed_at"] if row else None

        if last_sleep_time:
            cur = conn.execute(
                """SELECT chat_id, COUNT(*) as message_count, MAX(time_start) as last_message_time
                   FROM chunks 
                   WHERE time_start > ?
                   GROUP BY chat_id
                   ORDER BY message_count DESC""",
                (last_sleep_time,)
            )
        else:
            cur = conn.execute(
                """SELECT chat_id, COUNT(*) as message_count, MAX(time_start) as last_message_time
                   FROM chunks 
                   GROUP BY chat_id
                   ORDER BY message_count DESC"""
            )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
