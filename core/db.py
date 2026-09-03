import os
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Optional, Any
from config import settings

DB_PATH = settings.SQLITE_PATH

def get_conn():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        user_name TEXT,
        preferred_name TEXT,
        relationship_type TEXT DEFAULT 'friend',
        sentiment REAL DEFAULT 0.0,
        appearance_description TEXT
    );
    CREATE TABLE IF NOT EXISTS chats (
        chat_id TEXT PRIMARY KEY,
        chat_name TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS chat_members (
        chat_id TEXT,
        user_id TEXT,
        role TEXT DEFAULT 'user',
        PRIMARY KEY (chat_id, user_id),
        FOREIGN KEY(chat_id) REFERENCES chats(chat_id),
        FOREIGN KEY(user_id) REFERENCES users(user_id)
    );
    CREATE TABLE IF NOT EXISTS chunks (
        chunk_id TEXT PRIMARY KEY,
        chat_id TEXT NOT NULL,
        user_id TEXT,
        content TEXT,
        llm_full_response TEXT,
        type TEXT DEFAULT 'raw',
        faiss_id INTEGER,
        token_count INTEGER,
        time_start TEXT,
        time_end TEXT,
        FOREIGN KEY(chat_id) REFERENCES chats(chat_id)
    );
    CREATE TABLE IF NOT EXISTS memories (
        mem_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        faiss_id INTEGER,
        type TEXT DEFAULT 'fact',
        tag TEXT,
        content TEXT,
        importance REAL DEFAULT 0.5,
        confidence_source TEXT DEFAULT 'llm_extraction',
        confidence_verified INTEGER DEFAULT 0,
        last_accessed_at TEXT,
        time TEXT DEFAULT (datetime('now')),
        source_chat_id TEXT,
        source_message_id TEXT,
        FOREIGN KEY(user_id) REFERENCES users(user_id)
    );
    CREATE TABLE IF NOT EXISTS media_logs (
        media_id TEXT PRIMARY KEY,
        chat_id TEXT,
        user_id TEXT,
        timestamp TEXT DEFAULT (datetime('now')),
        file_path TEXT,
        vlm_description TEXT,
        FOREIGN KEY(chat_id) REFERENCES chats(chat_id),
        FOREIGN KEY(user_id) REFERENCES users(user_id)
    );
    """)
    conn.commit()
    conn.close()
    print("Database initialized.")

def ensure_user_exists(user_id: str, user_name: str = None):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO users (user_id, user_name) VALUES (?, ?)",
                (user_id, user_name)
            )
            conn.commit()
    finally:
        conn.close()

def ensure_chat_exists(chat_id: str, chat_name: str = None):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT chat_id FROM chats WHERE chat_id=?", (chat_id,))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO chats (chat_id, chat_name) VALUES (?, ?)",
                (chat_id, chat_name)
            )
            conn.commit()
    finally:
        conn.close()

def add_chunk(chat_id: str, user_id: str, content: str, token_count: int = 0, type: str = 'raw', llm_full_response: str = None):
    chunk_id = str(uuid.uuid4())
    time_now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO chunks (chunk_id, chat_id, user_id, content, llm_full_response, type, token_count, time_start) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (chunk_id, chat_id, user_id, content, llm_full_response, type, token_count, time_now)
        )
        conn.commit()
        return chunk_id
    finally:
        conn.close()

def get_recent_chunks(chat_id: str, limit: int = 30) -> list[dict]:
    """Returns the most recent N messages in the chat in chronological order (old -> new)."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT chunk_id, user_id, content, llm_full_response, type, time_start FROM chunks WHERE chat_id=? ORDER BY time_start ASC LIMIT ? OFFSET (SELECT MAX(0, COUNT(*)-?) FROM chunks WHERE chat_id=?)",
            (chat_id, limit, limit, chat_id)
        )
        rows = cur.fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()

def get_user_info(user_id: str) -> dict:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def update_user_name(user_id: str, user_name: str):
    conn = get_conn()
    try:
        conn.execute("UPDATE users SET user_name=? WHERE user_id=?", (user_name, user_id))
        conn.commit()
    finally:
        conn.close()

def add_memory(user_id: str, content: str, importance: float = 0.5, tags: list[str] = None, source_chat_id: str = None, source_message_id: str = None, faiss_id: int = None):
    mem_id = str(uuid.uuid4())
    time_now = datetime.now(timezone.utc).isoformat()
    tags_json = json.dumps(tags) if tags else '[]'
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO memories (mem_id, user_id, content, importance, tag, time, source_chat_id, source_message_id, faiss_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mem_id, user_id, content, importance, tags_json, time_now, source_chat_id, source_message_id, faiss_id)
        )
        conn.commit()
        return mem_id
    finally:
        conn.close()

def search_memories_by_ids(mem_ids: list[str]) -> list[dict]:
    conn = get_conn()
    try:
        placeholders = ','.join('?' for _ in mem_ids)
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM memories WHERE mem_id IN ({placeholders})", mem_ids)
        rows = cur.fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()

def search_memories_by_faiss_ids(faiss_ids: list[int]) -> list[dict]:
    """Search memories by FAISS integer IDs."""
    if not faiss_ids:
        return []
    conn = get_conn()
    try:
        placeholders = ','.join('?' for _ in faiss_ids)
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM memories WHERE faiss_id IN ({placeholders})", faiss_ids)
        rows = cur.fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()

def update_memory_access(mem_ids: list[str]):
    now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    try:
        conn.executemany("UPDATE memories SET last_accessed_at=? WHERE mem_id=?", [(now, mid) for mid in mem_ids])
        conn.commit()
    finally:
        conn.close()


def get_all_chat_ids_from_chunks() -> list[str]:
    """Get all distinct chat IDs from chunks table."""
    conn = get_conn()
    try:
        cur = conn.execute("SELECT DISTINCT chat_id FROM chunks")
        return [r["chat_id"] for r in cur.fetchall()]
    finally:
        conn.close()

