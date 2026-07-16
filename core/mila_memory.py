"""
core/mila_memory.py — Mila's eternal (autobiographical) memory.

This is Mila's own life experience memory, completely separate from
user memories. It stores only important events of Mila's own life:
- Serious personality changes
- Extremely important experiences
- Events that influenced future behavior

This is NOT a diary. This is autobiographical memory.

Storage:
- Each chunk is a Markdown text
- Each chunk is a separate DB record in mila.db
- Each chunk has its own metadata
- Chunks can be updated (not just duplicated) as personality develops
"""

import json
import uuid
from datetime import datetime
from typing import List, Dict, Optional, Any

from core.db import get_conn


def _ensure_table():
    """Create mila_memory_chunks table in mila.db if not exists."""
    conn = get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mila_memory_chunks (
                chunk_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content_md TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                version INTEGER DEFAULT 1,
                is_active INTEGER DEFAULT 1,
                importance REAL DEFAULT 0.5
            )
        """)
        conn.commit()
    finally:
        conn.close()


def add_chunk(title: str, content_md: str, importance: float = 0.5) -> str:
    """
    Add a new chunk to Mila's eternal memory.
    Returns the chunk_id.
    """
    _ensure_table()
    chunk_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO mila_memory_chunks 
               (chunk_id, title, content_md, created_at, updated_at, version, is_active, importance)
               VALUES (?, ?, ?, ?, ?, 1, 1, ?)""",
            (chunk_id, title, content_md, now, now, importance)
        )
        conn.commit()
        print(f"[MilaMemory] Added chunk '{title}' ({chunk_id[:8]}...)")
        return chunk_id
    finally:
        conn.close()


def update_chunk(chunk_id: str, title: str = None, content_md: str = None, 
                 importance: float = None) -> bool:
    """
    Update an existing chunk. This is how personality develops — 
    existing chunks are updated rather than creating duplicates.
    Returns True if successful.
    """
    _ensure_table()
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT version FROM mila_memory_chunks WHERE chunk_id=? AND is_active=1",
            (chunk_id,)
        )
        row = cur.fetchone()
        if not row:
            print(f"[MilaMemory] Chunk {chunk_id} not found or inactive")
            return False

        current_version = row["version"]
        now = datetime.utcnow().isoformat()

        updates = ["updated_at=?", "version=?"]
        params = [now, current_version + 1]

        if title is not None:
            updates.append("title=?")
            params.append(title)
        if content_md is not None:
            updates.append("content_md=?")
            params.append(content_md)
        if importance is not None:
            updates.append("importance=?")
            params.append(importance)

        params.append(chunk_id)
        conn.execute(
            f"UPDATE mila_memory_chunks SET {', '.join(updates)} WHERE chunk_id=?",
            params
        )
        conn.commit()
        print(f"[MilaMemory] Updated chunk '{chunk_id[:8]}...' to v{current_version + 1}")
        return True
    finally:
        conn.close()


def deactivate_chunk(chunk_id: str) -> bool:
    """
    Soft-delete a chunk by marking it inactive.
    This preserves history while removing it from active memory.
    """
    _ensure_table()
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE mila_memory_chunks SET is_active=0, updated_at=? WHERE chunk_id=?",
            (datetime.utcnow().isoformat(), chunk_id)
        )
        conn.commit()
        print(f"[MilaMemory] Deactivated chunk {chunk_id[:8]}...")
        return True
    finally:
        conn.close()


def get_all_active_chunks() -> List[Dict]:
    """Get all active chunks in chronological order."""
    _ensure_table()
    conn = get_conn()
    try:
        cur = conn.execute(
            """SELECT chunk_id, title, content_md, created_at, updated_at, 
                      version, is_active, importance 
               FROM mila_memory_chunks 
               WHERE is_active=1 
               ORDER BY created_at ASC"""
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_chunk(chunk_id: str) -> Optional[Dict]:
    """Get a single chunk by ID."""
    _ensure_table()
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT * FROM mila_memory_chunks WHERE chunk_id=?",
            (chunk_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_chunks_metadata() -> List[Dict]:
    """
    Get metadata of all active chunks (without content_md).
    Used by LLM to decide what to update during sleep.
    """
    _ensure_table()
    conn = get_conn()
    try:
        cur = conn.execute(
            """SELECT chunk_id, title, created_at, updated_at, 
                      version, importance 
               FROM mila_memory_chunks 
               WHERE is_active=1 
               ORDER BY created_at ASC"""
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_all_chunks_markdown() -> str:
    """
    Get all active chunks as a single Markdown document.
    Used by LLM during sleep analysis.
    """
    chunks = get_all_active_chunks()
    if not chunks:
        return "[No eternal memory chunks yet]"

    lines = ["# Mila's Eternal Memory", ""]
    for chunk in chunks:
        lines.append(f"## {chunk['title']}")
        lines.append(f"*Created: {chunk['created_at']} | Updated: {chunk['updated_at']} | v{chunk['version']}*")
        lines.append("")
        lines.append(chunk['content_md'])
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def get_chunk_count() -> int:
    """Get number of active chunks."""
    _ensure_table()
    conn = get_conn()
    try:
        cur = conn.execute("SELECT COUNT(*) as cnt FROM mila_memory_chunks WHERE is_active=1")
        row = cur.fetchone()
        return row["cnt"] if row else 0
    finally:
        conn.close()