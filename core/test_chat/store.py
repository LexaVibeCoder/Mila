"""Isolated storage for Observatory test dialogs.

A test chat runs the real persona/prompt/LLM loop, but its whole history lives
in ``test_chat_sessions`` / ``test_chat_messages`` / ``test_chat_diary`` (same
SQLite file as the context buffer, different table names). Nothing is written
to:

* ``chunks`` or ``chat_<id>`` — a test turn never enters a real chat's context;
* ``memories``, ``diary_entries``, ``presence_state``, ``action_journal``.

Deleting a session removes every row it owns, so Mila has nothing left to
remember. The synthetic user id is derived from the session's ``user_name``:
two sessions with the same name deliberately share the same on-disk memories
(which can be added by hand from the panel).
"""

import json
import re
import sqlite3
import uuid
from datetime import datetime
from typing import Optional

from core.context.buffer import _get_conn, now_local

DEFAULT_USER_NAME = "Собеседник"
DEFAULT_STATE = "dialog"
VALID_STATES = ("dialog", "rest", "working")
LEGACY_USER_ID = "test-user"

# Synthetic numeric chat id: TurnContext expects a Telegram-like string, but
# this id never reaches TDLib or the real DB.
_CHAT_ID_BASE = 9 * 10**14

_SLUG_RE = re.compile(r"[^\w\-]+", re.UNICODE)


def _now() -> str:
    # Same local timestamp format as the real context buffer.
    return now_local().strftime("%Y-%m-%d %H:%M:%S")


def normalize_user_name(name: Optional[str]) -> str:
    """Readable display name, falling back to the neutral default."""
    value = (name or "").strip()[:80]
    return value or DEFAULT_USER_NAME


def name_slug(name: Optional[str]) -> str:
    """Lowercase alias derived from the user name."""
    value = normalize_user_name(name).casefold().strip()
    value = re.sub(r"\s+", "-", value)
    value = _SLUG_RE.sub("", value).strip("-")
    return value or "user"


def user_id_for_name(name: Optional[str]) -> str:
    """Stable memory identity derived from the display name."""
    return f"{LEGACY_USER_ID}:{name_slug(name)}"


def alias_for_name(name: Optional[str]) -> str:
    """Chat alias shown to Mila; also derived from the name."""
    return name_slug(name)


def normalize_state(state: Optional[str]) -> str:
    return state if state in VALID_STATES else DEFAULT_STATE


def _connect():
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS test_chat_sessions (
            session_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            chat_id TEXT NOT NULL UNIQUE,
            user_id TEXT NOT NULL,
            user_name TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'dialog',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    # Migrations for sessions created by the first version of the feature.
    for sql in (
        "ALTER TABLE test_chat_sessions ADD COLUMN user_name TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE test_chat_sessions ADD COLUMN state TEXT NOT NULL DEFAULT 'dialog'",
    ):
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS test_chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'text',
            content TEXT NOT NULL DEFAULT '',
            debug_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES test_chat_sessions(session_id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_test_chat_messages_session "
        "ON test_chat_messages(session_id, id)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS test_chat_diary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            entry_number INTEGER NOT NULL,
            content_md TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'day_entries',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES test_chat_sessions(session_id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_test_chat_diary_session "
        "ON test_chat_diary(session_id, entry_number)"
    )
    # Backfill names/identities for rows created before user_name existed.
    rows = conn.execute(
        "SELECT session_id, user_id, user_name FROM test_chat_sessions"
    ).fetchall()
    for row in rows:
        name = normalize_user_name(row["user_name"])
        expected_id = user_id_for_name(name)
        if not row["user_name"] or row["user_id"] in (None, "", LEGACY_USER_ID):
            conn.execute(
                "UPDATE test_chat_sessions SET user_name = ?, user_id = ? "
                "WHERE session_id = ?",
                (name, expected_id, row["session_id"]),
            )
    conn.commit()
    return conn


def _row_to_message(row: sqlite3.Row) -> dict:
    debug = None
    raw = row["debug_json"]
    if raw:
        try:
            debug = json.loads(raw)
        except (TypeError, ValueError):
            debug = None
    return {
        "id": int(row["id"]),
        "role": row["role"],
        "kind": row["kind"],
        "content": row["content"] or "",
        "created_at": row["created_at"],
        "debug": debug,
    }


def _row_to_session(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["user_name"] = normalize_user_name(data.get("user_name"))
    data["user_id"] = data.get("user_id") or user_id_for_name(data["user_name"])
    data["alias"] = alias_for_name(data["user_name"])
    data["state"] = normalize_state(data.get("state"))
    data["message_count"] = int(data.get("message_count") or 0)
    data["preview"] = data.get("preview") or ""
    return data


def _row_to_diary(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "entry_number": int(row["entry_number"]),
        "content": row["content_md"] or "",
        "category": row["category"] or "day_entries",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _new_chat_id() -> str:
    return str(_CHAT_ID_BASE + uuid.uuid4().int % 10**12)


def create_session(title: Optional[str] = None,
                   user_name: Optional[str] = None,
                   state: Optional[str] = None) -> dict:
    """Create an empty test dialog and return its public row."""
    name = normalize_user_name(user_name)
    title = (title or "").strip()[:120] or f"Диалог с {name}"
    session_id = uuid.uuid4().hex
    ts = _now()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO test_chat_sessions "
            "(session_id, title, chat_id, user_id, user_name, state, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, title, _new_chat_id(), user_id_for_name(name), name,
             normalize_state(state), ts, ts),
        )
        conn.commit()
    finally:
        conn.close()
    return get_session(session_id)


def _session_select() -> str:
    return """
        SELECT s.*,
               (SELECT COUNT(*) FROM test_chat_messages m
                 WHERE m.session_id = s.session_id) AS message_count,
               (SELECT m.content FROM test_chat_messages m
                 WHERE m.session_id = s.session_id
                   AND m.kind IN ('text', 'bubble')
                 ORDER BY m.id DESC LIMIT 1) AS preview
          FROM test_chat_sessions s
    """


def list_sessions() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            _session_select() +
            " ORDER BY s.updated_at DESC, s.rowid DESC"
        ).fetchall()
        return [_row_to_session(r) for r in rows]
    finally:
        conn.close()


def get_session(session_id: str) -> Optional[dict]:
    conn = _connect()
    try:
        row = conn.execute(
            _session_select() + " WHERE s.session_id = ?",
            (str(session_id),),
        ).fetchone()
        return _row_to_session(row) if row else None
    finally:
        conn.close()


def update_session(session_id: str, *, title: Optional[str] = None,
                   user_name: Optional[str] = None,
                   state: Optional[str] = None) -> Optional[dict]:
    """Update scenario title, interlocutor name or Mila's session state."""
    session = get_session(session_id)
    if not session:
        return None
    updates = []
    params = []
    if title is not None:
        updates.append("title = ?")
        params.append((title or "").strip()[:120] or session["title"])
    if user_name is not None:
        name = normalize_user_name(user_name)
        updates.append("user_name = ?")
        updates.append("user_id = ?")
        params.extend([name, user_id_for_name(name)])
    if state is not None:
        updates.append("state = ?")
        params.append(normalize_state(state))
    if not updates:
        return session
    updates.append("updated_at = ?")
    params.append(_now())
    params.append(str(session_id))
    conn = _connect()
    try:
        conn.execute(
            f"UPDATE test_chat_sessions SET {', '.join(updates)} "
            "WHERE session_id = ?",
            params,
        )
        conn.commit()
    finally:
        conn.close()
    return get_session(session_id)


def set_state(session_id: str, state: str) -> bool:
    """Change Mila's state inside one test session (never global presence)."""
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE test_chat_sessions SET state = ?, updated_at = ? "
            "WHERE session_id = ?",
            (normalize_state(state), _now(), str(session_id)),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_session(session_id: str) -> bool:
    """Delete the dialog, its messages, debug rows and test diary."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM test_chat_messages WHERE session_id = ?",
                     (str(session_id),))
        conn.execute("DELETE FROM test_chat_diary WHERE session_id = ?",
                     (str(session_id),))
        cur = conn.execute("DELETE FROM test_chat_sessions WHERE session_id = ?",
                           (str(session_id),))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def clear_messages(session_id: str) -> int:
    conn = _connect()
    try:
        if not conn.execute(
            "SELECT 1 FROM test_chat_sessions WHERE session_id = ?",
            (str(session_id),),
        ).fetchone():
            return 0
        cur = conn.execute("DELETE FROM test_chat_messages WHERE session_id = ?",
                           (str(session_id),))
        conn.execute(
            "UPDATE test_chat_sessions SET updated_at = ? WHERE session_id = ?",
            (_now(), str(session_id)),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def append_message(session_id: str, role: str, content: str,
                   kind: str = "text", debug: Optional[dict] = None) -> dict:
    """Store one turn row and bump the session's ``updated_at``."""
    ts = _now()
    debug_json = (json.dumps(debug, ensure_ascii=False, default=str)
                  if debug is not None else None)
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO test_chat_messages "
            "(session_id, role, kind, content, debug_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(session_id), role, kind, content or "", debug_json, ts),
        )
        conn.execute(
            "UPDATE test_chat_sessions SET updated_at = ? WHERE session_id = ?",
            (ts, str(session_id)),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM test_chat_messages WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return _row_to_message(row)
    finally:
        conn.close()


def get_messages(session_id: str, limit: int = 1000) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM test_chat_messages WHERE session_id = ? "
            "ORDER BY id ASC LIMIT ?",
            (str(session_id), int(limit)),
        ).fetchall()
        return [_row_to_message(r) for r in rows]
    finally:
        conn.close()


def build_history(session_id: str) -> list[dict]:
    """OpenAI-style history for the next turn.

    Silent/error rows are UI-only: they are not part of what Mila said, so they
    never enter the model context. Timestamps mirror ``bot._build_histories``.
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT role, content, created_at FROM test_chat_messages "
            "WHERE session_id = ? AND kind IN ('text', 'bubble') "
            "ORDER BY id ASC",
            (str(session_id),),
        ).fetchall()
        return [
            {"role": r["role"],
             "content": f"[{r['created_at']}] {r['content']}"}
            for r in rows if (r["content"] or "").strip()
        ]
    finally:
        conn.close()


# ── test diary ────────────────────────────────────────────────────

def add_diary_entry(session_id: str, content_md: str,
                    category: str = "day_entries") -> Optional[dict]:
    """Write one entry into the session's own diary (never the real one)."""
    content_md = (content_md or "").strip()
    if not content_md:
        return None
    if category not in ("day_entries", "reflection_entries"):
        category = "day_entries"
    ts = _now()
    conn = _connect()
    try:
        if not conn.execute(
            "SELECT 1 FROM test_chat_sessions WHERE session_id = ?",
            (str(session_id),),
        ).fetchone():
            return None
        row = conn.execute(
            "SELECT COALESCE(MAX(entry_number), 0) + 1 AS next_num "
            "FROM test_chat_diary WHERE session_id = ?",
            (str(session_id),),
        ).fetchone()
        next_num = int(row["next_num"]) if row else 1
        cur = conn.execute(
            "INSERT INTO test_chat_diary "
            "(session_id, entry_number, content_md, category, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(session_id), next_num, content_md, category, ts, ts),
        )
        conn.execute(
            "UPDATE test_chat_sessions SET updated_at = ? WHERE session_id = ?",
            (ts, str(session_id)),
        )
        conn.commit()
        saved = conn.execute(
            "SELECT * FROM test_chat_diary WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return _row_to_diary(saved)
    finally:
        conn.close()


def update_diary_entry(session_id: str, entry_id: int, content_md: str,
                       category: Optional[str] = None) -> bool:
    content_md = (content_md or "").strip()
    if not content_md:
        return False
    conn = _connect()
    try:
        if category is not None:
            if category not in ("day_entries", "reflection_entries"):
                category = "day_entries"
            cur = conn.execute(
                "UPDATE test_chat_diary SET content_md = ?, category = ?, "
                "updated_at = ? WHERE id = ? AND session_id = ?",
                (content_md, category, _now(), int(entry_id), str(session_id)),
            )
        else:
            cur = conn.execute(
                "UPDATE test_chat_diary SET content_md = ?, updated_at = ? "
                "WHERE id = ? AND session_id = ?",
                (content_md, _now(), int(entry_id), str(session_id)),
            )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_diary_entry(session_id: str, entry_id: int) -> bool:
    conn = _connect()
    try:
        cur = conn.execute(
            "DELETE FROM test_chat_diary WHERE id = ? AND session_id = ?",
            (int(entry_id), str(session_id)),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_diary_entries(session_id: str) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM test_chat_diary WHERE session_id = ? "
            "ORDER BY entry_number ASC",
            (str(session_id),),
        ).fetchall()
        return [_row_to_diary(r) for r in rows]
    finally:
        conn.close()


def get_diary_context(session_id: str, max_entries: int = 30) -> str:
    """Session diary formatted like the real prompt block (with entry IDs)."""
    entries = get_diary_entries(session_id)
    if not entries:
        return ""
    entries = entries[-max_entries:]
    labels = {
        "day_entries": "Day Events",
        "reflection_entries": "Thoughts and Feelings",
    }
    blocks = []
    for category in ("day_entries", "reflection_entries"):
        cat_entries = [e for e in entries if e["category"] == category]
        if not cat_entries:
            continue
        lines = [f"## {labels[category]} ({category}):"]
        for entry in cat_entries:
            lines.append(f"[ID: {entry['id']}] {entry['content']}")
        blocks.append("\n".join(lines))
    if not blocks:
        return ""
    return (
        "\n[Your personal diary for today. Each entry has an ID — "
        "use update_diary to update an existing entry:]\n---\n"
        + "\n".join(blocks)
        + "\n---\n"
    )
