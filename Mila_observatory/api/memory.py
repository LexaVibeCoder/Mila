"""
Memory API — CRUD for memories, search, FAISS rebuild/health.
"""

import sys
import os
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import APIRouter, HTTPException, Query
from Mila_observatory.models import (
    MemoryItem, MemoryListResponse, MemoryCreateRequest, MemoryUpdateRequest
)
from Mila_observatory.api.dashboard import add_event
from core.db import (
    get_conn as get_mila_conn,
    get_memory as db_get_memory,
    update_memory_faiss_id,
)
from core.memory.indexer import get_indexer

router = APIRouter(tags=["memory"])


def _to_item(d: dict) -> MemoryItem:
    faiss_id = d.get("faiss_id")
    return MemoryItem(
        mem_id=d.get("mem_id", ""),
        user_id=str(d.get("user_id", "")),
        content=d.get("content", "") or "",
        type=d.get("type", "fact"),
        tag=d.get("tag"),
        importance=d.get("importance", 0.5),
        confidence=d.get("confidence", 0.5),
        source_chat_id=d.get("source_chat_id"),
        source_message_id=d.get("source_message_id"),
        created_at=d.get("time") or d.get("created_at"),
        last_accessed_at=d.get("last_accessed_at"),
        faiss_id=faiss_id,
        indexed=faiss_id is not None,
    )


@router.get("/api/memory", response_model=MemoryListResponse)
async def list_memories(
    query: Optional[str] = Query(None, description="Search text"),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    category: Optional[str] = Query(None, description="Filter by type/tag"),
):
    """List memories with optional search and filtering."""
    conn = get_mila_conn()
    cur = conn.cursor()

    conditions = []
    params = []

    if query:
        conditions.append("content LIKE ?")
        params.append(f"%{query}%")
    if category:
        conditions.append("(type = ? OR tag LIKE ?)")
        params.extend([category, f"%{category}%"])

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    cur.execute(f"SELECT COUNT(*) as cnt FROM memories {where}", params)
    total = cur.fetchone()["cnt"]

    offset = (page - 1) * limit
    cur.execute(
        f"SELECT * FROM memories {where} ORDER BY time DESC LIMIT ? OFFSET ?",
        params + [limit, offset]
    )
    rows = cur.fetchall()
    conn.close()

    return MemoryListResponse(
        items=[_to_item(dict(r)) for r in rows],
        total=total, page=page, limit=limit,
    )


@router.get("/api/memory/health")
async def memory_index_health():
    """Index/SQLite consistency check (M2).

    ``consistent`` is true when FAISS ntotal == indexable DB rows == rows
    with a non-NULL faiss_id.
    """
    return {"success": True, **get_indexer().health()}


@router.get("/api/memory/{mem_id}", response_model=MemoryItem)
async def get_memory(mem_id: str):
    """Get a single memory by ID."""
    d = db_get_memory(mem_id)
    if not d:
        raise HTTPException(status_code=404, detail="Memory not found")
    return _to_item(d)


@router.put("/api/memory/{mem_id}", response_model=MemoryItem)
async def update_memory(mem_id: str, req: MemoryUpdateRequest):
    """Update a memory; content changes are re-embedded and re-indexed."""
    old = db_get_memory(mem_id)
    if not old:
        raise HTTPException(status_code=404, detail="Memory not found")

    update_dict = req.model_dump(exclude_none=True)
    if not update_dict:
        raise HTTPException(status_code=400, detail="No fields to update")

    set_parts = []
    params = []
    for key, value in update_dict.items():
        if key == "tag":
            set_parts.append("tag = ?")
        elif key == "content":
            set_parts.append("content = ?")
        elif key == "type":
            set_parts.append("type = ?")
        elif key == "importance":
            set_parts.append("importance = ?")
        else:
            continue
        params.append(value)

    if not set_parts:
        raise HTTPException(status_code=400, detail="No valid fields to update")

    content_changed = (
        "content" in update_dict
        and (update_dict.get("content") or "") != (old.get("content") or "")
    )

    params.append(mem_id)
    conn = get_mila_conn()
    try:
        conn.execute(f"UPDATE memories SET {', '.join(set_parts)} WHERE mem_id = ?", params)
        conn.commit()
    finally:
        conn.close()

    if content_changed:
        indexer = get_indexer()
        # Drop the stale vector first; NULL means "please reindex" if the
        # embedding backend is down right now.
        await indexer.remove_memory(mem_id, old.get("faiss_id"))
        update_memory_faiss_id(mem_id, None)
        indexed = await indexer.index_memory(mem_id, force=True)
        if not indexed:
            print(f"[Memory API] memory {mem_id[:8]}... updated but not indexed — queued")

    add_event("memory_update", f"Memory {mem_id[:8]}... updated")
    return await get_memory(mem_id)


@router.delete("/api/memory/{mem_id}")
async def delete_memory(mem_id: str):
    """Delete a memory and its FAISS vector."""
    old = db_get_memory(mem_id)
    if not old:
        raise HTTPException(status_code=404, detail="Memory not found")

    conn = get_mila_conn()
    try:
        conn.execute("DELETE FROM memories WHERE mem_id = ?", (mem_id,))
        conn.commit()
    finally:
        conn.close()

    # Stale vectors are harmless (search drops unknown ids) but would make
    # index.ntotal drift from SQLite forever; remove now (M5).
    await get_indexer().remove_memory(mem_id, old.get("faiss_id"))

    add_event("memory_delete", f"Memory {mem_id[:8]}... deleted")
    return {"success": True, "message": "Memory deleted"}


@router.post("/api/memory", response_model=MemoryItem)
async def create_memory(req: MemoryCreateRequest):
    """Create a memory through the shared indexer (insert + embed + FAISS).

    If embeddings are unavailable the row is still created with
    ``indexed=false`` and retried by the bot's maintenance loop (M7).
    """
    indexer = get_indexer()
    mem_id, indexed = await indexer.add_memory(
        user_id=req.user_id,
        content=req.content,
        importance=req.importance,
        tags=[req.tag] if req.tag else None,
        source_chat_id=req.source_chat_id,
    )

    if req.type and req.type != "fact":
        conn = get_mila_conn()
        try:
            conn.execute("UPDATE memories SET type = ? WHERE mem_id = ?", (req.type, mem_id))
            conn.commit()
        finally:
            conn.close()

    add_event("memory_create",
              f"Memory created: {req.content[:50]}... (indexed={indexed})")
    return await get_memory(mem_id)


@router.post("/api/memory/rebuild-faiss")
async def rebuild_faiss():
    """Rebuild the memory FAISS index from all memories in SQLite."""
    try:
        result = await get_indexer().rebuild()
        add_event("faiss_rebuild",
                  f"FAISS index rebuilt: {result.get('count', 0)} vectors")
        return result
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"FAISS rebuild failed: {str(e)}")
