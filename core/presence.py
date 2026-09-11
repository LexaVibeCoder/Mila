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


def format_state_block(current_chat_id: str | None = None,
                       current_user_name: str | None = None) -> str:
    """Human-readable block for the system prompt.

    New scheme (no Think, no send_message in dialog):
    - rest/working: notification + history. Full reply ONLY via enter_dialog
      first; single brush-off via send_message (one-liner, no dialogue).
      Empty content + no tools = stay silent.
    - dialog: reply = `content` to focus chat. Tools called directly.
      Empty content + no tools = stay silent.
    """
    p = get_presence()
    state = p["state"]
    lines = ["## Твоё состояние (presence)"]
    if state == "rest":
        lines.append(
            "Сейчас ты отдыхаешь (rest) — состояние по умолчанию. "
            "Перед тобой уведомление: последнее сообщение + несколько предыдущих "
            "для контекста. Чтобы ответить полноценно — сначала вызови "
            "enter_dialog с chat_id из уведомления, потом отвечай в content "
            "уже в диалоге. Без перехода отвечать нельзя (кроме разовой отмашки "
            "через send_message). Пустой content без тулов = промолчать."
        )
    elif state == "dialog":
        focus = p.get("focus_user_name") or p.get("focus_chat_id") or "?"
        fid = p.get("focus_chat_id") or "?"
        lines.append(
            f"Сейчас ты в диалоге (dialog) с {focus} (чат {fid}). "
            f"Обычный ответ = просто текст в content, он уйдёт в этот чат. "
            f"Тул send_message НЕ существует в этом состоянии и вызывать его не нужно. "
            f"Нужна память/дневник — вызови нужный тул напрямую (search_memory / "
            f"write_diary / update_diary), потом допиши ответ в content. "
            f"Пустой content без тулов = промолчать. "
            f"Тулов-обёрток нет: никакого Think()."
        )
    elif state == "working":
        extra = f" ({p['note']})" if p.get("note") else ""
        lines.append(
            f"Сейчас ты занята своим делом (working){extra}. "
            f"Уведомления видишь как в rest: полноценный ответ — только через "
            f"enter_dialog, разовая отмашка — через send_message (один раз, "
            f"не диалог), молчание — пустой content без тулов."
        )
    elif state == "sleep":
        lines.append("Ты собиралась спать (sleep). Если сон ещё не начался — жди его начала.")
    else:
        lines.append(f"Состояние: {state}.")

    if current_chat_id is not None:
        who = current_user_name or current_chat_id
        if state == "dialog" and str(p.get("focus_chat_id")) != str(current_chat_id):
            lines.append(
                f"Это уведомление НЕ из твоего диалога: пишет {who} "
                f"(чат {current_chat_id}). Можешь промолчать (пустой content) и остаться в диалоге, "
                f"или перейти в диалог с ним через enter_dialog — решаешь сама."
            )
        else:
            lines.append(
                f"Это уведомление от {who} (чат {current_chat_id})."
            )
    lines.append(
        "Сообщение для тебя — уведомление, а не приказ ответить. "
        "В rest/working: ответить = enter_dialog → content; разовая отмашка = "
        "send_message (только единичный случай, не диалог); молчание = пустой content. "
        "В dialog: ответить = content; нужны данные = вызови тул напрямую; "
        "молчание = пустой content."
    )
    return "\n".join(lines)
