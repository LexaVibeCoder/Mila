"""
core/context/buffer.py — Context Buffer for Mila's working memory.
"""

import os
import sqlite3
from datetime import datetime
from typing import List, Dict
from zoneinfo import ZoneInfo

from config import settings
from core.llm.counter import count_tokens


CONTEXT_DB_PATH = settings.CONTEXT_DB_PATH


def _local_zone():
    """Return the configured IANA timezone, falling back to the OS local zone."""
    if getattr(settings, "LOCAL_TIMEZONE", ""):
        try:
            return ZoneInfo(settings.LOCAL_TIMEZONE)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo


def now_local() -> datetime:
    """Current datetime in the local timezone (config-aware, Docker-safe)."""
    return datetime.now(_local_zone())


def _get_conn():
    db_dir = os.path.dirname(CONTEXT_DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    # timeout=30 + busy_timeout: bot and Observatory write from two
    # processes — wait instead of failing with "database is locked".
    conn = sqlite3.connect(CONTEXT_DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_name(chat_id: str) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in chat_id)
    return f"chat_{safe}"


def ensure_chat_table(chat_id: str):
    table = _table_name(chat_id)
    conn = _get_conn()
    try:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS [{table}] (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('message', 'summary')),
                source_chunk_id TEXT,
                token_count INTEGER DEFAULT 0,
                role TEXT NOT NULL DEFAULT '',
                kind TEXT DEFAULT 'text',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        try:
            conn.execute(f"ALTER TABLE [{table}] ADD COLUMN token_count INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(f"ALTER TABLE [{table}] ADD COLUMN role TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(f"ALTER TABLE [{table}] ADD COLUMN kind TEXT DEFAULT 'text'")
        except sqlite3.OperationalError:
            pass
        conn.commit()
    finally:
        conn.close()


def push_message(chat_id: str, content: str, source_chunk_id: str = None, role: str = "user", kind: str = "text"):
    ensure_chat_table(chat_id)
    table = _table_name(chat_id)
    token_count = count_tokens(content)
    ts = now_local().strftime("%Y-%m-%d %H:%M:%S")
    conn = _get_conn()
    try:
        try:
            conn.execute(
                f"INSERT INTO [{table}] (content, type, source_chunk_id, token_count, role, kind, created_at) VALUES (?, 'message', ?, ?, ?, ?, ?)",
                (content, source_chunk_id, token_count, role, kind, ts)
            )
        except sqlite3.OperationalError:
            # Old table without kind column
            conn.execute(
                f"INSERT INTO [{table}] (content, type, source_chunk_id, token_count, role, created_at) VALUES (?, 'message', ?, ?, ?, ?)",
                (content, source_chunk_id, token_count, role, ts)
            )
        conn.commit()
    finally:
        conn.close()


def push_summary(chat_id: str, summary_text: str):
    ensure_chat_table(chat_id)
    table = _table_name(chat_id)
    token_count = count_tokens(summary_text)
    ts = now_local().strftime("%Y-%m-%d %H:%M:%S")
    conn = _get_conn()
    try:
        conn.execute(
            f"INSERT INTO [{table}] (content, type, token_count, created_at) VALUES (?, 'summary', ?, ?)",
            (summary_text, token_count, ts)
        )
        conn.commit()
    finally:
        conn.close()


def get_context(chat_id: str, limit: int = None) -> List[Dict]:
    if limit is None:
        limit = settings.MAX_STM_MESSAGES
    ensure_chat_table(chat_id)
    table = _table_name(chat_id)
    conn = _get_conn()
    try:
        cur = conn.execute(
            f"SELECT id, content, type, source_chunk_id, token_count, role, created_at FROM [{table}] ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        rows = [dict(r) for r in cur.fetchall()]
        rows.reverse()
        return rows
    finally:
        conn.close()


def get_total_entries(chat_id: str) -> int:
    ensure_chat_table(chat_id)
    table = _table_name(chat_id)
    conn = _get_conn()
    try:
        cur = conn.execute(f"SELECT COUNT(*) as cnt FROM [{table}]")
        row = cur.fetchone()
        return row["cnt"] if row else 0
    finally:
        conn.close()


def get_total_message_tokens(chat_id: str) -> int:
    ensure_chat_table(chat_id)
    table = _table_name(chat_id)
    conn = _get_conn()
    try:
        cur = conn.execute(
            f"SELECT COALESCE(SUM(token_count), 0) as total_tokens FROM [{table}] WHERE type='message'"
        )
        row = cur.fetchone()
        return row["total_tokens"] if row else 0
    finally:
        conn.close()


def get_context_within_token_limit(chat_id: str, max_tokens: int) -> List[Dict]:
    ensure_chat_table(chat_id)
    table = _table_name(chat_id)
    conn = _get_conn()
    try:
        cur = conn.execute(
            f"SELECT id, content, type, source_chunk_id, token_count, role, created_at FROM [{table}] ORDER BY id ASC"
        )
        all_entries = [dict(r) for r in cur.fetchall()]

        summaries = [e for e in all_entries if e["type"] == "summary"]
        messages = [e for e in all_entries if e["type"] == "message"]

        result = list(summaries)

        remaining_tokens = max_tokens - sum(s.get("token_count", 0) or 0 for s in summaries)

        selected_messages = []
        for msg in reversed(messages):
            msg_tokens = msg.get("token_count", 0) or count_tokens(msg.get("content", ""))
            if msg_tokens <= remaining_tokens:
                selected_messages.append(msg)
                remaining_tokens -= msg_tokens
            else:
                break

        selected_messages.reverse()
        result.extend(selected_messages)

        return result
    finally:
        conn.close()


def peek_oldest_messages_by_tokens(chat_id: str, target_tokens: int) -> List[Dict]:
    """Read-only preview of the oldest messages (~target_tokens). No deletion."""
    ensure_chat_table(chat_id)
    table = _table_name(chat_id)
    conn = _get_conn()
    try:
        cur = conn.execute(
            f"SELECT id, content, type, source_chunk_id, token_count, role, created_at FROM [{table}] WHERE type='message' ORDER BY id ASC"
        )
        all_messages = [dict(r) for r in cur.fetchall()]

        if not all_messages:
            return []

        selected = []
        accumulated_tokens = 0
        for msg in all_messages:
            msg_tokens = msg.get("token_count", 0) or count_tokens(msg.get("content", ""))
            selected.append(msg)
            accumulated_tokens += msg_tokens
            if accumulated_tokens >= target_tokens:
                break

        return selected
    finally:
        conn.close()


def delete_messages_by_ids(chat_id: str, ids: List[int]) -> int:
    """Delete messages by row ids. Returns the number of rows removed."""
    if not ids:
        return 0
    ensure_chat_table(chat_id)
    table = _table_name(chat_id)
    conn = _get_conn()
    try:
        placeholders = ",".join("?" for _ in ids)
        cur = conn.execute(
            f"DELETE FROM [{table}] WHERE id IN ({placeholders})",
            ids,
        )
        conn.commit()
        return cur.rowcount if cur.rowcount is not None else 0
    finally:
        conn.close()


def pop_oldest_messages_by_tokens(chat_id: str, target_tokens: int) -> List[Dict]:
    """Legacy atomic pop (peek + delete). Prefer peek → summarize → delete."""
    selected = peek_oldest_messages_by_tokens(chat_id, target_tokens)
    if not selected:
        return []
    delete_messages_by_ids(chat_id, [r["id"] for r in selected])
    return selected


def pop_oldest_messages(chat_id: str, count: int) -> List[Dict]:
    ensure_chat_table(chat_id)
    table = _table_name(chat_id)
    conn = _get_conn()
    try:
        cur = conn.execute(
            f"SELECT id, content, type, source_chunk_id, token_count, role, created_at FROM [{table}] WHERE type='message' ORDER BY id ASC LIMIT ?",
            (count,)
        )
        to_remove = [dict(r) for r in cur.fetchall()]

        if not to_remove:
            return []

        ids_to_remove = [r["id"] for r in to_remove]

        placeholders = ",".join("?" for _ in ids_to_remove)
        conn.execute(
            f"DELETE FROM [{table}] WHERE id IN ({placeholders})",
            ids_to_remove
        )
        conn.commit()

        return to_remove
    finally:
        conn.close()


def get_all_chat_ids() -> List[str]:
    conn = _get_conn()
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'chat_%'")
        tables = [r["name"] for r in cur.fetchall()]
        return tables
    finally:
        conn.close()


def get_raw_content_for_llm(chat_id: str, limit: int = None) -> str:
    context = get_context(chat_id, limit=limit)
    if not context:
        return ""

    summaries = [e for e in context if e["type"] == "summary"]
    messages = [e for e in context if e["type"] == "message"]

    parts = []

    if summaries:
        summary_texts = "\n\n".join(s["content"] for s in summaries)
        parts.append(f"[summary of previous conversation:]\n{summary_texts}")

    if messages:
        msg_texts = []
        for m in messages:
            msg_texts.append(m["content"])
        parts.append(f"[latest messages:]\n" + "\n".join(msg_texts))

    return "\n\n".join(parts)
