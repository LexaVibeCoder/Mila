"""Presence manager — explicit Mila states.

States (v1, per user decisions):
- rest    — default. Sees every notification, decides herself.
- dialog  — in dialog with one specific chat (focus_chat_id set).
            To talk to someone, enter dialog with their chat_id first.
- working — same visibility as rest in v1 (writes texts for her publishing
            house in the future). Kept separate so the prompt/tool history
            already distinguishes it.
- sleep   — chosen by Mila via start_sleep(duration); delegates to the
            existing sleep manager (reflection, memorization, etc.).

Persistence: single row in mila.db (table presence_state), so the state
survives restarts. Sleep itself is owned by core/sleep/* — here we only
record the intent; the sleep manager remains the source of truth for
is_sleeping().
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from core.db import get_conn

VALID_STATES = ("rest", "dialog", "working", "sleep")


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS presence_state (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            state TEXT NOT NULL DEFAULT 'rest',
            focus_chat_id TEXT,
            focus_user_name TEXT,
            note TEXT DEFAULT '',
            updated_at TEXT
        )
    """)
    conn.execute("INSERT OR IGNORE INTO presence_state (id, state) VALUES (1, 'rest')")


def _row_to_dict(row) -> dict:
    return {
        "state": row["state"] or "rest",
        "focus_chat_id": row["focus_chat_id"],
        "focus_user_name": row["focus_user_name"],
        "note": row["note"] or "",
        "updated_at": row["updated_at"],
    }


def get_presence() -> dict:
    conn = get_conn()
    try:
        _ensure_table(conn)
        conn.commit()
        cur = conn.execute("SELECT * FROM presence_state WHERE id=1")
        row = cur.fetchone()
        if row is None:
            return {"state": "rest", "focus_chat_id": None,
                    "focus_user_name": None, "note": "", "updated_at": None}
        return _row_to_dict(row)
    finally:
        conn.close()


def _set(state: str, focus_chat_id: Optional[str] = None,
         focus_user_name: Optional[str] = None, note: str = "") -> dict:
    assert state in VALID_STATES, f"unknown presence state: {state}"
    now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    try:
        _ensure_table(conn)
        conn.execute(
            "UPDATE presence_state SET state=?, focus_chat_id=?, "
            "focus_user_name=?, note=?, updated_at=? WHERE id=1",
            (state, focus_chat_id, focus_user_name, note or "", now),
        )
        conn.commit()
    finally:
        conn.close()
    return get_presence()


def enter_rest(note: str = "") -> dict:
    """Leave any dialog/work and return to default rest."""
    return _set("rest", None, None, note)


def enter_dialog(chat_id: str, user_name: Optional[str] = None,
                 note: str = "") -> dict:
    """Enter dialog with one specific person (focus_chat_id required)."""
    return _set("dialog", str(chat_id), user_name, note)


def enter_working(note: str = "") -> dict:
    """Busy with her own stuff (v1: same visibility as rest)."""
    return _set("working", None, None, note)


def enter_sleep_note(note: str = "") -> dict:
    """Record sleep intent. The real sleep cycle is owned by sleep manager."""
    return _set("sleep", None, None, note)


def is_in_dialog_with(chat_id: str) -> bool:
    p = get_presence()
    return p["state"] == "dialog" and str(p.get("focus_chat_id")) == str(chat_id)


def get_focus_last_activity_seconds() -> float | None:
    """Seconds since the last chunk (any side) in the focus chat.

    Returns None when not in dialog, no focus, or no history yet.
    """
    p = get_presence()
    if p.get("state") != "dialog" or not p.get("focus_chat_id"):
        return None
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT time_start FROM chunks WHERE chat_id=? ORDER BY time_start DESC LIMIT 1",
            (str(p["focus_chat_id"]),),
        )
        row = cur.fetchone()
        if row is None or not row["time_start"]:
            return None
        try:
            last = datetime.fromisoformat(row["time_start"])
        except (ValueError, TypeError):
            return None
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).total_seconds()
    finally:
        conn.close()


def check_dialog_idle(timeout_seconds: float | None = None) -> dict | None:
    """Exit dialog → rest if focus chat is silent longer than timeout.

    Returns the new presence dict when a transition happened, else None.
    Never raises; sleeping states are left alone (sleep manager owns them).
    """
    if timeout_seconds is None:
        try:
            from config import settings
            timeout_seconds = float(getattr(settings, "DIALOG_IDLE_TIMEOUT_SECONDS", 300.0))
        except Exception:
            timeout_seconds = 300.0
    try:
        p = get_presence()
        if p.get("state") != "dialog" or not p.get("focus_chat_id"):
            return None
        idle = get_focus_last_activity_seconds()
        if idle is None:
            return None
        if idle < timeout_seconds:
            return None
        # Don't kick her out while the peer is actively typing.
        try:
            if _typing_checker is not None and _typing_checker(str(p["focus_chat_id"])):
                return None
        except Exception:
            pass
        focus = p.get("focus_user_name") or p.get("focus_chat_id")
        state = enter_rest(f"auto: тишина в диалоге с {focus} более {int(timeout_seconds // 60)} мин")
        try:
            from core.needs.tracker import get_tracker as _get_needs
            _get_needs().apply_action_effect("enter_rest")
        except Exception:
            pass
        try:
            from Mila_observatory.api.dashboard import add_event
            add_event("presence", f"Auto-rest: диалог с {focus} завершен после {idle:.0f}с тишины")
        except Exception:
            pass
        print(f"[Presence] auto-rest: диалог с {focus} завершен после {idle:.0f}с тишины")
        return state
    except Exception as e:
        print(f"[Presence] idle check failed: {e}")
        return None


# Background watcher: ticks every N seconds, calls check_dialog_idle().
_auto_rest_task = None
_typing_checker = None


async def _auto_rest_loop(interval_seconds: float = 30.0) -> None:
    import asyncio
    while True:
        try:
            check_dialog_idle()
        except Exception as e:
            print(f"[Presence] auto-rest loop error: {e}")
        await asyncio.sleep(interval_seconds)


def start_auto_rest_scheduler(typing_checker=None) -> None:
    """Start the dialog-idle watcher. typing_checker(chat_id) -> bool."""
    global _auto_rest_task, _typing_checker
    if typing_checker is not None:
        _typing_checker = typing_checker
    if _auto_rest_task is not None and not _auto_rest_task.done():
        return
    try:
        from config import settings
        interval = float(getattr(settings, "DIALOG_IDLE_CHECK_INTERVAL_SECONDS", 30.0))
    except Exception:
        interval = 30.0
    import asyncio
    _auto_rest_task = asyncio.create_task(_auto_rest_loop(interval))
    print(f"[Presence] Auto-rest watcher started (timeout check every {interval:.0f}s)")


def stop_auto_rest_scheduler() -> None:
    global _auto_rest_task
    if _auto_rest_task is not None and not _auto_rest_task.done():
        _auto_rest_task.cancel()
    _auto_rest_task = None


def format_state_block(current_chat_id: str | None = None,
                       current_user_name: str | None = None) -> str:
    """Location block for the system prompt (no behavior rules here).

    Behavior lives in prompts/states/{rest,dialog,working}.md — the model
    sees exactly one of them. This block only says where she is and who
    writes, with display names, not bare ids.
    """
    p = get_presence()
    state = p["state"]
    lines = ["## Где ты"]
    if state == "rest":
        lines.append("Ты отдыхаешь (rest).")
    elif state == "dialog":
        focus = p.get("focus_user_name") or p.get("focus_chat_id") or "?"
        fid = p.get("focus_chat_id") or "?"
        lines.append(f"Ты в диалоге с {focus} (чат {fid}).")
    elif state == "working":
        extra = f" ({p['note']})" if p.get("note") else ""
        lines.append(f"Ты занята своим делом{extra}.")
    elif state == "sleep":
        lines.append("Ты собиралась спать. Если сон ещё не начался — жди его начала.")
    else:
        lines.append(f"Состояние: {state}.")

    if current_chat_id is not None:
        who = current_user_name or current_chat_id
        if state == "dialog" and str(p.get("focus_chat_id")) != str(current_chat_id):
            lines.append(
                f"Это уведомление не из твоего диалога: пишет {who} "
                f"(чат {current_chat_id})."
            )
        else:
            lines.append(f"Уведомление от {who} (чат {current_chat_id}).")
    return "\n".join(lines)
