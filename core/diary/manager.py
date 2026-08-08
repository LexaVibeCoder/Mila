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

DIARY_CATEGORIES = ("day_entries", "reflection_entries")

DIARY_CATEGORY_LABELS = {
    "day_entries": "События дня",
    "reflection_entries": "Мысли и ощущения",
}


def _normalize_category(category) -> str:
    if category not in DIARY_CATEGORIES:
        return "day_entries"
    return category


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
                category TEXT NOT NULL DEFAULT 'day_entries',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        try:
            conn.execute(f"ALTER TABLE {DIARY_TABLE} ADD COLUMN category TEXT NOT NULL DEFAULT 'day_entries'")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(f"ALTER TABLE {DIARY_TABLE} ADD COLUMN updated_at TEXT DEFAULT (datetime('now'))")
        except sqlite3.OperationalError:
            pass
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
    for category in DIARY_CATEGORIES:
        cat_entries = [e for e in entries if e.get("category") == category]
        if not cat_entries:
            continue
        label = DIARY_CATEGORY_LABELS.get(category, category)
        lines.append(f"## {label} ({category})")
        lines.append("")
        for entry in cat_entries:
            created = entry.get("created_at", "")[:16]
            lines.append(f"## Запись #{entry['entry_number']} — {created} (ID: {entry['id']})")
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


def add_diary_entry(content_md: str, category: str = "day_entries") -> Dict:
    category = _normalize_category(category)
    _ensure_diary_table()
    conn = _get_context_conn()
    try:
        cur = conn.execute(f"SELECT COALESCE(MAX(entry_number), 0) + 1 as next_num FROM {DIARY_TABLE}")
        row = cur.fetchone()
        next_num = row["next_num"] if row else 1

        cur = conn.execute(
            f"INSERT INTO {DIARY_TABLE} (entry_number, content_md, category) VALUES (?, ?, ?)",
            (next_num, content_md, category)
        )
        conn.commit()
        entry_id = cur.lastrowid
        print(f"[Diary] Entry #{next_num} (ID: {entry_id}, {category}) added")

        _sync_to_daily_file()

        return {"entry_id": entry_id, "entry_number": next_num, "category": category}
    finally:
        conn.close()


def update_diary_entry(entry_id: int, content_md: str, category: str = None) -> bool:
    """Update an existing diary entry by its ID. Returns False if not found."""
    _ensure_diary_table()
    conn = _get_context_conn()
    try:
        if category is not None:
            category = _normalize_category(category)
            cur = conn.execute(
                f"UPDATE {DIARY_TABLE} SET content_md=?, category=?, updated_at=datetime('now') WHERE id=?",
                (content_md, category, entry_id)
            )
        else:
            cur = conn.execute(
                f"UPDATE {DIARY_TABLE} SET content_md=?, updated_at=datetime('now') WHERE id=?",
                (content_md, entry_id)
            )
        conn.commit()
        if cur.rowcount == 0:
            print(f"[Diary] Entry ID {entry_id} not found — nothing updated")
            return False
        print(f"[Diary] Entry ID {entry_id} updated")
        _sync_to_daily_file()
        return True
    finally:
        conn.close()


def get_all_diary_entries(category: str = None) -> List[Dict]:
    _ensure_diary_table()
    conn = _get_context_conn()
    try:
        if category is not None:
            category = _normalize_category(category)
            cur = conn.execute(
                f"SELECT id, entry_number, content_md, category, created_at, updated_at FROM {DIARY_TABLE} "
                f"WHERE category=? ORDER BY entry_number ASC",
                (category,)
            )
        else:
            cur = conn.execute(
                f"SELECT id, entry_number, content_md, category, created_at, updated_at FROM {DIARY_TABLE} "
                f"ORDER BY entry_number ASC"
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
        category = entry.get("category") or "day_entries"
        lines.append(f"[{entry['entry_number']:03d}] ({category}, ID: {entry['id']})")
        lines.append(entry["content_md"])
        lines.append("")

    return "\n".join(lines)


def get_diary_entries_markdown(category: str = None) -> str:
    """Markdown of entries (optionally by category) with IDs — used for prompts."""
    entries = get_all_diary_entries(category)
    if not entries:
        return ""
    lines = ["# Diary", ""]
    for entry in entries:
        lines.append(f"[{entry['entry_number']:03d}] ({entry['category']}, ID: {entry['id']})")
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


def get_today_diary_context(max_entries: int = 30) -> str:
    """Today's diary formatted for the system prompt: last `max_entries`, grouped
    by category, each with its entry ID so the model can update them."""
    entries = get_all_diary_entries()
    if not entries:
        return ""

    entries = entries[-max_entries:]

    blocks = []
    for category in DIARY_CATEGORIES:
        cat_entries = [e for e in entries if e.get("category") == category]
        if not cat_entries:
            continue
        label = DIARY_CATEGORY_LABELS.get(category, category)
        lines = [f"## {label} ({category}):"]
        for entry in cat_entries:
            lines.append(f"[ID: {entry['id']}] {entry['content_md']}")
        blocks.append("\n".join(lines))

    if not blocks:
        return ""

    return f"""
[Твой личный дневник за сегодня. У каждой записи есть ID — используй update_diary, чтобы обновить существующую запись:]
---
{chr(10).join(blocks)}
---
"""
