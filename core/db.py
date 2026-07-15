# core/db.py
import os
import json
import sqlite3
import uuid
from datetime import datetime
from typing import List, Dict, Optional, Any
from config import settings

DB_PATH = settings.SQLITE_PATH

def get_conn():
    # Создаем директорию для базы данных, если она не существует
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    # Подключаемся к базе (get_conn создаст директорию, если нужно)
    conn = get_conn()
    cur = conn.cursor()
    
    # Всегда создаем таблицы, если они не существуют
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
    -- Night pipeline: chat episodes (summaries of conversation segments)
    CREATE TABLE IF NOT EXISTS chat_episodes (
        episode_id TEXT PRIMARY KEY,
        chat_id TEXT NOT NULL,
        summary TEXT,
        message_range_start TEXT,
        message_range_end TEXT,
        keywords TEXT,
        topics TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(chat_id) REFERENCES chats(chat_id)
    );
    -- Night pipeline: extracted facts (offline only)
    CREATE TABLE IF NOT EXISTS chat_facts (
        fact_id TEXT PRIMARY KEY,
        chat_id TEXT NOT NULL,
        fact_text TEXT,
        confidence REAL DEFAULT 0.5,
        source_message_ids TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(chat_id) REFERENCES chats(chat_id)
    );
    -- Night pipeline: tracking which messages have been processed
    CREATE TABLE IF NOT EXISTS offline_processing_tracker (
        chat_id TEXT NOT NULL,
        last_processed_chunk_id TEXT,
        processed_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (chat_id)
    );
    -- Sleep/wake mechanism
    CREATE TABLE IF NOT EXISTS sleep_flags (
        flag_name TEXT PRIMARY KEY,
        flag_value TEXT NOT NULL DEFAULT '0'
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
    time_now = datetime.utcnow().isoformat()
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
    """Возвращает последние N сообщений в чате в хронологическом порядке (старые -> новые)."""
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
    time_now = datetime.utcnow().isoformat()
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
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    try:
        conn.executemany("UPDATE memories SET last_accessed_at=? WHERE mem_id=?", [(now, mid) for mid in mem_ids])
        conn.commit()
    finally:
        conn.close()

# =====================================================================
# Night pipeline: Episodes
# =====================================================================

def save_episode(chat_id: str, summary: str, range_start: str, range_end: str,
                 keywords: List[str] = None, topics: List[str] = None) -> str:
    """Save a chat episode (segment summary) to DB."""
    episode_id = str(uuid.uuid4())
    keywords_json = json.dumps(keywords or [])
    topics_json = json.dumps(topics or [])
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO chat_episodes (episode_id, chat_id, summary, message_range_start, message_range_end, keywords, topics) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (episode_id, chat_id, summary, range_start, range_end, keywords_json, topics_json)
        )
        conn.commit()
        return episode_id
    finally:
        conn.close()

def get_episodes_by_chat(chat_id: str, limit: int = 10) -> List[Dict]:
    """Get the most recent episodes for a chat."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT * FROM chat_episodes WHERE chat_id=? ORDER BY created_at DESC LIMIT ?",
            (chat_id, limit)
        )
        rows = cur.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            # Parse JSON fields back
            if isinstance(d.get("keywords"), str):
                d["keywords"] = json.loads(d["keywords"])
            if isinstance(d.get("topics"), str):
                d["topics"] = json.loads(d["topics"])
            result.append(d)
        return result
    finally:
        conn.close()

# =====================================================================
# Night pipeline: Facts
# =====================================================================

def save_fact(chat_id: str, fact_text: str, confidence: float = 0.5,
              source_message_ids: List[str] = None) -> str:
    """Save an extracted fact to DB."""
    fact_id = str(uuid.uuid4())
    source_ids_json = json.dumps(source_message_ids or [])
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO chat_facts (fact_id, chat_id, fact_text, confidence, source_message_ids) VALUES (?, ?, ?, ?, ?)",
            (fact_id, chat_id, fact_text, confidence, source_ids_json)
        )
        conn.commit()
        return fact_id
    finally:
        conn.close()

def get_facts_by_chat(chat_id: str, limit: int = 50) -> List[Dict]:
    """Get the most recent facts for a chat."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT * FROM chat_facts WHERE chat_id=? ORDER BY created_at DESC LIMIT ?",
            (chat_id, limit)
        )
        rows = cur.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get("source_message_ids"), str):
                d["source_message_ids"] = json.loads(d["source_message_ids"])
            result.append(d)
        return result
    finally:
        conn.close()

def deduplicate_facts(chat_id: str, new_fact_text: str, threshold: float = 0.85) -> bool:
    """
    Simple check if a semantically similar fact already exists.
    Uses string overlap (Jaccard) as a cheap baseline.
    Returns True if duplicate found (should NOT insert).
    """
    existing = get_facts_by_chat(chat_id, limit=100)
    new_words = set(new_fact_text.lower().split())
    if not new_words:
        return False
    
    for fact in existing:
        old_words = set(fact["fact_text"].lower().split())
        if not old_words:
            continue
        intersection = new_words & old_words
        union = new_words | old_words
        jaccard = len(intersection) / len(union)
        if jaccard >= threshold:
            return True
    return False

# =====================================================================
# Night pipeline: Processing tracker
# =====================================================================

def get_last_processed_chunk_id(chat_id: str) -> Optional[str]:
    """Get the last processed chunk ID for a chat."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT last_processed_chunk_id FROM offline_processing_tracker WHERE chat_id=?",
            (chat_id,)
        )
        row = cur.fetchone()
        return row["last_processed_chunk_id"] if row else None
    finally:
        conn.close()

def update_processing_tracker(chat_id: str, last_chunk_id: str):
    """Update the processing tracker for a chat."""
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO offline_processing_tracker (chat_id, last_processed_chunk_id, processed_at) VALUES (?, ?, datetime('now'))",
            (chat_id, last_chunk_id)
        )
        conn.commit()
    finally:
        conn.close()

def get_all_chat_ids_from_chunks() -> List[str]:
    """Get all unique chat IDs that have chunks."""
    conn = get_conn()
    try:
        cur = conn.execute("SELECT DISTINCT chat_id FROM chunks ORDER BY chat_id")
        rows = cur.fetchall()
        return [r["chat_id"] for r in rows]
    finally:
        conn.close()

def get_unprocessed_chunks(chat_id: str, after_chunk_id: Optional[str] = None,
                           limit: int = 100) -> List[Dict]:
    """
    Get chunks that haven't been processed by the night pipeline yet.
    
    Args:
        chat_id: Chat to get chunks for
        after_chunk_id: Only get chunks with time_start > this chunk's time_start
        limit: Max chunks to return
    
    Returns:
        List of chunk dicts in chronological order
    """
    conn = get_conn()
    try:
        if after_chunk_id:
            # Get the timestamp of the last processed chunk
            cur = conn.execute(
                "SELECT time_start FROM chunks WHERE chunk_id=?",
                (after_chunk_id,)
            )
            row = cur.fetchone()
            if row:
                cur = conn.execute(
                    "SELECT chunk_id, user_id, content, type, time_start FROM chunks WHERE chat_id=? AND time_start > ? ORDER BY time_start ASC LIMIT ?",
                    (chat_id, row["time_start"], limit)
                )
            else:
                cur = conn.execute(
                    "SELECT chunk_id, user_id, content, type, time_start FROM chunks WHERE chat_id=? ORDER BY time_start ASC LIMIT ?",
                    (chat_id, limit)
                )
        else:
            cur = conn.execute(
                "SELECT chunk_id, user_id, content, type, time_start FROM chunks WHERE chat_id=? ORDER BY time_start ASC LIMIT ?",
                (chat_id, limit)
            )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()