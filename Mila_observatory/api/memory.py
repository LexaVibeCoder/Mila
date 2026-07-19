"""
Memory API — CRUD for memories, search, FAISS rebuild.
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
    search_memories_by_ids as db_search_memories_by_ids,
    add_memory as db_add_memory,
)
from core.embedding_client import EmbeddingClient

router = APIRouter(tags=["memory"])


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

    # Build query
    conditions = []
    params = []

    if query:
        conditions.append("content LIKE ?")
        params.append(f"%{query}%")
    if category:
        conditions.append("(type = ? OR tag LIKE ?)")
        params.extend([category, f"%{category}%"])

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    # Get total count
    cur.execute(f"SELECT COUNT(*) as cnt FROM memories {where}", params)
    total = cur.fetchone()["cnt"]

    # Get paginated results
    offset = (page - 1) * limit
    cur.execute(
        f"SELECT * FROM memories {where} ORDER BY time DESC LIMIT ? OFFSET ?",
        params + [limit, offset]
    )
    rows = cur.fetchall()
    conn.close()

    items = []
    for r in rows:
        d = dict(r)
        items.append(MemoryItem(
            mem_id=d.get("mem_id", ""),
            user_id=d.get("user_id", ""),
            content=d.get("content", ""),
            type=d.get("type", "fact"),
            tag=d.get("tag"),
            importance=d.get("importance", 0.5),
            confidence=d.get("confidence", 0.5),
            source_chat_id=d.get("source_chat_id"),
            source_message_id=d.get("source_message_id"),
            created_at=d.get("time") or d.get("created_at"),
            last_accessed_at=d.get("last_accessed_at"),
        ))

    return MemoryListResponse(items=items, total=total, page=page, limit=limit)


@router.get("/api/memory/{mem_id}", response_model=MemoryItem)
async def get_memory(mem_id: str):
    """Get a single memory by ID."""
    conn = get_mila_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM memories WHERE mem_id = ?", (mem_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Memory not found")

    d = dict(row)
    return MemoryItem(
        mem_id=d.get("mem_id", ""),
        user_id=d.get("user_id", ""),
        content=d.get("content", ""),
        type=d.get("type", "fact"),
        tag=d.get("tag"),
        importance=d.get("importance", 0.5),
        confidence=d.get("confidence", 0.5),
        source_chat_id=d.get("source_chat_id"),
        source_message_id=d.get("source_message_id"),
        created_at=d.get("time") or d.get("created_at"),
        last_accessed_at=d.get("last_accessed_at"),
    )


@router.put("/api/memory/{mem_id}", response_model=MemoryItem)
async def update_memory(mem_id: str, req: MemoryUpdateRequest):
    """Update a memory."""
    update_dict = req.model_dump(exclude_none=True)
    if not update_dict:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Build SET clause
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

    params.append(mem_id)

    conn = get_mila_conn()
    cur = conn.cursor()
    cur.execute(f"UPDATE memories SET {', '.join(set_parts)} WHERE mem_id = ?", params)
    conn.commit()
    conn.close()

    add_event("memory_update", f"Memory {mem_id[:8]}... updated")

    return await get_memory(mem_id)


@router.delete("/api/memory/{mem_id}")
async def delete_memory(mem_id: str):
    """Delete a memory."""
    conn = get_mila_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM memories WHERE mem_id = ?", (mem_id,))
    conn.commit()
    conn.close()

    add_event("memory_delete", f"Memory {mem_id[:8]}... deleted")
    return {"success": True, "message": "Memory deleted"}


@router.post("/api/memory", response_model=MemoryItem)
async def create_memory(req: MemoryCreateRequest):
    """Create a new memory with embedding."""
    # Create in SQLite
    mem_id = db_add_memory(
        user_id=req.user_id,
        content=req.content,
        importance=req.importance,
        tags=[req.tag] if req.tag else None,
        source_chat_id=req.source_chat_id,
    )

    # Create embedding and add to FAISS
    try:
        emb = EmbeddingClient()
        vec = await emb.embed([req.content])
        vec = vec[0]

        # Import here to avoid circular imports
        from core.memory import VectorMemory
        from config import settings

        text_memory = VectorMemory(settings.FAISS_TEXT_PATH, dim=settings.EMBED_DIM)
        # We need to get a unique FAISS ID
        from core.db import get_conn as get_mila_conn2
        conn2 = get_mila_conn2()
        cur2 = conn2.cursor()
        cur2.execute("SELECT COALESCE(MAX(faiss_id), 0) + 1 as next_id FROM memories")
        next_faiss_id = cur2.fetchone()["next_id"]
        conn2.close()

        import numpy as np
        text_memory.add(np.array([vec]), [next_faiss_id])

        # Update the memory record with faiss_id
        conn3 = get_mila_conn()
        conn3.execute("UPDATE memories SET faiss_id = ? WHERE mem_id = ?", (next_faiss_id, mem_id))
        conn3.commit()
        conn3.close()

        text_memory.save()
    except Exception as e:
        print(f"[Memory API] Warning: embedding failed: {e}")

    add_event("memory_create", f"Memory created: {req.content[:50]}...")

    return await get_memory(mem_id)


@router.post("/api/memory/rebuild-faiss")
async def rebuild_faiss():
    """Rebuild FAISS index from all memories in SQLite."""
    try:
        from core.memory import VectorMemory
        from core.embedding_client import EmbeddingClient
        from config import settings
        import numpy as np

        conn = get_mila_conn()
        cur = conn.cursor()
        cur.execute("SELECT mem_id, content, faiss_id FROM memories WHERE content IS NOT NULL AND content != ''")
        rows = cur.fetchall()
        conn.close()

        if not rows:
            return {"success": True, "message": "No memories to index", "count": 0}

        emb = EmbeddingClient()
        texts = [r["content"] for r in rows]
        existing_ids = [r["faiss_id"] for r in rows]

        print(f"[Memory API] Rebuilding FAISS: {len(texts)} memories...")

        # Generate embeddings in batches
        vectors = await emb.embed(texts)
        vectors = np.array(vectors)

        # Create new index
        text_memory = VectorMemory(settings.FAISS_TEXT_PATH, dim=settings.EMBED_DIM)
        from core.memory import VectorMemory as VM
        import faiss
        new_index = faiss.IndexIDMap(faiss.IndexFlatL2(settings.EMBED_DIM))

        valid_ids = []
        valid_vectors = []
        for i, (vid, vec) in enumerate(zip(existing_ids, vectors)):
            if vid is not None:
                valid_ids.append(int(vid))
                valid_vectors.append(vec)
            else:
                # Assign new FAISS ID
                new_id = max([x for x in existing_ids if x is not None] or [0]) + i + 1
                valid_ids.append(new_id)
                # Update the memory record
                conn2 = get_mila_conn()
                conn2.execute("UPDATE memories SET faiss_id = ? WHERE mem_id = ?", (new_id, rows[i]["mem_id"]))
                conn2.commit()
                conn2.close()
                valid_vectors.append(vec)

        if valid_ids:
            new_index.add_with_ids(
                np.array(valid_vectors).astype(np.float32),
                np.array(valid_ids, dtype=np.int64)
            )
            text_memory.index = new_index
            text_memory.save()

        add_event("faiss_rebuild", f"FAISS index rebuilt: {len(valid_ids)} vectors")
        return {"success": True, "message": "FAISS index rebuilt", "count": len(valid_ids)}

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"FAISS rebuild failed: {str(e)}")