import faiss
import numpy as np
import os
import threading
import time
from contextlib import contextmanager
from typing import Iterator, List, Sequence, Set, Tuple


@contextmanager
def _file_lock(lock_path: str) -> Iterator[None]:
    """Interprocess exclusive lock via a sidecar file (POSIX fcntl).

    Serializes read-modify-write of the shared index between the bot and
    Observatory processes. No-op on platforms without fcntl (Windows) — the
    atomic tmp+replace below still protects against torn files.
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


def _new_index(dim: int):
    return faiss.IndexIDMap(faiss.IndexFlatL2(dim))


def _index_ids(index) -> Set[int]:
    """All integer ids currently stored in an IndexIDMap."""
    try:
        return {int(x) for x in faiss.vector_to_array(index.id_map)}
    except Exception:
        return set()


class VectorMemory:
    """FAISS text index shared by the bot and Observatory.

    Every mutation that must survive a restart goes through the interprocess
    file lock: the latest on-disk index is reloaded, modified and written back
    atomically. This lets two processes add/remove vectors without silently
    overwriting each other's changes (the old implementation only locked
    ``save()`` and could lose a concurrent writer's in-memory state).
    """

    def __init__(self, index_path: str, dim: int):
        self.index_path = index_path
        self.dim = dim
        self.index = None
        self._lock = threading.RLock()
        # (mtime_ns, size) of the file we last loaded/wrote; used by
        # refresh_if_changed() to notice writes from the other process.
        self._signature: Tuple[int, int] | None = None
        self.load_or_create()

    @property
    def _lock_path(self) -> str:
        return self.index_path + ".lock"

    # ── loading / refresh ────────────────────────────────────────────

    def _file_signature(self) -> Tuple[int, int] | None:
        try:
            st = os.stat(self.index_path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _load_locked(self) -> None:
        """Read the index from disk (must be called with self._lock held)."""
        if os.path.exists(self.index_path):
            try:
                self.index = faiss.read_index(self.index_path)
                self._signature = self._file_signature()
                print(f"Loaded index from {self.index_path}, size: {self.index.ntotal}")
                return
            except Exception as e:
                # Never crash on a torn/corrupt index: quarantine it and
                # start fresh (vectors can be rebuilt from SQLite).
                corrupt = f"{self.index_path}.corrupt-{int(time.time())}"
                try:
                    os.replace(self.index_path, corrupt)
                    print(f"Corrupt FAISS index moved to {corrupt}: {e}")
                except OSError:
                    print(f"Failed to quarantine corrupt index: {e}")
        self.index = _new_index(self.dim)
        self._signature = self._file_signature()
        print(f"Created new index with dim {self.dim}")

    def load_or_create(self):
        with self._lock:
            self._load_locked()

    def refresh_if_changed(self) -> bool:
        """Reload the index if another process wrote it since our last read.

        Returns True when the in-memory copy was replaced. Called from
        ``search()`` so the bot immediately sees memories created in the
        Observatory (and vice versa) without a restart.
        """
        signature = self._file_signature()
        if signature == self._signature:
            return False
        with self._lock:
            signature = self._file_signature()
            if signature == self._signature:
                return False
            self._load_locked()
            return True

    # ── mutation ─────────────────────────────────────────────────────

    def ids(self) -> Set[int]:
        with self._lock:
            if self.index is None:
                return set()
            return _index_ids(self.index)

    def add(self, vectors: np.ndarray, ids: Sequence[int], persist: bool = True):
        """Add/replace vectors by id.

        With ``persist=True`` (default) the on-disk index is reloaded under
        the interprocess lock before the mutation, so a concurrent writer's
        vectors are preserved.
        """
        vectors = np.asarray(vectors, dtype=np.float32)
        ids_array = np.asarray(list(ids), dtype=np.int64)
        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)
        if ids_array.size == 0:
            return
        if vectors.shape[0] != ids_array.shape[0]:
            raise ValueError("vectors and ids must have the same length")

        with self._lock:
            if not persist:
                if self.index is None:
                    raise RuntimeError("Index not initialized")
                self._remove_duplicates_locked(ids_array)
                self.index.add_with_ids(vectors, ids_array)
                print(f"Added {len(ids_array)} vectors, total: {self.index.ntotal}")
                return

            with _file_lock(self._lock_path):
                self._load_locked()
                self._remove_duplicates_locked(ids_array)
                self.index.add_with_ids(vectors, ids_array)
                self._write_locked()
            print(f"Added {len(ids_array)} vectors, total: {self.index.ntotal}")

    def add_new(self, vectors: np.ndarray, min_id: int = 0,
                persist: bool = True) -> List[int]:
        """Add vectors with freshly allocated ids and return those ids.

        Allocation happens while holding the interprocess lock, so two
        processes cannot hand out the same id (the old
        ``SELECT MAX(faiss_id)+1`` race).
        """
        vectors = np.asarray(vectors, dtype=np.float32)
        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)
        if vectors.shape[0] == 0:
            return []

        with self._lock:
            if not persist:
                if self.index is None:
                    raise RuntimeError("Index not initialized")
                used = _index_ids(self.index)
                start = max([min_id - 1, *used]) + 1
                new_ids = list(range(start, start + vectors.shape[0]))
                self.index.add_with_ids(
                    vectors, np.asarray(new_ids, dtype=np.int64))
                return new_ids

            with _file_lock(self._lock_path):
                self._load_locked()
                used = _index_ids(self.index)
                start = max([min_id - 1, *used]) + 1
                new_ids = list(range(start, start + vectors.shape[0]))
                self.index.add_with_ids(
                    vectors, np.asarray(new_ids, dtype=np.int64))
                self._write_locked()
        print(f"Added {len(new_ids)} vectors with new ids, total: {self.index.ntotal}")
        return new_ids

    def remove_ids(self, ids: Sequence[int], persist: bool = True) -> int:
        """Remove vectors by id; returns how many entries FAISS removed."""
        ids_array = np.asarray(list(ids), dtype=np.int64)
        if ids_array.size == 0:
            return 0
        with self._lock:
            if not persist:
                if self.index is None:
                    return 0
                removed = int(self.index.remove_ids(ids_array))
                print(f"Removed {removed} vectors, total: {self.index.ntotal}")
                return removed

            with _file_lock(self._lock_path):
                self._load_locked()
                removed = int(self.index.remove_ids(ids_array))
                self._write_locked()
            print(f"Removed {removed} vectors, total: {self.index.ntotal}")
            return removed

    def _remove_duplicates_locked(self, ids_array: np.ndarray) -> None:
        """IndexFlatL2 does not enforce unique ids; replace, don't duplicate."""
        if self.index is None or self.index.ntotal == 0:
            return
        used = _index_ids(self.index)
        duplicate = [int(i) for i in ids_array if int(i) in used]
        if duplicate:
            self.index.remove_ids(np.asarray(duplicate, dtype=np.int64))

    # ── search / persistence ─────────────────────────────────────────

    def search(self, query_vector: np.ndarray, k: int = 5) -> Tuple[np.ndarray, np.ndarray]:
        # Pick up writes from the other process before answering.
        self.refresh_if_changed()
        with self._lock:
            if self.index is None or self.index.ntotal == 0:
                return np.array([]), np.array([])
            query = np.asarray(query_vector, dtype=np.float32).reshape(1, -1)
            distances, indices = self.index.search(query, k)
            return distances[0], indices[0]

    def _write_locked(self) -> None:
        """Atomically write the current index. Caller holds file + thread lock."""
        if self.index is None:
            return
        index_dir = os.path.dirname(os.path.abspath(self.index_path))
        if index_dir:
            os.makedirs(index_dir, exist_ok=True)
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
        self._signature = self._file_signature()

    def save(self):
        with self._lock:
            if self.index is None:
                return
            with _file_lock(self._lock_path):
                # If the other process wrote a newer index, merge by reloading
                # before saving; otherwise a stale periodic save would clobber
                # memories created in the Observatory.
                if self._file_signature() != self._signature:
                    self._load_locked()
                self._write_locked()
            print(f"Index saved to {self.index_path}")

    def replace_index(self, new_index, persist: bool = True) -> None:
        """Install a fully built index (used by rebuild)."""
        with self._lock:
            if not persist:
                self.index = new_index
                return
            with _file_lock(self._lock_path):
                self.index = new_index
                self._write_locked()
        print(f"Index replaced, total: {self.index.ntotal}")

    def rebuild_from_active(self, active_ids: List[int], all_vectors: np.ndarray):
        new_index = _new_index(self.dim)
        if len(active_ids) > 0:
            new_index.add_with_ids(
                np.asarray(all_vectors, dtype=np.float32),
                np.asarray(active_ids, dtype=np.int64),
            )
        self.replace_index(new_index, persist=True)
