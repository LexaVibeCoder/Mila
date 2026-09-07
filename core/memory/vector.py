import faiss
import numpy as np
import os
import threading
import time
from contextlib import contextmanager
from typing import Iterator, List, Tuple


@contextmanager
def _file_lock(lock_path: str) -> Iterator[None]:
    """Interprocess exclusive lock via a sidecar file (POSIX fcntl).

    Serializes concurrent save() calls from the bot and Observatory
    processes. No-op on platforms without fcntl (Windows) — the atomic
    tmp+replace below still protects against torn files.
    """
    try:
        import fcntl
    except ImportError:
        yield
        return
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)) or ".", exist_ok=True)
    with open(lock_path, "a+b") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


class VectorMemory:
    def __init__(self, index_path: str, dim: int):
        self.index_path = index_path
        self.dim = dim
        self.index = None
        self._lock = threading.RLock()
        self.load_or_create()

    @property
    def _lock_path(self) -> str:
        return self.index_path + ".lock"

    def load_or_create(self):
        with self._lock:
            if os.path.exists(self.index_path):
                try:
                    self.index = faiss.read_index(self.index_path)
                except Exception as e:
                    # Never crash on a torn/corrupt index: quarantine it
                    # and start fresh (vectors can be rebuilt from SQLite).
                    corrupt = f"{self.index_path}.corrupt-{int(time.time())}"
                    try:
                        os.replace(self.index_path, corrupt)
                        print(f"Corrupt FAISS index moved to {corrupt}: {e}")
                    except OSError:
                        print(f"Failed to quarantine corrupt index: {e}")
                    base_index = faiss.IndexFlatL2(self.dim)
                    self.index = faiss.IndexIDMap(base_index)
                    print(f"Created new index with dim {self.dim}")
                    return
                print(f"Loaded index from {self.index_path}, size: {self.index.ntotal}")
            else:
                base_index = faiss.IndexFlatL2(self.dim)
                self.index = faiss.IndexIDMap(base_index)
                print(f"Created new index with dim {self.dim}")

    def add(self, vectors: np.ndarray, ids: List[int]):
        with self._lock:
            if self.index is None:
                raise RuntimeError("Index not initialized")
            self.index.add_with_ids(vectors.astype(np.float32), np.array(ids, dtype=np.int64))
            print(f"Added {len(ids)} vectors, total: {self.index.ntotal}")

    def search(self, query_vector: np.ndarray, k: int = 5) -> Tuple[np.ndarray, np.ndarray]:
        with self._lock:
            if self.index is None:
                return np.array([]), np.array([])
            query = query_vector.astype(np.float32).reshape(1, -1)
            distances, indices = self.index.search(query, k)
            return distances[0], indices[0]

    def save(self):
        with self._lock:
            if self.index is None or self.index.ntotal == 0:
                return
            index_dir = os.path.dirname(os.path.abspath(self.index_path))
            if index_dir:
                os.makedirs(index_dir, exist_ok=True)
            # Atomic write: crash mid-write leaves the old index intact.
            # Interprocess lock: two writers (bot + Observatory) can't interleave.
            with _file_lock(self._lock_path):
                tmp_path = f"{self.index_path}.tmp-{os.getpid()}"
                faiss.write_index(self.index, tmp_path)
                try:
                    os.replace(tmp_path, self.index_path)
                except BaseException:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise
            print(f"Index saved to {self.index_path}")

    def rebuild_from_active(self, active_ids: List[int], all_vectors: np.ndarray):
        with self._lock:
            new_index = faiss.IndexIDMap(faiss.IndexFlatL2(self.dim))
            if len(active_ids) > 0:
                new_index.add_with_ids(all_vectors.astype(np.float32), np.array(active_ids, dtype=np.int64))
            self.index = new_index
        self.save()
