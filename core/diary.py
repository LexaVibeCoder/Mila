"""
core/diary.py — Mila's diary management.

The diary is Mila's working memory for one day.
It's stored as individual entries in context_buffer.db.
During the day, Mila can consciously write entries via the write_diary tool.
During sleep, the diary is analyzed and then cleared.

After sleep processing:
- Diary is always cleared
- If setting enabled, a copy is saved to project diary_logs/ folder
"""

import os
import sqlite3
from datetime import datetime
from typing import List, Dict, Optional

from config import settings
from core.context_buffer import _get_conn as _get_context_conn

# Diary DB is in context_buffer.db (separate table)
DIARY_TABLE = "diary_entries"


def _ensure_diary_table():
    """Create diary_entries table in context_buffer.db if not exists."""
    conn = _get_context_conn()
    try:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {DIARY_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_number INTEGER NOT NULL,
                content_md TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
    finally:
        conn.close()


def add_diary_entry(content_md: str) -> int:
    """
    Add a new diary entry.
    Returns the entry number.
    """
    _ensure_diary_table()
    conn = _get_context_conn()
    try:
        # Get next entry number
        cur = conn.execute(f"SELECT COALESCE(MAX(entry_number), 0) + 1 as next_num FROM {DIARY_TABLE}")
        row = cur.fetchone()
        next_num = row["next_num"] if row else 1

        conn.execute(
            f"INSERT INTO {DIARY_TABLE} (entry_number, content_md) VALUES (?, ?)",
            (next_num, content_md)
        )
        conn.commit()
        print(f"[Diary] Entry #{next_num} added")
        return next_num
    finally:
        conn.close()


def get_all_diary_entries() -> List[Dict]:
    """Get all diary entries in chronological order."""
    _ensure_diary_table()
    conn = _get_context_conn()
    try:
        cur = conn.execute(
            f"SELECT id, entry_number, content_md, created_at FROM {DIARY_TABLE} ORDER BY entry_number ASC"
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_diary_markdown() -> str:
    """
    Get the full diary as a Markdown document.
    Returns empty string if no entries.
    """
    entries = get_all_diary_entries()
    if not entries:
        return ""

    lines = ["# Diary", ""]
    for entry in entries:
        lines.append(f"[{entry['entry_number']:03d}]")
        lines.append(entry["content_md"])
        lines.append("")

    return "\n".join(lines)


def get_diary_entry_count() -> int:
    """Get number of diary entries."""
    _ensure_diary_table()
    conn = _get_context_conn()
    try:
        cur = conn.execute(f"SELECT COUNT(*) as cnt FROM {DIARY_TABLE}")
        row = cur.fetchone()
        return row["cnt"] if row else 0
    finally:
        conn.close()


def clear_diary():
    """Clear all diary entries (called after sleep processing)."""
    _ensure_diary_table()
    conn = _get_context_conn()
    try:
        conn.execute(f"DELETE FROM {DIARY_TABLE}")
        conn.commit()
        print("[Diary] All entries cleared")
    finally:
        conn.close()


def save_diary_to_logs():
    """
    Save a copy of the diary to the diary_logs/ folder.
    Only called if the setting is enabled.
    """
    markdown = get_diary_markdown()
    if not markdown:
        return

    log_dir = os.path.join(settings.LOG_DIR, "diary_logs")
    os.makedirs(log_dir, exist_ok=True)

    filename = f"diary_{datetime.now().strftime('%Y%m%d')}.md"
    filepath = os.path.join(log_dir, filename)

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(markdown)
        print(f"[Diary] Saved to {filepath}")
    except Exception as e:
        print(f"[Diary] Failed to save log: {e}")