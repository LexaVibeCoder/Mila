"""Single write path for FAISS-backed memory.

Before this module every caller inserted a row into SQLite and forgot the
vector, so ``memories.faiss_id`` stayed NULL and search was always empty.
``MemoryIndexer`` owns the whole sequence:

    SQLite insert → embedding → FAISS add (cross-process lock) → UPDATE faiss_id

A NULL ``faiss_id`` is the retry queue: if embeddings are temporarily down,
the row still exists and a background pass (bot maintenance loop or the
Observatory health/rebuild endpoints) indexes it later.

Two indexes are used on purpose:
  * ``FAISS_TEXT_PATH``  — user facts (``memories``), searched by the model;
  * ``FAISS_CHUNK_PATH`` — conversation chunks, kept for completeness.
Separate files keep the integer id spaces independent, so a chunk can never
shadow a memory in ``search_memory``.
"""

import asyncio
import os
import threading
from typing import Any, List

import faiss
import numpy as np

from config import settings
from core.db import (
    add_chunk as db_add_chunk,
    add_memory as db_add_memory,
    clear_chunk_faiss_ids,
    clear_memory_faiss_ids,
    count_indexable_chunks,
    count_indexable_memories,
    count_pending_memories,
    ensure_user_exists,
    get_all_chunk_faiss_ids,
    get_all_memory_faiss_ids,
    get_chunk,
    get_chunks_by_faiss_ids,
    get_indexable_chunks,
    get_indexable_memories,
    get_memory,
    get_memories_by_faiss_ids,
    get_pending_chunks,
    get_pending_memories,
    max_chunk_faiss_id,
    max_memory_faiss_id,
    set_chunk_faiss_ids,
    set_memory_faiss_ids,
    update_chunk_faiss_id,
    update_memory_faiss_id,
)
from core.llm.embeddings import EmbeddingClient
from core.memory.vector import VectorMemory, _new_index


class MemoryIndexer:
    def __init__(
        self,
        vector: VectorMemory | None = None,
        chunk_vector: VectorMemory | None = None,
        embedder: Any | None = None,
    ):
        self.vector = vector or VectorMemory(
            settings.FAISS_TEXT_PATH, dim=settings.EMBED_DIM)
        chunk_path = getattr(settings, "FAISS_CHUNK_PATH", "data/faiss_chunks.index")
        self.chunk_vector = chunk_vector or VectorMemory(
            chunk_path, dim=settings.EMBED_DIM)
        self.embedder = embedder or EmbeddingClient()
        self.embed_batch_size = 64
        # Serialize embed+write inside one process; cross-process safety is
        # provided by VectorMemory's flock.
        self._mutation_lock = asyncio.Lock()
        self._bg_tasks: set[asyncio.Task] = set()

    # ── embeddings ───────────────────────────────────────────────────

    async def _embed_many(self, texts: List[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.vector.dim), dtype=np.float32)
        parts: List[np.ndarray] = []
        for start in range(0, len(texts), self.embed_batch_size):
            batch = texts[start:start + self.embed_batch_size]
            vectors = await self.embedder.embed(batch)
            arr = np.asarray(vectors, dtype=np.float32)
            if arr.ndim == 1:
                arr = arr.reshape(1, -1)
            if arr.shape[0] != len(batch):
                raise ValueError(
                    f"embedding count mismatch: got {arr.shape[0]} for {len(batch)} texts")
            parts.append(arr)
        return np.vstack(parts)

    async def _embed_one(self, text: str) -> np.ndarray:
        return (await self._embed_many([text]))[0]

    # ── memories ─────────────────────────────────────────────────────

    async def add_memory(
        self,
        user_id: str,
        content: str,
        importance: float = 0.5,
        tags: list[str] | None = None,
        source_chat_id: str | None = None,
        source_message_id: str | None = None,
        faiss_id: int | None = None,
    ) -> tuple[str, bool]:
        """Insert a memory and index it. Returns ``(mem_id, indexed)``.

        On embedding failure the row is still created (``indexed=False``,
        ``faiss_id=NULL``) and will be picked up by ``reindex_pending()``.
        """
        content = (content or "").strip()
        if not content:
            raise ValueError("memory content must not be empty")
        try:
            ensure_user_exists(user_id)
        except Exception:
            # FK/missing user: let the insert surface the real error.
            pass

        mem_id = db_add_memory(
            user_id=user_id,
            content=content,
            importance=importance,
            tags=tags,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            faiss_id=faiss_id,
        )

        if faiss_id is not None:
            async with self._mutation_lock:
                try:
                    vec = await self._embed_one(content)
                    self.vector.add(np.asarray([vec]), [int(faiss_id)])
                    return mem_id, True
                except Exception as e:
                    print(f"[MemoryIndexer] memory {mem_id[:8]}... index failed: {e}")
                    update_memory_faiss_id(mem_id, None)
                    return mem_id, False

        indexed = await self.index_memory(mem_id)
        return mem_id, indexed

    async def index_memory(self, mem_id: str, force: bool = False) -> bool:
        """Embed and index one memory row; returns whether it is searchable."""
        async with self._mutation_lock:
            # Read inside the lock: a background task may have indexed this
            # row between the caller's snapshot and now.
            row = get_memory(mem_id)
            if not row:
                return False
            content = (row.get("content") or "").strip()
            if not content:
                return False

            existing = row.get("faiss_id")
            if existing is not None and not force:
                if int(existing) in self.vector.ids():
                    return True

            try:
                vec = await self._embed_one(content)
                # min_id keeps the new id above anything already claimed in
                # SQLite, even if that row failed to reach the index earlier.
                new_ids = self.vector.add_new(
                    np.asarray([vec]), min_id=max_memory_faiss_id() + 1)
                update_memory_faiss_id(mem_id, new_ids[0])
                return True
            except Exception as e:
                print(f"[MemoryIndexer] memory {mem_id[:8]}... index failed: {e}")
                return False

    async def remove_memory(self, mem_id: str, faiss_id: int | None = None) -> bool:
        """Remove a memory's vector (row deletion is the caller's job)."""
        if faiss_id is None:
            row = get_memory(mem_id)
            faiss_id = row.get("faiss_id") if row else None
        if faiss_id is None:
            return False
        try:
            self.vector.remove_ids([int(faiss_id)])
            return True
        except Exception as e:
            print(f"[MemoryIndexer] memory {mem_id[:8]}... remove failed: {e}")
            return False

    # ── chunks ───────────────────────────────────────────────────────

    def add_chunk(self, chat_id: str, user_id: str, content: str,
                  token_count: int = 0, type: str = "raw",
                  llm_full_response: str | None = None, kind: str = "text",
                  media_path: str | None = None) -> str:
        """Insert a chunk (sync, call sites are sync) and index it in background.

        Returning immediately keeps message handling fast; if no event loop is
        running the row simply stays in the NULL queue and is indexed by the
        maintenance pass.
        """
        chunk_id = db_add_chunk(
            chat_id, user_id, content, token_count=token_count, type=type,
            llm_full_response=llm_full_response, kind=kind, media_path=media_path,
        )
        if (content or "").strip() and (kind or "text").lower() != "silence":
            self._schedule(self._safe_index_chunk(chunk_id))
        return chunk_id

    async def _safe_index_chunk(self, chunk_id: str) -> None:
        try:
            await self.index_chunk(chunk_id)
        except Exception as e:
            print(f"[MemoryIndexer] background chunk index failed: {e}")

    def _schedule(self, coro) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Called outside asyncio (CLI/tests): reindex_pending() later.
            coro.close()
            return
        # If the embedding backend is slow/down, don't grow an unbounded
        # task pile — these rows stay NULL and the maintenance pass retries.
        if len(self._bg_tasks) >= 32:
            coro.close()
            return
        task = loop.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def index_chunk(self, chunk_id: str, force: bool = False) -> bool:
        async with self._mutation_lock:
            row = get_chunk(chunk_id)
            if not row:
                return False
            content = (row.get("content") or "").strip()
            kind = (row.get("kind") or "text").lower()
            if not content or kind == "silence":
                return False

            existing = row.get("faiss_id")
            if existing is not None and not force:
                if int(existing) in self.chunk_vector.ids():
                    return True

            try:
                vec = await self._embed_one(content)
                new_ids = self.chunk_vector.add_new(
                    np.asarray([vec]), min_id=max_chunk_faiss_id() + 1)
                update_chunk_faiss_id(chunk_id, new_ids[0])
                return True
            except Exception as e:
                print(f"[MemoryIndexer] chunk {chunk_id[:8]}... index failed: {e}")
                return False

    # ── queues / startup ─────────────────────────────────────────────

    async def reindex_pending(self, memory_limit: int | None = None,
                              chunk_limit: int | None = None) -> dict:
        """Index rows whose faiss_id is NULL. Never deletes rows on failure."""
        mem_limit = settings.MEMORY_REINDEX_BATCH if memory_limit is None else memory_limit
        chunk_limit = settings.MEMORY_REINDEX_BATCH if chunk_limit is None else chunk_limit
        result = {"memories": 0, "chunks": 0, "failed": 0}
        for row in get_pending_memories(mem_limit):
            if await self.index_memory(row["mem_id"]):
                result["memories"] += 1
            else:
                result["failed"] += 1
        for row in get_pending_chunks(chunk_limit):
            if await self.index_chunk(row["chunk_id"]):
                result["chunks"] += 1
            else:
                result["failed"] += 1
        if result["memories"] or result["chunks"] or result["failed"]:
            print(f"[MemoryIndexer] reindexed: {result}")
        return result

    async def ensure_index_ready(self) -> dict:
        """Repair the indexes after a restart / missing file.

        * If the memories index is missing/empty but SQLite has rows → full
          rebuild from ``memories`` (M2).
        * Otherwise index pending rows and drop index ids that no longer have
          a row, so ``ntotal`` matches SQLite (health).
        """
        self.vector.refresh_if_changed()
        self.chunk_vector.refresh_if_changed()

        db_total = count_indexable_memories()
        if db_total > 0 and (not os.path.exists(self.vector.index_path)
                             or self.vector.index.ntotal == 0):
            print(f"[MemoryIndexer] rebuilding empty index from {db_total} memories")
            await self.rebuild()

        db_chunks = count_indexable_chunks()
        if db_chunks > 0 and (not os.path.exists(self.chunk_vector.index_path)
                              or self.chunk_vector.index.ntotal == 0):
            print(f"[MemoryIndexer] rebuilding empty chunk index from {db_chunks} chunks")
            await self.rebuild_chunks()

        self._sync_memories()
        self._sync_chunks()
        await self.reindex_pending(
            memory_limit=_all_or_batch(count_pending_memories()),
        )
        return self.health()

    def _sync_memories(self) -> None:
        db_ids = get_all_memory_faiss_ids()
        index_ids = self.vector.ids()
        missing = db_ids - index_ids
        if missing:
            rows = get_memories_by_faiss_ids(sorted(missing))
            clear_memory_faiss_ids([r["mem_id"] for r in rows])
            print(f"[MemoryIndexer] {len(rows)} memory rows re-queued (vector missing)")
        orphans = index_ids - db_ids
        if orphans:
            self.vector.remove_ids(sorted(orphans))
            print(f"[MemoryIndexer] removed {len(orphans)} orphan memory vectors")

    def _sync_chunks(self) -> None:
        db_ids = get_all_chunk_faiss_ids()
        index_ids = self.chunk_vector.ids()
        missing = db_ids - index_ids
        if missing:
            rows = get_chunks_by_faiss_ids(sorted(missing))
            clear_chunk_faiss_ids([r["chunk_id"] for r in rows])
            print(f"[MemoryIndexer] {len(rows)} chunk rows re-queued (vector missing)")
        orphans = index_ids - db_ids
        if orphans:
            self.chunk_vector.remove_ids(sorted(orphans))
            print(f"[MemoryIndexer] removed {len(orphans)} orphan chunk vectors")

    # ── full rebuild ─────────────────────────────────────────────────

    async def _rebuild(self, rows: List[dict], id_key: str, text_key: str,
                       max_id_getter, db_id_getter,
                       vector: VectorMemory, label: str) -> dict:
        if not rows:
            vector.replace_index(_new_index(vector.dim), persist=True)
            return {"success": True, "message": f"No {label} to index", "count": 0}

        texts = [r[text_key] for r in rows]
        async with self._mutation_lock:
            vectors = await self._embed_many(texts)

        used: set[int] = set()
        occupied = set(db_id_getter()) | set(vector.ids())
        next_id = max([max_id_getter(), *occupied], default=0) + 1
        ids: List[int] = []
        updates: dict[str, int] = {}
        for row, vec in zip(rows, vectors):
            old = row.get("faiss_id")
            if old is not None and int(old) not in used:
                assigned = int(old)
            else:
                assigned = next_id
                next_id += 1
            used.add(assigned)
            ids.append(assigned)
            if old != assigned:
                updates[row[id_key]] = assigned

        new_index = faiss.IndexIDMap(faiss.IndexFlatL2(vector.dim))
        new_index.add_with_ids(
            np.asarray(vectors, dtype=np.float32),
            np.asarray(ids, dtype=np.int64),
        )
        vector.replace_index(new_index, persist=True)
        # Persist the id mapping after the index write. If this fails, the
        # next startup health check sees index/db mismatch and re-queues them.
        if id_key == "mem_id":
            set_memory_faiss_ids(updates)
        else:
            set_chunk_faiss_ids(updates)
        print(f"[MemoryIndexer] rebuilt {label}: {len(ids)} vectors")
        return {"success": True, "message": f"{label} FAISS index rebuilt",
                "count": len(ids)}

    async def rebuild(self) -> dict:
        return await self._rebuild(
            get_indexable_memories(), "mem_id", "content",
            max_memory_faiss_id, get_all_memory_faiss_ids,
            self.vector, "memories")

    async def rebuild_chunks(self) -> dict:
        return await self._rebuild(
            get_indexable_chunks(), "chunk_id", "content",
            max_chunk_faiss_id, get_all_chunk_faiss_ids,
            self.chunk_vector, "chunks")

    # ── health ───────────────────────────────────────────────────────

    def health(self) -> dict:
        self.vector.refresh_if_changed()
        self.chunk_vector.refresh_if_changed()
        mem_db = count_indexable_memories()
        mem_indexed = len(get_all_memory_faiss_ids())
        mem_ntotal = int(self.vector.index.ntotal if self.vector.index else 0)
        chunk_db = count_indexable_chunks()
        chunk_indexed = len(get_all_chunk_faiss_ids())
        chunk_ntotal = int(self.chunk_vector.index.ntotal if self.chunk_vector.index else 0)
        return {
            "memories": {
                "path": self.vector.index_path,
                "exists": os.path.exists(self.vector.index_path),
                "ntotal": mem_ntotal,
                "db_total": mem_db,
                "db_indexed": mem_indexed,
                "pending": max(0, mem_db - mem_indexed),
                "consistent": mem_ntotal == mem_db == mem_indexed,
            },
            "chunks": {
                "path": self.chunk_vector.index_path,
                "exists": os.path.exists(self.chunk_vector.index_path),
                "ntotal": chunk_ntotal,
                "db_total": chunk_db,
                "db_indexed": chunk_indexed,
                "pending": max(0, chunk_db - chunk_indexed),
                "consistent": chunk_ntotal == chunk_db == chunk_indexed,
            },
        }


def _all_or_batch(pending: int) -> int:
    """Startup should drain the whole queue; runtime passes stay bounded."""
    return max(pending, settings.MEMORY_REINDEX_BATCH)


_INDEXER: MemoryIndexer | None = None
_INDEXER_LOCK = threading.Lock()


def get_indexer(embedder: Any | None = None,
                vector: VectorMemory | None = None,
                chunk_vector: VectorMemory | None = None) -> MemoryIndexer:
    """Process-wide indexer (bot and each Observatory worker have their own)."""
    global _INDEXER
    with _INDEXER_LOCK:
        if (_INDEXER is None or (vector is not None and vector is not _INDEXER.vector)
                or (chunk_vector is not None and chunk_vector is not _INDEXER.chunk_vector)):
            _INDEXER = MemoryIndexer(
                vector=vector, chunk_vector=chunk_vector, embedder=embedder)
        return _INDEXER


def reset_indexer() -> None:
    """Test helper: forget the process singleton."""
    global _INDEXER
    with _INDEXER_LOCK:
        _INDEXER = None
