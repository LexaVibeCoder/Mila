"""Unit tests for the FAISS memory write path (block 1, M1–M14).

Everything runs against a temporary SQLite DB and temp index files; no
network, no Telegram, no real ``data/``.
"""

import os
import sqlite3

import numpy as np
import pytest

from core import db as core_db


class FakeEmbedder:
    """Deterministic 8-dim bag-of-chars embedding (no network)."""

    def __init__(self):
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        vectors = []
        for text in texts:
            v = np.zeros(8, dtype=np.float32)
            for ch in text:
                v[ord(ch) % 8] += 1.0
            norm = float(np.linalg.norm(v)) or 1.0
            vectors.append(v / norm)
        return np.asarray(vectors, dtype=np.float32)


class FailingEmbedder:
    async def embed(self, texts):
        raise RuntimeError("embedding backend unavailable")


@pytest.fixture
def memory_env(tmp_path, monkeypatch):
    monkeypatch.setattr(core_db, "DB_PATH", str(tmp_path / "mila.db"))
    core_db.init_db()
    core_db.ensure_user_exists("u1", "User")
    core_db.ensure_chat_exists("c1", "User")

    from core.memory.indexer import MemoryIndexer
    from core.memory.vector import VectorMemory

    text_path = str(tmp_path / "faiss_text.index")
    chunk_path = str(tmp_path / "faiss_chunks.index")
    vector = VectorMemory(text_path, dim=8)
    chunk_vector = VectorMemory(chunk_path, dim=8)
    embedder = FakeEmbedder()
    indexer = MemoryIndexer(
        vector=vector, chunk_vector=chunk_vector, embedder=embedder)
    return {
        "indexer": indexer,
        "vector": vector,
        "chunk_vector": chunk_vector,
        "embedder": embedder,
        "text_path": text_path,
        "chunk_path": chunk_path,
        "tmp": tmp_path,
    }


async def test_add_memory_is_indexed_and_searchable(memory_env):
    indexer = memory_env["indexer"]
    vector = memory_env["vector"]
    embedder = memory_env["embedder"]

    mem_id, indexed = await indexer.add_memory(
        user_id="u1", content="alpha fact", importance=0.9)

    assert indexed is True
    row = core_db.get_memory(mem_id)
    assert row["faiss_id"] is not None

    vec = (await embedder.embed(["alpha fact"]))[0]
    _, ids = vector.search(vec, k=5)
    assert int(row["faiss_id"]) in [int(i) for i in ids if i != -1]


async def test_index_survives_reload(memory_env):
    indexer = memory_env["indexer"]
    embedder = memory_env["embedder"]
    mem_id, _ = await indexer.add_memory(
        user_id="u1", content="beta fact")

    from core.memory.vector import VectorMemory
    reloaded = VectorMemory(memory_env["text_path"], dim=8)
    vec = (await embedder.embed(["beta fact"]))[0]
    _, ids = reloaded.search(vec, k=3)

    row = core_db.get_memory(mem_id)
    assert int(row["faiss_id"]) in [int(i) for i in ids if i != -1]


async def test_search_sees_other_process_write(memory_env):
    """M3: a second VectorMemory instance refreshes before searching."""
    indexer = memory_env["indexer"]
    embedder = memory_env["embedder"]

    from core.memory.vector import VectorMemory
    reader = VectorMemory(memory_env["text_path"], dim=8)

    mem_id, _ = await indexer.add_memory(user_id="u1", content="Новый факт")
    vec = (await embedder.embed(["Новый факт"]))[0]
    # no explicit reload on ``reader`` — search() must refresh_if_changed()
    _, ids = reader.search(vec, k=3)

    row = core_db.get_memory(mem_id)
    assert int(row["faiss_id"]) in [int(i) for i in ids if i != -1]


async def test_delete_removes_vector(memory_env):
    indexer = memory_env["indexer"]
    vector = memory_env["vector"]
    embedder = memory_env["embedder"]

    mem_id, _ = await indexer.add_memory(user_id="u1", content="Удаляемый факт")
    faiss_id = core_db.get_memory(mem_id)["faiss_id"]
    assert vector.index.ntotal == 1

    await indexer.remove_memory(mem_id, faiss_id)
    assert vector.index.ntotal == 0

    vec = (await embedder.embed(["Удаляемый факт"]))[0]
    _, ids = vector.search(vec, k=3)
    assert faiss_id not in [int(i) for i in ids if i != -1]


async def test_parallel_creates_get_unique_faiss_ids(memory_env):
    """M6: no SELECT MAX()+1 race inside the FAISS file lock."""
    indexer = memory_env["indexer"]

    async def add(i):
        mem_id, _ = await indexer.add_memory(
            user_id="u1", content=f"Факт номер {i}")
        return core_db.get_memory(mem_id)["faiss_id"]

    faiss_ids = await __import__("asyncio").gather(*(add(i) for i in range(10)))
    assert all(fid is not None for fid in faiss_ids)
    assert len(set(faiss_ids)) == 10
    assert memory_env["vector"].index.ntotal == 10


async def test_failed_embedding_keeps_row_for_retry(memory_env):
    """M7: create succeeds but reports indexed=False, row is queued."""
    from core.memory.indexer import MemoryIndexer
    from core.memory.vector import VectorMemory

    indexer = MemoryIndexer(
        vector=VectorMemory(memory_env["text_path"], dim=8),
        chunk_vector=VectorMemory(memory_env["chunk_path"], dim=8),
        embedder=FailingEmbedder(),
    )
    mem_id, indexed = await indexer.add_memory(
        user_id="u1", content="Не проиндексировано")

    assert indexed is False
    row = core_db.get_memory(mem_id)
    assert row["faiss_id"] is None
    assert mem_id in [r["mem_id"] for r in core_db.get_pending_memories()]


async def test_rebuild_assigns_unique_ids(memory_env):
    """M8: rebuild keeps valid ids and gives fresh unique ids to NULL rows."""
    indexer = memory_env["indexer"]

    ids = [
        core_db.add_memory(user_id="u1", content=f"Старый факт {i}")
        for i in range(3)
    ]
    # One row already has an id, the rest are NULL.
    core_db.update_memory_faiss_id(ids[0], 7)

    result = await indexer.rebuild()
    assert result["count"] == 3
    assert indexer.vector.index.ntotal == 3

    assigned = [core_db.get_memory(mid)["faiss_id"] for mid in ids]
    assert all(x is not None for x in assigned)
    assert len(set(assigned)) == 3

    # A second rebuild after deleting a row must not create collisions.
    conn = core_db.get_conn()
    try:
        conn.execute("DELETE FROM memories WHERE mem_id=?", (ids[0],))
        conn.commit()
    finally:
        conn.close()
    await indexer.rebuild()
    assigned = [core_db.get_memory(mid)["faiss_id"] for mid in ids[1:]]
    assert len(set(assigned)) == 2


def test_empty_text_chunk_is_marked_silence(memory_env):
    """M10: empty assistant turns are not normal searchable text."""
    indexer = memory_env["indexer"]
    chunk_id = indexer.add_chunk("c1", "u1", content="", kind="text")

    row = core_db.get_chunk(chunk_id)
    assert row["kind"] == "silence"
    assert row["faiss_id"] is None


def test_photo_chunk_keeps_media_path(memory_env):
    """M11: media path passed by the queue is persisted."""
    indexer = memory_env["indexer"]
    photo = memory_env["tmp"] / "photo.jpg"
    photo.write_bytes(b"not-a-real-jpeg")

    chunk_id = indexer.add_chunk(
        "c1", "u1", content="[photo]", kind="photo", media_path=str(photo))

    row = core_db.get_chunk(chunk_id)
    assert row["media_path"] == str(photo)


def test_detach_media_clears_media_log(memory_env):
    """M12: deleting summarized media must not leave dead paths in media_logs."""
    indexer = memory_env["indexer"]
    photo = memory_env["tmp"] / "photo.jpg"
    photo.write_bytes(b"x")
    core_db.log_media("c1", "u1", str(photo), "[photo]")

    chunk_id = indexer.add_chunk(
        "c1", "u1", content="[photo]", kind="photo", media_path=str(photo))
    detached = core_db.detach_media_by_chunk_ids([chunk_id])

    assert detached == [str(photo)]
    assert core_db.get_chunk(chunk_id)["media_path"] is None
    conn = core_db.get_conn()
    try:
        row = conn.execute(
            "SELECT file_path, detached_at FROM media_logs WHERE chat_id='c1'"
        ).fetchone()
    finally:
        conn.close()
    assert row["file_path"] is None
    assert row["detached_at"] is not None


async def test_sqlite_fallback_when_embeddings_down(memory_env):
    """M9: search_memory falls back to keyword SQLite search."""
    from core.tools.executor import ToolExecutor

    await memory_env["indexer"].add_memory(
        user_id="u1", content="alpha fact")

    class BrokenVector:
        def refresh_if_changed(self):
            return False

        def search(self, *_args, **_kwargs):
            raise RuntimeError("faiss unavailable")

    executor = ToolExecutor(
        text_memory=BrokenVector(),
        embedding_client=FailingEmbedder(),
        current_user_id="u1",
        chat_id="c1",
    )
    result = await executor.execute_action(
        {"action_type": "search_memory", "params": {"query": "alpha"}})

    assert result["fallback"] is True
    assert result["source"] == "sqlite_fallback"
    assert result["results"]
    assert "alpha" in result["results"][0]["content"]


async def test_startup_rebuilds_missing_index(memory_env):
    """M2: deleting the index then starting must recreate it from SQLite."""
    from core.memory.indexer import MemoryIndexer
    from core.memory.vector import VectorMemory

    # Seed rows without indexing them (simulates rows written by an old build).
    ids = [core_db.add_memory(user_id="u1", content=f"Факт {i}") for i in range(4)]
    assert all(core_db.get_memory(mid)["faiss_id"] is None for mid in ids)
    assert not os.path.exists(memory_env["text_path"])

    fresh = MemoryIndexer(
        vector=VectorMemory(memory_env["text_path"], dim=8),
        chunk_vector=VectorMemory(memory_env["chunk_path"], dim=8),
        embedder=memory_env["embedder"],
    )
    health = await fresh.ensure_index_ready()

    assert os.path.exists(memory_env["text_path"])
    assert fresh.vector.index.ntotal == 4
    assert health["memories"]["consistent"] is True
    assert all(core_db.get_memory(mid)["faiss_id"] is not None for mid in ids)


async def test_two_index_instances_do_not_lose_writes(memory_env):
    """M4: two writers (bot/Observatory stand-ins) merge through the file lock."""
    from core.memory.indexer import MemoryIndexer
    from core.memory.vector import VectorMemory

    a = MemoryIndexer(
        vector=VectorMemory(memory_env["text_path"], dim=8),
        chunk_vector=VectorMemory(memory_env["chunk_path"], dim=8),
        embedder=memory_env["embedder"],
    )
    b = MemoryIndexer(
        vector=VectorMemory(memory_env["text_path"], dim=8),
        chunk_vector=VectorMemory(memory_env["chunk_path"], dim=8),
        embedder=memory_env["embedder"],
    )

    id_a, _ = await a.add_memory(user_id="u1", content="Пишет процесс A")
    id_b, _ = await b.add_memory(user_id="u1", content="Пишет процесс B")

    from core.memory.vector import VectorMemory as VM
    final = VM(memory_env["text_path"], dim=8)
    assert final.index.ntotal == 2
    assert core_db.get_memory(id_a)["faiss_id"] in final.ids()
    assert core_db.get_memory(id_b)["faiss_id"] in final.ids()


async def test_health_matches_sqlite(memory_env):
    indexer = memory_env["indexer"]
    await indexer.add_memory(user_id="u1", content="Факт для health")
    indexer.add_chunk("c1", "u1", content="Обычный чат-чанк")
    await indexer.reindex_pending()

    health = indexer.health()
    assert health["memories"]["consistent"] is True
    assert health["chunks"]["consistent"] is True


def test_faiss_id_indexes_exist(memory_env):
    """M13: faiss_id lookups use an index."""
    conn = sqlite3.connect(core_db.DB_PATH)
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        conn.close()
    assert "idx_memories_faiss" in names
    assert "idx_chunks_faiss" in names
