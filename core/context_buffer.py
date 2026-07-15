"""
core/context_buffer.py — Context Buffer for Mila's working memory.

Manages a separate SQLite database (context_buffer.db) that stores ONLY
the messages that are part of the LLM's active context window, plus
summaries of older conversation segments.

This is the "daytime" context store. It is periodically compressed:
oldest raw messages → summary, to keep the context within limits.

Table-per-chat schema:
    chat_{chat_id} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        type TEXT NOT NULL CHECK(type IN ('message', 'summary')),
        source_chunk_id TEXT,
        token_count INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    )
"""

import os
import sqlite3
import uuid
from datetime import datetime
from typing import List, Dict, Optional, Tuple

from config import settings
from core.token_counter import count_tokens


CONTEXT_DB_PATH = settings.CONTEXT_DB_PATH


def _get_conn():
    """Get connection to context buffer DB."""
    db_dir = os.path.dirname(CONTEXT_DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(CONTEXT_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _table_name(chat_id: str) -> str:
    """Sanitize chat_id for use as a table name."""
    # Replace non-alphanumeric chars with underscore
    safe = "".join(c if c.isalnum() else "_" for c in chat_id)
    return f"chat_{safe}"


def ensure_chat_table(chat_id: str):
    """Create the table for a given chat if it doesn't exist.
    Also migrates schema to add token_count column if missing."""
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
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Migrate: add token_count column if it doesn't exist (for legacy tables)
        try:
            conn.execute(f"ALTER TABLE [{table}] ADD COLUMN token_count INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # Column already exists
        conn.commit()
    finally:
        conn.close()


def push_message(chat_id: str, content: str, source_chunk_id: str = None):
    """Add a raw message to the context buffer (appended at end / newest).
    Automatically counts tokens for the message."""
    ensure_chat_table(chat_id)
    table = _table_name(chat_id)
    token_count = count_tokens(content)
    conn = _get_conn()
    try:
        conn.execute(
            f"INSERT INTO [{table}] (content, type, source_chunk_id, token_count) VALUES (?, 'message', ?, ?)",
            (content, source_chunk_id, token_count)
        )
        conn.commit()
    finally:
        conn.close()


def push_summary(chat_id: str, summary_text: str):
    """Add a summary entry to the context buffer.
    Summaries also get token_count for accurate tracking."""
    ensure_chat_table(chat_id)
    table = _table_name(chat_id)
    token_count = count_tokens(summary_text)
    conn = _get_conn()
    try:
        conn.execute(
            f"INSERT INTO [{table}] (content, type, token_count) VALUES (?, 'summary', ?)",
            (summary_text, token_count)
        )
        conn.commit()
    finally:
        conn.close()


def get_context(chat_id: str, limit: int = None) -> List[Dict]:
    """
    Get the current context window for a chat, in CHRONOLOGICAL order (oldest first).
    Returns list of {id, content, type, source_chunk_id, token_count, created_at}.
    """
    if limit is None:
        limit = settings.MAX_STM_MESSAGES
    ensure_chat_table(chat_id)
    table = _table_name(chat_id)
    conn = _get_conn()
    try:
        # Get the last N entries (newest first), then reverse to chronological
        cur = conn.execute(
            f"SELECT id, content, type, source_chunk_id, token_count, created_at FROM [{table}] ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        rows = [dict(r) for r in cur.fetchall()]
        rows.reverse()  # chronological order
        return rows
    finally:
        conn.close()


def get_total_entries(chat_id: str) -> int:
    """Get total number of entries in the context buffer for a chat."""
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
    """
    Get total token count for ALL message-type entries in the context buffer.
    This is used to determine if compression is needed.
    Summaries are NOT counted here — they are part of the compressed context.
    """
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
    """
    Get context buffer entries that fit within max_tokens.
    Returns entries in chronological order, including summaries first,
    then as many recent messages as fit within the token limit.
    
    This is the token-aware replacement for get_context().
    """
    ensure_chat_table(chat_id)
    table = _table_name(chat_id)
    conn = _get_conn()
    try:
        # Get ALL entries in chronological order
        cur = conn.execute(
            f"SELECT id, content, type, source_chunk_id, token_count, created_at FROM [{table}] ORDER BY id ASC"
        )
        all_entries = [dict(r) for r in cur.fetchall()]
        
        # Separate summaries and messages
        summaries = [e for e in all_entries if e["type"] == "summary"]
        messages = [e for e in all_entries if e["type"] == "message"]
        
        # Summaries always fit (they're already compressed)
        result = list(summaries)
        
        # Add messages from newest to oldest until we hit the token limit
        remaining_tokens = max_tokens - sum(s.get("token_count", 0) or 0 for s in summaries)
        
        # Process messages newest-first to keep the most recent
        selected_messages = []
        for msg in reversed(messages):
            msg_tokens = msg.get("token_count", 0) or count_tokens(msg.get("content", ""))
            if msg_tokens <= remaining_tokens:
                selected_messages.append(msg)
                remaining_tokens -= msg_tokens
            else:
                break
        
        # Reverse back to chronological order
        selected_messages.reverse()
        result.extend(selected_messages)
        
        return result
    finally:
        conn.close()


def pop_oldest_messages_by_tokens(chat_id: str, target_tokens: int) -> List[Dict]:
    """
    Remove and return the oldest message entries until we've removed
    at least `target_tokens` tokens worth of messages.
    Returns the removed entries in chronological order.
    Summaries are skipped (not removed).
    """
    ensure_chat_table(chat_id)
    table = _table_name(chat_id)
    conn = _get_conn()
    try:
        # Get all message entries oldest first
        cur = conn.execute(
            f"SELECT id, content, type, source_chunk_id, token_count, created_at FROM [{table}] WHERE type='message' ORDER BY id ASC"
        )
        all_messages = [dict(r) for r in cur.fetchall()]
        
        if not all_messages:
            return []
        
        # Select oldest messages until we hit target_tokens
        to_remove = []
        accumulated_tokens = 0
        for msg in all_messages:
            msg_tokens = msg.get("token_count", 0) or count_tokens(msg.get("content", ""))
            to_remove.append(msg)
            accumulated_tokens += msg_tokens
            if accumulated_tokens >= target_tokens:
                break
        
        if not to_remove:
            return []
        
        ids_to_remove = [r["id"] for r in to_remove]
        
        # Delete them
        placeholders = ",".join("?" for _ in ids_to_remove)
        conn.execute(
            f"DELETE FROM [{table}] WHERE id IN ({placeholders})",
            ids_to_remove
        )
        conn.commit()
        
        return to_remove
    finally:
        conn.close()


def pop_oldest_messages(chat_id: str, count: int) -> List[Dict]:
    """
    Legacy: Remove and return the oldest `count` message entries (type='message' only).
    Returns the removed entries in chronological order.
    Summaries are skipped (not removed).
    """
    ensure_chat_table(chat_id)
    table = _table_name(chat_id)
    conn = _get_conn()
    try:
        # Find the oldest N type='message' entries
        cur = conn.execute(
            f"SELECT id, content, type, source_chunk_id, token_count, created_at FROM [{table}] WHERE type='message' ORDER BY id ASC LIMIT ?",
            (count,)
        )
        to_remove = [dict(r) for r in cur.fetchall()]
        
        if not to_remove:
            return []
        
        ids_to_remove = [r["id"] for r in to_remove]
        
        # Delete them
        placeholders = ",".join("?" for _ in ids_to_remove)
        conn.execute(
            f"DELETE FROM [{table}] WHERE id IN ({placeholders})",
            ids_to_remove
        )
        conn.commit()
        
        return to_remove
    finally:
        conn.close()


def replace_all_with_summary(chat_id: str, summary_text: str):
    """
    Replace ALL entries for a chat with a single summary entry.
    Used by the night pipeline to reset the context buffer for the new day.
    """
    ensure_chat_table(chat_id)
    table = _table_name(chat_id)
    conn = _get_conn()
    try:
        conn.execute(f"DELETE FROM [{table}]")
        conn.execute(
            f"INSERT INTO [{table}] (content, type) VALUES (?, 'summary')",
            (summary_text,)
        )
        conn.commit()
    finally:
        conn.close()


def get_all_chat_ids() -> List[str]:
    """
    Get all chat IDs that have context tables.
    Strips the 'chat_' prefix from table names.
    """
    conn = _get_conn()
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'chat_%'")
        tables = [r["name"] for r in cur.fetchall()]
        # Extract original chat_id (undo sanitization — best effort)
        # We stored sanitized names, but we assume the original chat_id is reconstructable
        return tables
    finally:
        conn.close()


def get_raw_content_for_llm(chat_id: str, limit: int = None) -> str:
    """
    Get the formatted context text for LLM injection.
    Format: summaries first (oldest), then recent messages.

    Returns a single string like:
    "[Сводка предыдущего разговора:]
    <summary text>

    [Последние сообщения:]
    user: hello
    assistant: hi
    user: how are you?"
    """
    context = get_context(chat_id, limit=limit)
    if not context:
        return ""
    
    summaries = [e for e in context if e["type"] == "summary"]
    messages = [e for e in context if e["type"] == "message"]
    
    parts = []
    
    if summaries:
        summary_texts = "\n\n".join(s["content"] for s in summaries)
        parts.append(f"[Сводка предыдущего разговора:]\n{summary_texts}")
    
    if messages:
        msg_texts = []
        for m in messages:
            # We don't know the role here (we stripped it for storage).
            # The caller (pipeline) will handle role assignment.
            msg_texts.append(m["content"])
        parts.append(f"[Последние сообщения:]\n" + "\n".join(msg_texts))
    
    return "\n\n".join(parts)