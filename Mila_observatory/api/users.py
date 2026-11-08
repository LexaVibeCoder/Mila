"""
Users API — list users Mila has talked to, manage relationship
(sentiment / relationship_type / names) and inspect per-user
memory and dialog history.
"""

import sys
import os
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import APIRouter, HTTPException, Query
from Mila_observatory.models import (
    AttitudeHistoryResponse,
    AttitudePoint,
    MemoryItem,
    MemoryListResponse,
    PortraitResponse,
    UserChatInfo,
    UserDetailResponse,
    UserItem,
    UserListResponse,
    UserMemoryCreateRequest,
    UserMessageItem,
    UserMessageListResponse,
    UserUpdateRequest,
)
from Mila_observatory.api.dashboard import add_event
from core.db import (
    get_conn as get_mila_conn,
    ensure_user_exists,
    get_bot_user_id,
)
from core.memory.indexer import get_indexer

router = APIRouter(tags=["users"])


def _ensure_history_table(conn) -> None:
    """Create sentiment-history table on first use (safe on existing DBs)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_attitude_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            sentiment REAL DEFAULT 0.0,
            relationship_type TEXT DEFAULT 'friend',
            changed_by TEXT DEFAULT 'manual',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


def _record_attitude(conn, user_id: str, sentiment: float, relationship_type: str,
                     changed_by: str = "manual") -> None:
    _ensure_history_table(conn)
    from datetime import datetime, timezone
    conn.execute(
        "INSERT INTO user_attitude_history (user_id, sentiment, relationship_type, changed_by, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, sentiment, relationship_type, changed_by,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()

RELATIONSHIP_CHOICES = {
    "friend", "close_friend", "family", "partner",
    "acquaintance", "colleague", "stranger", "blocked",
}


def closeness_of(sentiment: float) -> tuple[str, str]:
    """Mirror core/llm/persona.py thresholds: key + human label."""
    try:
        s = float(sentiment)
    except (TypeError, ValueError):
        s = 0.0
    if s > 0.6:
        return "friend", "Друг"
    if s > 0.2:
        return "warm", "Тёплый знакомый"
    if s < -0.6:
        return "cold_stranger", "Холодный чужой"
    if s < -0.2:
        return "cold", "Холодный знакомый"
    return "neutral", "Нейтрально"


def _display_name(row: dict) -> str:
    return row.get("preferred_name") or row.get("user_name") or f"user {str(row.get('user_id', ''))[:8]}"


def _is_empty_profile(profile: dict, stats: dict) -> bool:
    """True for rows with no data and no manually filled profile fields.

    ``ensure_user_exists`` used to leave such rows behind when a staged
    memory was rolled back. They are not users Mila has communicated with.
    """
    return (
        stats.get("messages_count", 0) == 0
        and stats.get("memories_count", 0) == 0
        and stats.get("chats_count", 0) == 0
        and not profile.get("preferred_name")
        and not profile.get("user_name")
        and not profile.get("appearance_description")
    )


def _user_item(row: dict, stats: dict) -> UserItem:
    sentiment = row.get("sentiment", 0.0)
    try:
        sentiment = float(sentiment) if sentiment is not None else 0.0
    except (TypeError, ValueError):
        sentiment = 0.0
    key, label = closeness_of(sentiment)
    return UserItem(
        user_id=str(row.get("user_id", "")),
        display_name=_display_name(row),
        user_name=row.get("user_name"),
        preferred_name=row.get("preferred_name"),
        relationship_type=row.get("relationship_type") or "friend",
        sentiment=sentiment,
        closeness=key,
        closeness_label=label,
        appearance_description=row.get("appearance_description"),
        memories_count=stats.get("memories_count", 0),
        messages_count=stats.get("messages_count", 0),
        chats_count=stats.get("chats_count", 0),
        last_activity=stats.get("last_activity"),
    )


def _all_user_ids(cur) -> set[str]:
    """All known user ids: users table + anyone seen in chunks/memories."""
    ids: set[str] = set()
    for table in ("users",):
        try:
            for r in cur.execute(f"SELECT user_id FROM {table}"):
                if r["user_id"]:
                    ids.add(str(r["user_id"]))
        except Exception:
            pass
    for table, col in (("chunks", "user_id"), ("memories", "user_id"), ("chat_members", "user_id")):
        try:
            for r in cur.execute(f"SELECT DISTINCT {col} FROM {table}"):
                if r[col]:
                    ids.add(str(r[col]))
        except Exception:
            pass
    return ids


def _stats_for(cur, user_id: str) -> dict:
    stats = {"memories_count": 0, "messages_count": 0, "chats_count": 0, "last_activity": None}
    try:
        r = cur.execute("SELECT COUNT(*) c FROM memories WHERE user_id = ?", (user_id,)).fetchone()
        stats["memories_count"] = r["c"] if r else 0
    except Exception:
        pass
    try:
        r = cur.execute("SELECT COUNT(*) c FROM chunks WHERE user_id = ?", (user_id,)).fetchone()
        stats["messages_count"] = r["c"] if r else 0
    except Exception:
        pass
    chat_ids: set[str] = set()
    try:
        for r in cur.execute("SELECT DISTINCT chat_id FROM chunks WHERE user_id = ?", (user_id,)):
            if r["chat_id"]:
                chat_ids.add(str(r["chat_id"]))
    except Exception:
        pass
    try:
        for r in cur.execute("SELECT DISTINCT chat_id FROM chat_members WHERE user_id = ?", (user_id,)):
            if r["chat_id"]:
                chat_ids.add(str(r["chat_id"]))
    except Exception:
        pass
    try:
        for r in cur.execute("SELECT DISTINCT source_chat_id FROM memories WHERE user_id = ?", (user_id,)):
            if r["source_chat_id"]:
                chat_ids.add(str(r["source_chat_id"]))
    except Exception:
        pass
    stats["chats_count"] = len(chat_ids)
    last = None
    try:
        r = cur.execute("SELECT MAX(time_start) m FROM chunks WHERE user_id = ?", (user_id,)).fetchone()
        last = r["m"] if r else None
    except Exception:
        pass
    try:
        r = cur.execute("SELECT MAX(time) m FROM memories WHERE user_id = ?", (user_id,)).fetchone()
        mem_last = r["m"] if r else None
        if mem_last and (not last or mem_last > last):
            last = mem_last
    except Exception:
        pass
    stats["last_activity"] = last
    return stats


def _get_profile(cur, user_id: str) -> Optional[dict]:
    try:
        r = cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(r) if r else None
    except Exception:
        return None


@router.get("/api/users", response_model=UserListResponse)
async def list_users(
    search: Optional[str] = Query(None, description="Filter by name or user_id"),
    sort: str = Query("last_activity", description="Sort: last_activity|messages|memories|name"),
):
    """List all users Mila has communicated with, with aggregated stats."""
    conn = get_mila_conn()
    try:
        cur = conn.cursor()
        ids = _all_user_ids(cur)
        own_id = get_bot_user_id()
        items: list[UserItem] = []
        for uid in ids:
            if own_id and str(uid) == str(own_id):
                continue
            profile = _get_profile(cur, uid)
            if profile is None:
                profile = {"user_id": uid, "user_name": None, "preferred_name": None,
                           "relationship_type": "friend", "sentiment": 0.0,
                           "appearance_description": None}
            stats = _stats_for(cur, uid)
            if _is_empty_profile(profile, stats):
                continue
            items.append(_user_item(profile, stats))
    finally:
        conn.close()

    if search:
        q = search.strip().lower()
        items = [u for u in items
                 if q in (u.display_name or "").lower() or q in u.user_id.lower()]

    if sort == "messages":
        items.sort(key=lambda u: u.messages_count, reverse=True)
    elif sort == "memories":
        items.sort(key=lambda u: u.memories_count, reverse=True)
    elif sort == "name":
        items.sort(key=lambda u: (u.display_name or "").lower())
    else:  # last_activity, newest first (None last)
        items.sort(key=lambda u: (u.last_activity is not None, u.last_activity or ""), reverse=True)

    return UserListResponse(items=items, total=len(items))


def _user_detail(user_id: str) -> UserDetailResponse:
    own_id = get_bot_user_id()
    if own_id and str(user_id) == str(own_id):
        raise HTTPException(status_code=404, detail="Mila's own account is not a user")
    conn = get_mila_conn()
    try:
        cur = conn.cursor()
        profile = _get_profile(cur, user_id)
        if profile is None:
            # Still show users known only from history
            ids = _all_user_ids(cur)
            if user_id not in ids:
                raise HTTPException(status_code=404, detail="User not found")
            profile = {"user_id": user_id, "user_name": None, "preferred_name": None,
                       "relationship_type": "friend", "sentiment": 0.0,
                       "appearance_description": None}
        stats = _stats_for(cur, user_id)
        item = _user_item(profile, stats)

        # Chats with names + counts
        chats: list[UserChatInfo] = []
        try:
            chat_names = {str(r["chat_id"]): (r["chat_name"] or "") for r in cur.execute("SELECT chat_id, chat_name FROM chats")}
        except Exception:
            chat_names = {}
        try:
            rows = cur.execute(
                "SELECT chat_id, COUNT(*) c, MAX(time_start) last_t FROM chunks "
                "WHERE user_id = ? GROUP BY chat_id ORDER BY last_t DESC",
                (user_id,)).fetchall()
            for r in rows:
                cid = str(r["chat_id"] or "")
                if not cid:
                    continue
                chats.append(UserChatInfo(
                    chat_id=cid,
                    chat_name=chat_names.get(cid, ""),
                    messages_count=r["c"] or 0,
                    last_time=r["last_t"],
                ))
        except Exception:
            pass

        # Recent memories
        recent_memories: list[MemoryItem] = []
        try:
            for r in cur.execute(
                "SELECT * FROM memories WHERE user_id = ? ORDER BY time DESC LIMIT 10",
                (user_id,)).fetchall():
                d = dict(r)
                recent_memories.append(MemoryItem(
                    mem_id=d.get("mem_id", ""),
                    user_id=str(d.get("user_id", "")),
                    content=d.get("content", ""),
                    type=d.get("type", "fact"),
                    tag=d.get("tag"),
                    importance=d.get("importance", 0.5),
                    confidence=0.5,
                    source_chat_id=d.get("source_chat_id"),
                    source_message_id=d.get("source_message_id"),
                    created_at=d.get("time"),
                    last_accessed_at=d.get("last_accessed_at"),
                ))
        except Exception:
            pass

        # Recent messages
        recent_messages = _recent_messages(cur, chat_names, user_id, limit=10)
    finally:
        conn.close()

    return UserDetailResponse(**item.model_dump(), chats=chats,
                              recent_memories=recent_memories,
                              recent_messages=recent_messages)


def _recent_messages(cur, chat_names: dict, user_id: str, limit: int, offset: int = 0) -> list[UserMessageItem]:
    out: list[UserMessageItem] = []
    try:
        cols = {r["name"] for r in cur.execute("PRAGMA table_info(chunks)").fetchall()}
        has_kind = "kind" in cols
        kind_sel = "kind" if has_kind else "'text' AS kind"
        rows = cur.execute(
            f"SELECT chunk_id, chat_id, content, {kind_sel}, time_start FROM chunks "
            f"WHERE user_id = ? ORDER BY time_start DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset)).fetchall()
        for r in rows:
            d = dict(r)
            cid = str(d.get("chat_id") or "")
            content = d.get("content") or ""
            out.append(UserMessageItem(
                chunk_id=str(d.get("chunk_id") or ""),
                chat_id=cid,
                chat_name=chat_names.get(cid, ""),
                content=content[:500],
                kind=d.get("kind") or "text",
                time_start=d.get("time_start"),
            ))
    except Exception:
        pass
    return out


@router.get("/api/users/{user_id}", response_model=UserDetailResponse)
async def get_user(user_id: str):
    """Full user card: profile, attitude, chats, recent memory/messages."""
    return _user_detail(user_id)


@router.put("/api/users/{user_id}", response_model=UserDetailResponse)
async def update_user(user_id: str, req: UserUpdateRequest):
    """Update Mila's attitude and profile data for a user."""
    data = req.model_dump(exclude_none=True)
    if not data:
        raise HTTPException(status_code=400, detail="No fields to update")

    if "sentiment" in data:
        try:
            s = float(data["sentiment"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="sentiment must be a number")
        if not -1.0 <= s <= 1.0:
            raise HTTPException(status_code=400, detail="sentiment must be between -1 and 1")
        data["sentiment"] = s

    if "relationship_type" in data:
        rt = str(data["relationship_type"]).strip()
        if not rt or len(rt) > 50:
            raise HTTPException(status_code=400, detail="Invalid relationship_type")
        data["relationship_type"] = rt

    for key in ("user_name", "preferred_name", "appearance_description"):
        if key in data and data[key] is not None:
            data[key] = str(data[key])
            if len(data[key]) > 2000:
                raise HTTPException(status_code=400, detail=f"{key} too long")

    allowed = {"user_name", "preferred_name", "relationship_type", "sentiment", "appearance_description"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields to update")

    conn = get_mila_conn()
    try:
        cur = conn.cursor()
        exists = cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not exists:
            cur.execute(
                "INSERT INTO users (user_id, user_name, preferred_name, relationship_type, sentiment, appearance_description) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id,
                 updates.get("user_name"),
                 updates.get("preferred_name"),
                 updates.get("relationship_type", "friend"),
                 updates.get("sentiment", 0.0),
                 updates.get("appearance_description")),
            )
        else:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            cur.execute(f"UPDATE users SET {set_clause} WHERE user_id = ?",
                        list(updates.values()) + [user_id])
        conn.commit()

        # Record attitude history when sentiment/relationship changed
        if "sentiment" in updates or "relationship_type" in updates:
            row = cur.execute(
                "SELECT sentiment, relationship_type FROM users WHERE user_id = ?",
                (user_id,)).fetchone()
            cur_s = float(row["sentiment"]) if row and row["sentiment"] is not None else 0.0
            cur_r = (row["relationship_type"] or "friend") if row else "friend"
            _ensure_history_table(conn)
            last = cur.execute(
                "SELECT sentiment, relationship_type FROM user_attitude_history "
                "WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
            if (not last or abs(float(last["sentiment"] or 0.0) - cur_s) > 1e-9
                    or (last["relationship_type"] or "") != cur_r):
                _record_attitude(conn, user_id, cur_s, cur_r)
    finally:
        conn.close()

    add_event("user_update", f"User {user_id[:8]}... updated: {', '.join(updates.keys())}")
    return _user_detail(user_id)


@router.get("/api/users/{user_id}/memories", response_model=MemoryListResponse)
async def list_user_memories(
    user_id: str,
    search: Optional[str] = Query(None, description="Search in content"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=200),
):
    """Paginated memories stored about a specific user."""
    conn = get_mila_conn()
    try:
        cur = conn.cursor()
        conds = ["user_id = ?"]
        params: list = [user_id]
        if search:
            conds.append("content LIKE ?")
            params.append(f"%{search}%")
        where = "WHERE " + " AND ".join(conds)
        total = cur.execute(f"SELECT COUNT(*) c FROM memories {where}", params).fetchone()["c"]
        offset = (page - 1) * limit
        rows = cur.execute(
            f"SELECT * FROM memories {where} ORDER BY time DESC LIMIT ? OFFSET ?",
            params + [limit, offset]).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            items.append(MemoryItem(
                mem_id=d.get("mem_id", ""),
                user_id=str(d.get("user_id", "")),
                content=d.get("content", ""),
                type=d.get("type", "fact"),
                tag=d.get("tag"),
                importance=d.get("importance", 0.5),
                confidence=0.5,
                source_chat_id=d.get("source_chat_id"),
                source_message_id=d.get("source_message_id"),
                created_at=d.get("time"),
                last_accessed_at=d.get("last_accessed_at"),
                faiss_id=d.get("faiss_id"),
                indexed=d.get("faiss_id") is not None,
            ))
    finally:
        conn.close()
    return MemoryListResponse(items=items, total=total, page=page, limit=limit)


@router.post("/api/users/{user_id}/memories", response_model=MemoryItem)
async def create_user_memory(user_id: str, req: UserMemoryCreateRequest):
    """Manually add a memory about a user (goes through the same pipeline as auto-extracted ones)."""
    # memories.user_id is a FK to users — make sure the row exists first.
    ensure_user_exists(user_id)
    mem_id, indexed = await get_indexer().add_memory(
        user_id=user_id,
        content=req.content,
        importance=req.importance,
        tags=[req.tag] if req.tag else None,
    )
    if not indexed:
        print(f"[Users API] memory {mem_id[:8]}... created but queued for reindex")
    # Best-effort: set type (add_memory defaults to 'fact')
    if req.type and req.type != "fact":
        conn = get_mila_conn()
        try:
            conn.execute("UPDATE memories SET type = ? WHERE mem_id = ?", (req.type, mem_id))
            conn.commit()
        finally:
            conn.close()
    add_event("memory_create", f"Memory for {user_id[:8]}...: {req.content[:50]}...")

    conn = get_mila_conn()
    try:
        d = dict(conn.execute("SELECT * FROM memories WHERE mem_id = ?", (mem_id,)).fetchone())
    finally:
        conn.close()
    return MemoryItem(
        mem_id=d.get("mem_id", ""),
        user_id=str(d.get("user_id", "")),
        content=d.get("content", ""),
        type=d.get("type", "fact"),
        tag=d.get("tag"),
        importance=d.get("importance", 0.5),
        confidence=0.5,
        source_chat_id=d.get("source_chat_id"),
        source_message_id=d.get("source_message_id"),
        created_at=d.get("time"),
        last_accessed_at=d.get("last_accessed_at"),
        faiss_id=d.get("faiss_id"),
        indexed=d.get("faiss_id") is not None,
    )


@router.get("/api/users/{user_id}/messages", response_model=UserMessageListResponse)
async def list_user_messages(
    user_id: str,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
):
    """Paginated dialog history (chunks) for a specific user, newest first."""
    conn = get_mila_conn()
    try:
        cur = conn.cursor()
        try:
            chat_names = {str(r["chat_id"]): (r["chat_name"] or "") for r in cur.execute("SELECT chat_id, chat_name FROM chats")}
        except Exception:
            chat_names = {}
        try:
            total = cur.execute("SELECT COUNT(*) c FROM chunks WHERE user_id = ?", (user_id,)).fetchone()["c"]
        except Exception:
            total = 0
        offset = (page - 1) * limit
        items = _recent_messages(cur, chat_names, user_id, limit=limit, offset=offset)
    finally:
        conn.close()
    return UserMessageListResponse(items=items, total=total, page=page, limit=limit)


@router.get("/api/users/{user_id}/attitude-history", response_model=AttitudeHistoryResponse)
async def get_attitude_history(user_id: str, limit: int = Query(100, ge=1, le=500)):
    """Sentiment/relationship change history for the attitude graph."""
    conn = get_mila_conn()
    try:
        _ensure_history_table(conn)
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT sentiment, relationship_type, changed_by, created_at "
            "FROM user_attitude_history WHERE user_id = ? ORDER BY id ASC LIMIT ?",
            (user_id, limit)).fetchall()
        items = [AttitudePoint(
            sentiment=float(r["sentiment"] or 0.0),
            relationship_type=r["relationship_type"] or "friend",
            changed_by=r["changed_by"] or "manual",
            created_at=r["created_at"],
        ) for r in rows]
    finally:
        conn.close()
    return AttitudeHistoryResponse(items=items, total=len(items))


@router.post("/api/users/{user_id}/portrait", response_model=PortraitResponse)
async def generate_portrait(user_id: str):
    """Generate an LLM portrait: what Mila thinks about this user, from stored memory."""
    detail = _user_detail(user_id)

    conn = get_mila_conn()
    try:
        cur = conn.cursor()
        try:
            mem_rows = cur.execute(
                "SELECT content, type, tag, importance, time FROM memories "
                "WHERE user_id = ? ORDER BY importance DESC, time DESC LIMIT 50",
                (user_id,)).fetchall()
            memories_text = "\n".join(
                f"- [{dict(r).get('type', 'fact')}] {dict(r).get('content', '')[:300]}"
                for r in mem_rows) or "(памяти пока нет)"
        except Exception:
            memories_text = "(память недоступна)"
    finally:
        conn.close()

    recent = "\n".join(
        f"- {m.content[:200]}" for m in detail.recent_messages[:10]) or "(переписки пока нет)"

    prompt = (
        "Составь короткий психологический портрет пользователя по данным ниже. "
        "Пиши от лица наблюдателя, на русском, 5-10 предложений, без воды. "
        "Структура: кто это и чем живёт; характер и интересы; "
        "как к нему относится Мила и почему; что ей стоит узнать о нём.\n\n"
        f"Имя: {detail.display_name}\n"
        f"Тип связи: {detail.relationship_type}\n"
        f"Отношение Милы: {detail.closeness_label} (sentiment {detail.sentiment:.2f})\n"
        f"Сообщений: {detail.messages_count}, воспоминаний: {detail.memories_count}, "
        f"чатов: {detail.chats_count}\n\n"
        f"Память о пользователе:\n{memories_text}\n\n"
        f"Недавние сообщения:\n{recent}"
    )

    from Mila_observatory.llm_manager import llm_manager
    result = await llm_manager.test_query(prompt, params={"max_tokens": 1000})
    if not result.get("success"):
        raise HTTPException(status_code=502, detail=f"LLM failed: {result.get('error', 'unknown')}")

    add_event("portrait", f"Portrait generated for {user_id[:8]}...")
    return PortraitResponse(
        portrait=result.get("response", ""),
        model=result.get("model", ""),
        timing=result.get("timing", ""),
    )
