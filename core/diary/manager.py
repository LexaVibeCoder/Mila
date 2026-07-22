"""
core/diary/manager.py — Mila's diary management.
"""

import os
import sqlite3
from datetime import datetime
from typing import List, Dict, Optional

from config import settings
from core.context.buffer import _get_conn as _get_context_conn

DIARY_TABLE = "diary_entries"

DAILY_REPORTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "daily reports"
)


def _ensure_daily_reports_dir():
    os.makedirs(DAILY_REPORTS_DIR, exist_ok=True)


def _get_today_date_str() -> str:
    return datetime.now().strftime("%d.%m.%Y")


def _daily_filepath(date_str: str = None) -> str:
    if date_str is None:
        date_str = _get_today_date_str()
    return os.path.join(DAILY_REPORTS_DIR, f"{date_str}.md")


def _ensure_diary_table():
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


def _sync_to_daily_file():
    _ensure_daily_reports_dir()
    entries = get_all_diary_entries()
    if not entries:
        return

    date_str = _get_today_date_str()
    filepath = _daily_filepath(date_str)

    lines = [f"# Личный дневник — {date_str}", ""]
    for entry in entries:
        created = entry.get("created_at", "")[:16]
        lines.append(f"## Запись #{entry['entry_number']} — {created}")
        lines.append("")
        lines.append(entry["content_md"])
        lines.append("")
        lines.append("---")
        lines.append("")

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception as e:
        print(f"[Diary] Failed to sync daily file: {e}")


def add_diary_entry(content_md: str) -> int:
    _ensure_diary_table()
    conn = _get_context_conn()
    try:
        cur = conn.execute(f"SELECT COALESCE(MAX(entry_number), 0) + 1 as next_num FROM {DIARY_TABLE}")
        row = cur.fetchone()
        next_num = row["next_num"] if row else 1

        conn.execute(
            f"INSERT INTO {DIARY_TABLE} (entry_number, content_md) VALUES (?, ?)",
            (next_num, content_md)
        )
        conn.commit()
        print(f"[Diary] Entry #{next_num} added")

        _sync_to_daily_file()

        return next_num
    finally:
        conn.close()


def get_all_diary_entries() -> List[Dict]:
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
    _ensure_diary_table()
    conn = _get_context_conn()
    try:
        cur = conn.execute(f"SELECT COUNT(*) as cnt FROM {DIARY_TABLE}")
        row = cur.fetchone()
        return row["cnt"] if row else 0
    finally:
        conn.close()


def clear_diary():
    _ensure_diary_table()
    conn = _get_context_conn()
    try:
        conn.execute(f"DELETE FROM {DIARY_TABLE}")
        conn.commit()
        print("[Diary] All entries cleared")
    finally:
        conn.close()


def save_diary_to_logs():
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


def load_daily_report(date_str: str = None) -> str:
    _ensure_daily_reports_dir()
    filepath = _daily_filepath(date_str)

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""
    except Exception as e:
        print(f"[Diary] Failed to load daily report {filepath}: {e}")
        return ""


def list_daily_reports() -> List[str]:
    _ensure_daily_reports_dir()
    try:
        files = os.listdir(DAILY_REPORTS_DIR)
        reports = []
        for f in files:
            if f.endswith(".md"):
                date_str = f[:-3]
                reports.append(date_str)
        return sorted(reports, reverse=True)
    except Exception as e:
        print(f"[Diary] Failed to list daily reports: {e}")
        return []


def get_today_diary_context() -> str:
    today_md = load_daily_report()
    if not today_md:
        return ""

    return f"""
[Твой личный дневник за сегодня:]
---
{today_md.strip()}
---
"""
