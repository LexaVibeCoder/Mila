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
    
    # timeout=30 + busy_timeout: bot and Observatory write from two
    # processes — wait instead of failing with "database is locked".
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
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
    # Lightweight migrations for multimodality (safe on existing DBs).
    _migrate_multimedia()
    _migrate_indexes()
    print("Database initialized.")


def _migrate_multimedia():
    conn = get_conn()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(chunks)").fetchall()}
        if "kind" not in cols:
            conn.execute("ALTER TABLE chunks ADD COLUMN kind TEXT DEFAULT 'text'")
        if "media_path" not in cols:
            conn.execute("ALTER TABLE chunks ADD COLUMN media_path TEXT")
        media_cols = {r["name"] for r in conn.execute("PRAGMA table_info(media_logs)").fetchall()}
        if "detached_at" not in media_cols:
            conn.execute("ALTER TABLE media_logs ADD COLUMN detached_at TEXT")
        conn.commit()
    except Exception as e:
        print(f"[DB migrate] multimedia columns: {e}")
    finally:
        conn.close()


def _migrate_indexes():
    """Indexes for faiss_id lookups and unique id allocation.

    ``UNIQUE`` is preferred (M6: no two memories may share a FAISS id). On an
    old DB with duplicate non-NULL ids the unique index cannot be created;
    fall back to a plain index so lookups still work — the next rebuild
    repairs the duplicates.
    """
    conn = get_conn()
    try:
        for table in ("memories", "chunks"):
            name = f"idx_{table}_faiss"
            try:
                conn.execute(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table}(faiss_id)")
            except sqlite3.IntegrityError as e:
                print(f"[DB migrate] {name}: duplicate faiss_id ({e}); using non-unique index")
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS {name} ON {table}(faiss_id)")
        conn.commit()
    except Exception as e:
        print(f"[DB migrate] faiss indexes: {e}")
    finally:
        conn.close()

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

def add_chunk(chat_id: str, user_id: str, content: str, token_count: int = 0, type: str = 'raw', llm_full_response: str = None, kind: str = "text", media_path: str = None):
    chunk_id = str(uuid.uuid4())
    time_now = datetime.now(timezone.utc).isoformat()
    # Silent turns are stored (history/diary need them) but must not look
    # like a normal empty text message: mark them explicitly so indexing and
    # search can skip them (M10).
    kind = (kind or "text").strip().lower() or "text"
    content = content or ""
    if kind == "text" and not content.strip():
        kind = "silence"
    conn = get_conn()
    try:
        # Backward compatible: older DBs may lack kind/media_path until migrate runs.
        try:
            conn.execute(
                "INSERT INTO chunks (chunk_id, chat_id, user_id, content, llm_full_response, type, token_count, time_start, kind, media_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (chunk_id, chat_id, user_id, content, llm_full_response, type, token_count, time_now, kind, media_path)
            )
        except sqlite3.OperationalError:
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


def get_user_display_name(user_id: str) -> str:
    """Display name for prompts: preferred_name > user_name > short id."""
    info = get_user_info(user_id)
    if not info:
        return f"user {str(user_id)[:8]}"
    return info.get("preferred_name") or info.get("user_name") or f"user {str(user_id)[:8]}"


def log_media(chat_id: str, user_id: str, file_path: str, description: str = ""):
    """Record a media file in media_logs (best-effort)."""
    import uuid as _uuid
    media_id = str(_uuid.uuid4())
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO media_logs (media_id, chat_id, user_id, file_path, vlm_description) VALUES (?, ?, ?, ?, ?)",
            (media_id, chat_id, user_id, file_path, description),
        )
        conn.commit()
        return media_id
    except Exception:
        return None
    finally:
        conn.close()


def get_recent_media(chat_id: str, kinds: list[str] | tuple[str, ...] = ("photo", "voice"),
                     limit: int = 5) -> list[dict]:
    """Return recent chunks with media files for a chat, newest last.

    Used to re-attach recent photos/audios to history so the model still
    sees them a few turns later (not only when they are the last message).
    Skips rows whose file no longer exists on disk.
    """
    if not kinds:
        return []
    conn = get_conn()
    try:
        try:
            placeholders = ",".join("?" for _ in kinds)
            cur = conn.cursor()
            cur.execute(
                f"SELECT chunk_id, user_id, content, kind, media_path, time_start "
                f"FROM chunks WHERE chat_id=? AND kind IN ({placeholders}) "
                f"AND media_path IS NOT NULL AND media_path != '' "
                f"ORDER BY time_start DESC LIMIT ?",
                (chat_id, *kinds, limit * 3),
            )
            rows = [dict(r) for r in cur.fetchall()]
        except sqlite3.OperationalError:
            return []
        import os as _os
        out = []
        for r in rows:
            p = r.get("media_path") or ""
            if p and _os.path.isfile(p):
                out.append(r)
            if len(out) >= limit:
                break
        out.reverse()  # chronological
        return out
    finally:
        conn.close()


def detach_media_by_chunk_ids(chunk_ids: list[str]) -> list[str]:
    """Clear media_path for given chunks, return detached file paths.

    Used by sleep summarization: bytes of summarized-away photos/audios
    are dropped so get_recent_media no longer re-attaches them to later
    turns. Text placeholders and summaries stay untouched; the caller
    deletes the returned files from disk (including converted twins).
    """
    ids = [c for c in (chunk_ids or []) if c]
    if not ids:
        return []
    conn = get_conn()
    try:
        try:
            placeholders = ",".join("?" for _ in ids)
            cur = conn.cursor()
            cur.execute(
                f"SELECT chunk_id, media_path FROM chunks "
                f"WHERE chunk_id IN ({placeholders}) "
                f"AND media_path IS NOT NULL AND media_path != ''",
                ids,
            )
            rows = [dict(r) for r in cur.fetchall()]
            if not rows:
                return []
            paths = [r.get("media_path") for r in rows if r.get("media_path")]
            found_ids = [r["chunk_id"] for r in rows]
            ph2 = ",".join("?" for _ in found_ids)
            conn.execute(
                f"UPDATE chunks SET media_path=NULL WHERE chunk_id IN ({ph2})",
                found_ids,
            )
            # media_logs pointed at the same files; clear those references too
            # so the panel/logs don't keep dead paths (M12).
            try:
                now = datetime.now(timezone.utc).isoformat()
                ph3 = ",".join("?" for _ in paths)
                conn.execute(
                    f"UPDATE media_logs SET file_path=NULL, detached_at=? "
                    f"WHERE file_path IN ({ph3})",
                    (now, *paths),
                )
            except sqlite3.OperationalError:
                # Older DB without detached_at: still clear the path.
                try:
                    ph3 = ",".join("?" for _ in paths)
                    conn.execute(
                        f"UPDATE media_logs SET file_path=NULL "
                        f"WHERE file_path IN ({ph3})", paths)
                except sqlite3.OperationalError:
                    pass
            conn.commit()
            return paths
        except sqlite3.OperationalError:
            return []
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
    """Insert a memory row.

    Low-level storage only: callers that need the row to be searchable must
    use ``core.memory.indexer.MemoryIndexer.add_memory()`` (insert + embed +
    FAISS + update ``faiss_id``) or call ``index_memory(mem_id)`` afterwards.
    A NULL ``faiss_id`` is the reindex queue.
    """
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


def get_memory(mem_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM memories WHERE mem_id=?", (mem_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_chunk(chunk_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM chunks WHERE chunk_id=?", (chunk_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_memory_faiss_id(mem_id: str, faiss_id: int | None) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE memories SET faiss_id=? WHERE mem_id=?", (faiss_id, mem_id))
        conn.commit()
    finally:
        conn.close()


def update_chunk_faiss_id(chunk_id: str, faiss_id: int | None) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE chunks SET faiss_id=? WHERE chunk_id=?", (faiss_id, chunk_id))
        conn.commit()
    finally:
        conn.close()


def set_memory_faiss_ids(mapping: dict[str, int | None]) -> None:
    if not mapping:
        return
    conn = get_conn()
    try:
        conn.executemany(
            "UPDATE memories SET faiss_id=? WHERE mem_id=?",
            [(fid, mem_id) for mem_id, fid in mapping.items()],
        )
        conn.commit()
    finally:
        conn.close()


def _max_faiss_id(table: str) -> int:
    conn = get_conn()
    try:
        row = conn.execute(
            f"SELECT COALESCE(MAX(faiss_id), 0) AS max_id FROM {table}").fetchone()
        return int(row["max_id"] or 0)
    finally:
        conn.close()


def max_memory_faiss_id() -> int:
    return _max_faiss_id("memories")


def max_chunk_faiss_id() -> int:
    return _max_faiss_id("chunks")


def get_indexable_memories() -> list[dict]:
    """All non-empty memories; used by full FAISS rebuild."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT mem_id, content, faiss_id FROM memories "
            "WHERE content IS NOT NULL AND TRIM(content) != ''"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_pending_memories(limit: int = 500) -> list[dict]:
    """Memories without a FAISS id — the reindex queue."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT mem_id, content FROM memories "
            "WHERE faiss_id IS NULL AND content IS NOT NULL AND TRIM(content) != '' "
            "ORDER BY time ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_pending_chunks(limit: int = 500) -> list[dict]:
    """Text chunks without a FAISS id — the chunk reindex queue."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT chunk_id, content FROM chunks "
            "WHERE faiss_id IS NULL AND content IS NOT NULL AND TRIM(content) != '' "
            "AND COALESCE(kind, 'text') != 'silence' "
            "ORDER BY time_start ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_indexable_memories() -> int:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM memories "
            "WHERE content IS NOT NULL AND TRIM(content) != ''").fetchone()
        return int(row["cnt"] or 0)
    finally:
        conn.close()


def count_pending_memories() -> int:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM memories "
            "WHERE faiss_id IS NULL AND content IS NOT NULL AND TRIM(content) != ''"
        ).fetchone()
        return int(row["cnt"] or 0)
    finally:
        conn.close()


def get_all_memory_faiss_ids() -> set[int]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT faiss_id FROM memories WHERE faiss_id IS NOT NULL").fetchall()
        return {int(r["faiss_id"]) for r in rows}
    finally:
        conn.close()


def get_all_chunk_faiss_ids() -> set[int]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT faiss_id FROM chunks WHERE faiss_id IS NOT NULL").fetchall()
        return {int(r["faiss_id"]) for r in rows}
    finally:
        conn.close()


def get_indexable_chunks() -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT chunk_id, content, faiss_id FROM chunks "
            "WHERE content IS NOT NULL AND TRIM(content) != '' "
            "AND COALESCE(kind, 'text') != 'silence'"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_chunk_faiss_ids(mapping: dict[str, int | None]) -> None:
    if not mapping:
        return
    conn = get_conn()
    try:
        conn.executemany(
            "UPDATE chunks SET faiss_id=? WHERE chunk_id=?",
            [(fid, chunk_id) for chunk_id, fid in mapping.items()],
        )
        conn.commit()
    finally:
        conn.close()


def count_indexable_chunks() -> int:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM chunks "
            "WHERE content IS NOT NULL AND TRIM(content) != '' "
            "AND COALESCE(kind, 'text') != 'silence'").fetchone()
        return int(row["cnt"] or 0)
    finally:
        conn.close()


def clear_chunk_faiss_ids(chunk_ids: list[str]) -> None:
    if not chunk_ids:
        return
    conn = get_conn()
    try:
        conn.executemany(
            "UPDATE chunks SET faiss_id=NULL WHERE chunk_id=?",
            [(c,) for c in chunk_ids],
        )
        conn.commit()
    finally:
        conn.close()


def clear_memory_faiss_ids(mem_ids: list[str]) -> None:
    if not mem_ids:
        return
    conn = get_conn()
    try:
        conn.executemany(
            "UPDATE memories SET faiss_id=NULL WHERE mem_id=?",
            [(m,) for m in mem_ids],
        )
        conn.commit()
    finally:
        conn.close()


def get_chunks_by_faiss_ids(faiss_ids: list[int]) -> list[dict]:
    if not faiss_ids:
        return []
    conn = get_conn()
    try:
        placeholders = ",".join("?" for _ in faiss_ids)
        rows = conn.execute(
            f"SELECT chunk_id, faiss_id FROM chunks WHERE faiss_id IN ({placeholders})",
            faiss_ids,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_memories_by_faiss_ids(faiss_ids: list[int]) -> list[dict]:
    if not faiss_ids:
        return []
    conn = get_conn()
    try:
        placeholders = ",".join("?" for _ in faiss_ids)
        rows = conn.execute(
            f"SELECT mem_id, faiss_id FROM memories WHERE faiss_id IN ({placeholders})",
            faiss_ids,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def search_memories_like(user_id: str | None, query: str,
                        limit: int = 5) -> list[dict]:
    """Keyword fallback when embeddings/FAISS are unavailable (M9)."""
    query = (query or "").strip()
    if not query:
        return []
    import re
    words = [w for w in re.split(r"[^\w]+", query, flags=re.UNICODE) if len(w) >= 3]
    if not words:
        words = [query]
    clauses = " OR ".join("content LIKE ?" for _ in words)
    if user_id is not None:
        scope_sql = "user_id=?"
        params: list = [str(user_id)]
    else:
        scope_sql = "user_id IS NULL"
        params = []
    params.extend(f"%{w}%" for w in words)
    conn = get_conn()
    try:
        rows = conn.execute(
            f"SELECT * FROM memories WHERE ({scope_sql}) AND ({clauses}) "
            f"ORDER BY importance DESC, time DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def search_memories_by_ids(mem_ids: list[str]) -> list[dict]:
    if not mem_ids:
        return []
    conn = get_conn()
    try:
        placeholders = ','.join('?' for _ in mem_ids)
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM memories WHERE mem_id IN ({placeholders})", mem_ids)
        rows = cur.fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()

def search_memories_by_faiss_ids(faiss_ids: list[int], user_id: str | None = None) -> list[dict]:
    """Search memories by FAISS integer IDs, optionally scoped to one user.

    user_id filter is required for multi-user isolation: without it a vector
    hit for user A can surface user B's fact in the prompt.
    """
    if not faiss_ids:
        return []
    conn = get_conn()
    try:
        placeholders = ','.join('?' for _ in faiss_ids)
        cur = conn.cursor()
        if user_id is not None:
            cur.execute(
                f"SELECT * FROM memories WHERE faiss_id IN ({placeholders}) AND user_id=?",
                (*faiss_ids, str(user_id)),
            )
        else:
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


def get_chats_by_last_activity(limit: int = 20) -> list[dict]:
    """Chats sorted by last activity, oldest first (for proactive).

    Returns [{chat_id, chat_name, last_at, count}] ordered ASC by last_at,
    i.e. stale chats in the middle of context, fresh ones at the bottom.
    No message truncation happens here — only chat-level limiting.
    """
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT chat_id, MAX(time_start) AS last_at, COUNT(*) AS cnt "
            "FROM chunks GROUP BY chat_id ORDER BY last_at ASC"
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    # Keep only the freshest `limit` chats when there are too many,
    # preserving old->new order.
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    out = []
    for r in rows:
        chat_id = str(r.get("chat_id") or "")
        if not chat_id:
            continue
        name = ""
        try:
            conn2 = get_conn()
            try:
                cur2 = conn2.execute(
                    "SELECT chat_name FROM chats WHERE chat_id=?", (chat_id,))
                row2 = cur2.fetchone()
                if row2 and row2["chat_name"]:
                    name = row2["chat_name"]
            finally:
                conn2.close()
        except Exception:
            name = ""
        if not name:
            try:
                name = get_user_display_name(chat_id)
            except Exception:
                name = chat_id[:8]
        out.append({
            "chat_id": chat_id,
            "chat_name": name or chat_id[:8],
            "last_at": r.get("last_at") or "",
            "count": int(r.get("cnt") or 0),
        })
    return out


def get_global_last_activity() -> str | None:
    """Newest chunks.time_start across all chats (any side)."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT MAX(time_start) AS last_at FROM chunks")
        row = cur.fetchone()
        return row["last_at"] if row else None
    finally:
        conn.close()

